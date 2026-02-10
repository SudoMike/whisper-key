from pynput import keyboard
from typing import Callable, Optional


class KeyboardHandler:
    """Global keyboard shortcut handler for WhisperKey."""

    # Keyboard shortcut configuration
    START_STOP_KEYS = {
        keyboard.Key.ctrl_l,
        keyboard.Key.alt_l,
        keyboard.KeyCode.from_char("g"),
    }  # Ctrl+Alt+G

    CLEANUP_KEYS = {
        keyboard.Key.ctrl_l,
        keyboard.Key.alt_l,
        keyboard.KeyCode.from_char("f"),
    }  # Ctrl+Alt+F

    def __init__(
        self,
        toggle_recording_callback: Callable,
        toggle_recording_cleanup_callback: Optional[Callable] = None,
    ):
        """Initialize the keyboard handler."""
        self.current_keys = set()
        self.listener = None
        self.toggle_recording_callback = toggle_recording_callback
        self.toggle_recording_cleanup_callback = toggle_recording_cleanup_callback

    def setup_keyboard_listener(self) -> bool:
        """Set up the keyboard listener for hotkeys."""
        try:
            self.listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
                suppress=False
            )
            self.listener.start()
            return True
        except Exception as e:
            print(f"Error starting keyboard listener: {e}")
            return False

    def _on_press(self, key):
        """Handle key press events."""
        try:
            self.current_keys.add(key)
            # Standard transcription: Ctrl+Alt+G
            if all(k in self.current_keys for k in self.START_STOP_KEYS):
                self.toggle_recording_callback()
            # Cleaned transcription: Ctrl+Alt+F
            elif (
                self.toggle_recording_cleanup_callback
                and all(k in self.current_keys for k in self.CLEANUP_KEYS)
            ):
                self.toggle_recording_cleanup_callback()

        except Exception as e:
            print(f"Error in key press handler: {e}")

    def _on_release(self, key):
        """Handle key release events."""
        try:
            self.current_keys.remove(key)
        except (KeyError, Exception):
            # Just ignore if the key wasn't in the set
            pass
