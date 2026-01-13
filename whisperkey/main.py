#!/usr/bin/env python3
import datetime
import pyaudio
import wave
import os
import signal
import sys
import time
import pyperclip
import notify2
import threading
import argparse
from typing import Optional, Tuple
from pynput import keyboard
from openai import OpenAI
from whisperkey.keyboard_handler import KeyboardHandler
from whisperkey.utils import show_notification, suppress_stderr
from whisperkey.file_handler import FileHandler
from whisperkey.config import AUDIO_CONFIG
from whisperkey.tray_icon import TrayIcon


class WhisperKey:
    """A class that handles audio recording and transcription using OpenAI's Whisper API."""

    def __init__(self, device_name: Optional[str] = None, device_index: Optional[int] = None):
        """Initialize the WhisperKey application."""
        self.file_handler = FileHandler()
        self.audio_config = AUDIO_CONFIG

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
        self.recording_thread = None
        self.frames = []
        self.audio = None
        self.stream = None
        self.recording_complete = False

        # Initialize OpenAI client
        self.client = OpenAI()

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
                    print(
                        f"Requested device index {self.preferred_device_index} has no input channels. Ignoring.")
            except Exception as e:
                print(
                    f"Invalid input device index {self.preferred_device_index}: {e}")

        # 2) If name provided, find by case-insensitive substring
        if self.preferred_device_name:
            name_lower = self.preferred_device_name.lower()
            for i in range(p.get_device_count()):
                with suppress_stderr():
                    info = p.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0 and name_lower in str(info.get("name", "")).lower():
                    return i, info
            print(
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
                transcription = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text",
                    language="en",
                )

            print(transcription)
            return transcription

        except Exception as e:
            print(f"Transcription error: {e}")
            return None

    def start_recording(self):
        """Start recording audio in a separate thread."""
        if self.is_recording:
            print("Already recording!")
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
                print(
                    f"Requested channels {self.audio_config.CHANNELS} exceed device capability {max_channels}. Using {max_channels}.")
                open_kwargs["channels"] = max_channels

        if selected_index is not None:
            open_kwargs["input_device_index"] = selected_index
            print(
                f"Using input device [{selected_index}]: {selected_info.get('name')} | rate={self.audio_config.RATE} | channels={open_kwargs['channels']}")
        else:
            # Print default device info for visibility
            try:
                with suppress_stderr():
                    default_info = self.audio.get_default_input_device_info()
                print(
                    f"Using default input device: {default_info.get('name')} | rate={self.audio_config.RATE} | channels={open_kwargs['channels']}")
            except Exception:
                print(
                    "No default input device info available. Attempting to open stream with defaults.")

        with suppress_stderr():
            self.stream = self.audio.open(**open_kwargs)

        self.is_recording = True

        # Start recording in a separate thread
        self.recording_thread = threading.Thread(target=self._record_audio)
        self.recording_thread.daemon = True
        self.recording_thread.start()

        if self.tray:
            self.tray.set_recording()
        print("Recording started. Press Ctrl+Alt+G to stop.")

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
                print(f"Error recording audio: {e}")
                break

        # If we reach the time limit
        if self.is_recording:
            self.stop_recording()

    def stop_recording(self):
        """Stop the current recording, save the file, and transcribe it."""
        if not self.is_recording:
            print("Not currently recording!")
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

        print("Recording stopped. Processing transcription...")

        if self.tray:
            self.tray.set_processing()

        # Transcribe the recording
        transcription = self.transcribe_audio(filename)
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

        pyperclip.copy(transcription)
        print("Transcription copied to clipboard!")

        if self.tray:
            self.tray.set_success()

    def toggle_recording(self):
        """Toggle recording state."""
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def run(self):
        """Run the WhisperKey application."""

        # Create PID file to indicate this process is running
        self.file_handler.create_pid_file()

        # Set up and start the tray icon
        self.tray = TrayIcon(quit_callback=lambda: self._signal_handler(signal.SIGTERM, None))
        self.tray.start()

        # Set up keyboard listener
        self.keyboard_handler = KeyboardHandler(self.toggle_recording)
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

        print("WhisperKey is running in the background.")
        print("Press Ctrl+Alt+G to start/stop recording.")

        # Keep the main thread alive
        try:
            while True:
                time.sleep(1)
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
    return parser.parse_args()


def main():
    """Main entry point for the application."""
    args = _parse_args()

    if args.list_devices:
        _print_devices()
        return

    whisperkey = WhisperKey(device_name=args.device,
                            device_index=args.device_index)

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
