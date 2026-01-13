import logging
import os
import datetime
import appdirs
import wave
from whisperkey.config import APP_NAME, AudioConfig

logger = logging.getLogger(__name__)


class FileHandler:
    """A class to handle file operations for the application."""

    def __init__(self):
        """Initialize the PID handler using system cache directory."""

        # Create the cache directory if it doesn't exist
        os.makedirs(self.get_cache_dir(), exist_ok=True)

        # Store the full path to the PID file
        self.pid_file = os.path.join(self.get_cache_dir(), 'recorder.pid')

    def get_cache_dir(self):
        """Get the cache directory for the application."""
        return appdirs.user_cache_dir(APP_NAME)

    def create_pid_file(self):
        """Create a PID file with the current process ID."""
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))

    def remove_pid_file(self):
        """Remove the PID file when the process exits."""
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)

    def save_recording(self, frames, audio, audio_config: AudioConfig):
        try:
            """Save the recorded frames to a WAV file."""
            if not frames:
                logger.warning("No audio data to save")
                return None

            # Generate a timestamped filename with full path
            filename = os.path.join(self.get_cache_dir(),
                                    datetime.datetime.now().strftime("recording_%Y%m%d_%H%M%S.wav"))

            logger.info(f"Saving to {filename}")
            with wave.open(filename, 'wb') as wf:
                wf.setnchannels(audio_config.CHANNELS)
                wf.setsampwidth(audio.get_sample_size(audio_config.FORMAT))
                wf.setframerate(audio_config.RATE)
                wf.writeframes(b''.join(frames))

                return filename

        except Exception as e:
            logger.warning(f"Error saving recording: {e}")
            return None
