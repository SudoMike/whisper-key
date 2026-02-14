#!/usr/bin/env python3
import datetime
import logging
import pyaudio
import wave
import os
import signal
import sys
import time
import tty
import termios
import select
import pyperclip
import threading
import argparse
from typing import Optional, Tuple
from openai import OpenAI

# Set up GLib as the D-Bus mainloop BEFORE anything touches dbus.SessionBus()
# (e.g. notify2.init). The shared SessionBus is cached on first use, so the
# mainloop must be attached before that happens.
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default=True)

import notify2  # noqa: E402 – must come after DBusGMainLoop setup

logger = logging.getLogger(__name__)
from whisperkey.keyboard_handler import KeyboardHandler
from whisperkey.utils import show_notification, suppress_stderr
from whisperkey.file_handler import FileHandler
from whisperkey.config import AUDIO_CONFIG
from whisperkey.tray_icon import TrayIcon


class WhisperKey:
    """A class that handles audio recording and transcription using OpenAI's Whisper API."""

    def __init__(
        self,
        device_name: Optional[str] = None,
        device_index: Optional[int] = None,
        keep_audio: bool = False,
        use_openai: bool = False,
    ):
        """Initialize the WhisperKey application."""
        self.file_handler = FileHandler()
        self.audio_config = AUDIO_CONFIG
        self.keep_audio = keep_audio

        # Preferred input device (overridable by env)
        self.preferred_device_name = (
            device_name or os.getenv("WHISPERKEY_INPUT_DEVICE")
        )
        env_index = os.getenv("WHISPERKEY_INPUT_DEVICE_INDEX")
        self.preferred_device_index = (
            device_index if device_index is not None else (
                int(env_index) if env_index else None)
        )

        # Recording state
        self.is_recording = False
        self.cleanup_mode = False
        self.recording_thread = None
        self.frames = []
        self.audio = None
        self.stream = None
        self.recording_complete = False

        # In-memory history of transcripts for tray menu
        # Each entry: {"timestamp": datetime.datetime, "text": str}
        self.transcripts: list[dict] = []

        # Initialize API clients
        if use_openai:
            self.client = OpenAI()
            self.transcription_client = self.client
            self.transcription_model = "whisper-1"
            self.chat_model = "gpt-5"
            logger.warning("Using OpenAI for all API calls")
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error("GROQ_API_KEY not set. Set it or use --openai.")
                sys.exit(1)
            self.client = OpenAI(
                api_key=groq_api_key,
                base_url="https://api.groq.com/openai/v1",
            )
            self.transcription_client = self.client
            self.transcription_model = "whisper-large-v3"
            self.chat_model = "llama-3.3-70b-versatile"
            logger.warning("Using Groq for all API calls")

        # Initialize tray icon (quit callback will be set in run())
        self.tray = None

        # Initialize notification system
        notify2.init("WhisperKey")

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)  # Ctrl+C
        # Termination signal
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ---------------------------
    # Audio device management
    # ---------------------------
    def _list_input_devices(self, p: pyaudio.PyAudio):
        """Return a list of input-capable device info dicts."""
        devices = []
        for i in range(p.get_device_count()):
            with suppress_stderr():
                info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devices.append(info)
        return devices

    def _resolve_input_device(self, p: pyaudio.PyAudio) -> Tuple[Optional[int], Optional[dict]]:
        """Resolve the input device index from preferred name or index.

        Returns a tuple of (device_index, device_info). If no specific device is
        requested or found, returns (None, None) which means use the system default.
        """
        # 1) If index provided, validate it
        if self.preferred_device_index is not None:
            try:
                with suppress_stderr():
                    info = p.get_device_info_by_index(
                        self.preferred_device_index)
                if info.get("maxInputChannels", 0) > 0:
                    return self.preferred_device_index, info
                else:
                    logger.warning(
                        f"Requested device index {self.preferred_device_index} has no input channels. Ignoring.")
            except Exception as e:
                logger.warning(
                    f"Invalid input device index {self.preferred_device_index}: {e}")

        # 2) If name provided, find by case-insensitive substring
        if self.preferred_device_name:
            name_lower = self.preferred_device_name.lower()
            for i in range(p.get_device_count()):
                with suppress_stderr():
                    info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0 and name_lower in str(info.get("name", "")).lower():
                    return i, info
            logger.warning(
                f"No input device matched name '{self.preferred_device_name}'. Using default.")

        # 3) Fallback: None means use default device
        return None, None

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals by stopping recording and cleaning up."""
        if self.is_recording:
            self.stop_recording()

        if self.tray:
            self.tray.stop()

        self.recording_complete = True
        self.file_handler.remove_pid_file()
        sys.exit(0)

    def transcribe_audio(self, filename) -> str | None:
        """Transcribe the audio file using OpenAI's Whisper API."""
        try:
            with open(filename, "rb") as audio_file:
                transcription = self.transcription_client.audio.transcriptions.create(
                    model=self.transcription_model,
                    file=audio_file,
                    response_format="text",
                    language="en",
                )

            logger.info(f"Transcription: {transcription}")
            return transcription

        except Exception as e:
            logger.warning(f"Transcription error: {e}")
            return None

    def cleanup_transcript(self, transcript: str) -> str | None:
        """Use an LLM to clean up the transcript into well-written prose."""
        try:
            response = self.client.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a writing assistant. Rewrite the user's spoken "
                            "transcript as clear, concise, well-structured prose. "
                            "Preserve all of the important points and meaning, but "
                            "remove filler words (ums, ahs, like, you know, etc.), "
                            "stutters, and obvious rambling. Organize the result into "
                            "paragraphs as needed so it reads like deliberate writing."
                        ),
                    },
                    {"role": "user", "content": transcript},
                ]
            )

            cleaned = response.choices[0].message.content if response.choices else None
            if cleaned:
                cleaned = cleaned.strip()
            logger.info("Cleanup transcription completed via LLM")
            return cleaned
        except Exception as e:
            logger.warning(f"Cleanup transcription error: {e}")
            return None

    # ---------------------------
    # Transcript history helpers
    # ---------------------------
    def _add_transcript_to_history(self, transcript: str):
        """Store a transcript in memory for later access via the tray icon."""
        try:
            entry = {
                "timestamp": datetime.datetime.now(),
                "text": transcript,
            }
            self.transcripts.append(entry)
        except Exception:
            # History is best-effort only; never fail the main flow
            pass

    def get_transcripts(self) -> list[dict]:
        """Return the list of stored transcript entries."""
        return self.transcripts

    def copy_transcript_from_history(self, index: int, cleanup: bool = False):
        """Copy a previous transcript to the clipboard, optionally cleaned up first.

        This is used by the tray icon menu callbacks.
        """
        try:
            if index < 0 or index >= len(self.transcripts):
                return

            raw_text = self.transcripts[index].get("text", "")
            if not raw_text:
                return

            if cleanup:
                cleaned = self.cleanup_transcript(raw_text)
                text_to_copy = cleaned if cleaned else raw_text
            else:
                text_to_copy = raw_text

            pyperclip.copy(text_to_copy)
            logger.warning(
                "Copied %s transcript from history",
                "cleaned" if cleanup else "raw",
            )

            if self.tray:
                # Reuse the existing success feedback on the tray icon
                self.tray.set_success()
        except Exception as e:
            logger.warning(f"Error copying transcript from history: {e}")

    def start_recording(self):
        """Start recording audio in a separate thread."""
        if self.is_recording:
            logger.info("Already recording!")
            return

        # Clear previous recording data
        self.frames = []

        # Initialize PyAudio
        with suppress_stderr():
            self.audio = pyaudio.PyAudio()

        # Resolve preferred device
        selected_index, selected_info = self._resolve_input_device(self.audio)

        open_kwargs = {
            "format": self.audio_config.FORMAT,
            "channels": self.audio_config.CHANNELS,
            "rate": self.audio_config.RATE,
            "input": True,
            "frames_per_buffer": self.audio_config.CHUNK,
        }

        # Adjust channel count if device has fewer channels
        if selected_info is not None:
            max_channels = int(selected_info.get(
                "maxInputChannels", self.audio_config.CHANNELS))
            if self.audio_config.CHANNELS > max_channels:
                logger.info(
                    f"Requested channels {self.audio_config.CHANNELS} exceed device capability {max_channels}. Using {max_channels}.")
                open_kwargs["channels"] = max_channels

        if selected_index is not None:
            open_kwargs["input_device_index"] = selected_index
            # Always show which device is being used
            logger.warning(
                f"Using input device [{selected_index}]: {selected_info.get('name')}")
        else:
            # Always show which device is being used
            try:
                with suppress_stderr():
                    default_info = self.audio.get_default_input_device_info()
                logger.warning(
                    f"Using default input device: {default_info.get('name')}")
            except Exception:
                logger.warning(
                    "Using system default input device")

        with suppress_stderr():
            self.stream = self.audio.open(**open_kwargs)

        self.is_recording = True

        # Start recording in a separate thread
        self.recording_thread = threading.Thread(target=self._record_audio)
        self.recording_thread.daemon = True
        self.recording_thread.start()

        if self.tray:
            self.tray.set_recording()
        logger.info("Recording started. Press Ctrl+Alt+G to stop.")

    def _record_audio(self):
        """Record audio until stopped or time limit reached."""
        # Calculate how many chunks we need to read for RECORD_SECONDS
        chunks_to_record = int(
            self.audio_config.RATE / self.audio_config.CHUNK * self.audio_config.RECORD_SECONDS)

        # Record until stopped or time limit reached
        for _ in range(chunks_to_record):
            if not self.is_recording:
                break

            try:
                data = self.stream.read(
                    self.audio_config.CHUNK, exception_on_overflow=False)
                self.frames.append(data)
            except Exception as e:
                logger.warning(f"Error recording audio: {e}")
                break

        # If we reach the time limit
        if self.is_recording:
            self.stop_recording()

    def stop_recording(self):
        """Stop the current recording, save the file, and transcribe it."""
        if not self.is_recording:
            logger.info("Not currently recording!")
            return

        self.is_recording = False

        # Wait for recording thread to finish
        if self.recording_thread and self.recording_thread.is_alive():
            self.recording_thread.join(timeout=1.0)

        if self.stream:
            self.stream.stop_stream()
            self.stream.close()

        if self.audio:
            self.audio.terminate()

        # Save the recording
        filename = self.file_handler.save_recording(
            self.frames, self.audio, self.audio_config)
        if not filename:
            if self.tray:
                self.tray.set_idle()
            show_notification(
                "Error",
                "Failed to save recording",
                "dialog-error",
                urgency=notify2.URGENCY_CRITICAL
            )
            return

        logger.info("Recording stopped. Processing transcription...")

        if self.tray:
            self.tray.set_processing()

        # Transcribe the recording
        transcription = self.transcribe_audio(filename)

        # Clean up audio file unless --keep-audio was specified
        if not self.keep_audio and filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except OSError:
                pass

        if not transcription:
            if self.tray:
                self.tray.set_idle()
            show_notification(
                "Error",
                "Failed to transcribe recording",
                "dialog-error",
                urgency=notify2.URGENCY_CRITICAL
            )
            return

        # Add transcript to in-memory history for tray access
        self._add_transcript_to_history(transcription)

        # Optionally post-process the transcript with an LLM when cleanup_mode is enabled
        if self.cleanup_mode:
            cleaned = self.cleanup_transcript(transcription)
            if cleaned:
                pyperclip.copy(cleaned)
                logger.warning("Cleaned transcription successful")
            else:
                # Fallback to raw transcription if cleanup fails
                pyperclip.copy(transcription)
                logger.warning(
                    "Cleanup transcription failed or returned empty; using raw transcription"
                )
        else:
            pyperclip.copy(transcription)
            logger.warning("Transcription successful")

        # Reset cleanup mode after handling this recording
        self.cleanup_mode = False

        if self.tray:
            self.tray.set_success()

    # ---------------------------
    # Terminal history menu
    # ---------------------------
    def _getch_nonblocking(self, timeout=0.5):
        """Read a single character from stdin with timeout. Returns None on timeout."""
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
        if rlist:
            return sys.stdin.read(1)
        return None

    def _terminal_loop(self):
        """Interactive terminal loop handling H for history menu."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            # Patch all log handlers to use \r\n so output isn't garbled in raw mode
            for handler in logging.root.handlers:
                handler.terminator = '\r\n'
            while True:
                ch = self._getch_nonblocking(0.5)
                if ch is None:
                    continue
                if ch in ('h', 'H'):
                    self._history_menu(fd)
                elif ch == '\x03':  # Ctrl+C
                    break
        finally:
            for handler in logging.root.handlers:
                handler.terminator = '\n'
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _print_raw(self, text):
        """Print text in raw terminal mode (convert \\n to \\r\\n)."""
        sys.stdout.write(text.replace('\n', '\r\n'))
        sys.stdout.flush()

    def _history_menu(self, fd):
        """Show history menu and handle selection."""
        while True:
            # Show last 9 transcripts
            recent = self.transcripts[-9:] if self.transcripts else []
            if not recent:
                self._print_raw("\n--- History (empty) ---\n")
                self._print_raw("Press ESC to go back.\n")
            else:
                self._print_raw("\n--- History Menu ---\n")
                for i, entry in enumerate(recent):
                    words = entry["text"].split()
                    preview = " ".join(words[:15])
                    if len(words) > 15:
                        preview += "..."
                    self._print_raw(f"  {i + 1}) {preview}\n")
                self._print_raw("\nPress 1-{} to select, ESC to go back.\n".format(len(recent)))

            ch = self._getch_nonblocking(30)
            if ch is None:
                continue
            if ch == '\x1b':  # ESC
                self._print_raw("\n")
                return
            if ch == '\x03':  # Ctrl+C
                return
            if recent and ch.isdigit() and 1 <= int(ch) <= len(recent):
                selected_idx = int(ch) - 1
                # Convert to absolute index in self.transcripts
                abs_idx = len(self.transcripts) - len(recent) + selected_idx
                result = self._copy_method_menu(abs_idx)
                if result == 'done':
                    # Copied successfully, exit history
                    return

    def _copy_method_menu(self, transcript_index):
        """Show copy method selection. Returns 'done' if copied, 'back' if ESC."""
        entry = self.transcripts[transcript_index]
        words = entry["text"].split()
        preview = " ".join(words[:15])
        if len(words) > 15:
            preview += "..."

        self._print_raw(f"\nSelected: {preview}\n")
        self._print_raw("(c)opy transcript or (i)mproved transcript? (ESC to go back)\n")

        while True:
            ch = self._getch_nonblocking(30)
            if ch is None:
                continue
            if ch == '\x1b':  # ESC
                return 'back'
            if ch == '\x03':  # Ctrl+C
                return 'back'
            if ch in ('c', 'C'):
                pyperclip.copy(entry["text"])
                self._print_raw("Copied verbatim transcript to clipboard.\n")
                if self.tray:
                    self.tray.set_success()
                return 'done'
            if ch in ('i', 'I'):
                self._print_raw("Improving transcript...\n")
                cleaned = self.cleanup_transcript(entry["text"])
                if cleaned:
                    pyperclip.copy(cleaned)
                    self._print_raw("Copied improved transcript to clipboard.\n")
                else:
                    pyperclip.copy(entry["text"])
                    self._print_raw("Improvement failed; copied verbatim transcript.\n")
                if self.tray:
                    self.tray.set_success()
                return 'done'

    def toggle_recording(self):
        """Toggle recording state."""
        if self.is_recording:
            self.stop_recording()
        else:
            # Standard transcription (no cleanup)
            self.cleanup_mode = False
            self.start_recording()

    def toggle_recording_cleanup(self):
        """Toggle recording state for cleanup mode (LLM-processed transcript)."""
        if self.is_recording:
            self.stop_recording()
        else:
            self.cleanup_mode = True
            self.start_recording()

    def run(self):
        """Run the WhisperKey application."""

        # Create PID file to indicate this process is running
        self.file_handler.create_pid_file()

        # Set up and start the tray icon
        self.tray = TrayIcon(
            quit_callback=lambda: self._signal_handler(signal.SIGTERM, None),
        )
        self.tray.start()

        # Set up keyboard listener
        self.keyboard_handler = KeyboardHandler(
            self.toggle_recording, self.toggle_recording_cleanup
        )
        keyboard_setup_success = self.keyboard_handler.setup_keyboard_listener()

        if not keyboard_setup_success:
            show_notification(
                "Error",
                "Failed to set up keyboard listener",
                "dialog-error",
                urgency=notify2.URGENCY_CRITICAL
            )
            self.tray.stop()
            return

        logger.warning(
            "WhisperKey is running. Press Ctrl+Alt+G for standard or Ctrl+Alt+F for cleaned transcription."
        )
        logger.warning("Press H in this terminal for transcript history.")

        # Run interactive terminal loop
        try:
            self._terminal_loop()
        except KeyboardInterrupt:
            self._signal_handler(signal.SIGINT, None)
        finally:
            if self.is_recording:
                self.stop_recording()
            if self.tray:
                self.tray.stop()
            self.file_handler.remove_pid_file()


def _print_devices():
    with suppress_stderr():
        p = pyaudio.PyAudio()
    try:
        with suppress_stderr():
            host_apis = [p.get_host_api_info_by_index(
                i)['name'] for i in range(p.get_host_api_count())]
        print("Host APIs:", host_apis)
        print("Input devices:")
        with suppress_stderr():
            count = p.get_device_count()
        for i in range(count):
            with suppress_stderr():
                info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                name = info.get("name")
                max_in = info.get("maxInputChannels")
                rate = info.get("defaultSampleRate")
                print(f"{i}: {name} | channels={max_in} | defaultRate={rate}")
    finally:
        p.terminate()


def _parse_args():
    parser = argparse.ArgumentParser(
        description="WhisperKey - quick voice-to-text hotkey recorder")
    parser.add_argument("--list-devices", action="store_true",
                        help="List input audio devices and exit")
    parser.add_argument("--device", type=str, default=None,
                        help="Preferred input device name (substring match)")
    parser.add_argument("--device-index", type=int,
                        default=None, help="Preferred input device index")
    parser.add_argument("--rate", type=int, default=None,
                        help="Sample rate (Hz), overrides config")
    parser.add_argument("--channels", type=int, default=None,
                        help="Number of channels, overrides config")
    parser.add_argument("--record-seconds", type=int, default=None,
                        help="Time limit per recording in seconds")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Keep audio files after transcription (default: delete)")
    parser.add_argument("--openai", action="store_true",
                        help="Use OpenAI for transcription and cleanup (default: Groq)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable detailed logging (default: only show device and success messages)")
    return parser.parse_args()


def main():
    """Main entry point for the application."""
    args = _parse_args()

    # Configure logging based on verbosity
    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.list_devices:
        _print_devices()
        return

    whisperkey = WhisperKey(
        device_name=args.device,
        device_index=args.device_index,
        keep_audio=args.keep_audio,
        use_openai=args.openai,
    )

    # Apply CLI overrides to audio configuration
    if args.rate is not None:
        whisperkey.audio_config.RATE = args.rate
    if args.channels is not None:
        whisperkey.audio_config.CHANNELS = args.channels
    if args.record_seconds is not None:
        whisperkey.audio_config.RECORD_SECONDS = args.record_seconds

    whisperkey.run()


if __name__ == "__main__":
    main()
