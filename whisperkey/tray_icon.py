"""System tray icon for WhisperKey status indication."""
import threading
import pystray
from PIL import Image, ImageDraw
from typing import Callable, Optional


class TrayIcon:
    """Manages the system tray icon for WhisperKey."""

    # Icon colors for different states
    COLOR_IDLE = "#808080"       # Gray
    COLOR_RECORDING = "#FF0000"  # Red
    COLOR_PROCESSING = "#FFA500" # Orange
    COLOR_SUCCESS = "#00FF00"    # Green

    ICON_SIZE = 22  # Standard system tray icon size

    def __init__(self, quit_callback: Callable[[], None]):
        """Initialize the tray icon.

        Args:
            quit_callback: Function to call when user clicks Quit in menu.
        """
        self._quit_callback = quit_callback
        self._icon: Optional[pystray.Icon] = None
        self._icon_thread: Optional[threading.Thread] = None
        self._success_timer: Optional[threading.Timer] = None

        # Pre-generate icons for each state
        self._icons = {
            "idle": self._create_circle_icon(self.COLOR_IDLE),
            "recording": self._create_circle_icon(self.COLOR_RECORDING),
            "processing": self._create_circle_icon(self.COLOR_PROCESSING),
            "success": self._create_circle_icon(self.COLOR_SUCCESS),
        }

    def _create_circle_icon(self, color: str) -> Image.Image:
        """Create a simple colored circle icon.

        Args:
            color: Hex color string (e.g., "#FF0000").

        Returns:
            PIL Image with the colored circle.
        """
        size = self.ICON_SIZE
        # Use RGB with black background (no alpha channel issues)
        img = Image.new("RGB", (size, size), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Draw filled circle at native size, no anti-aliasing
        draw.ellipse([0, 0, size - 1, size - 1], fill=color)

        return img

    def _create_menu(self) -> pystray.Menu:
        """Create the right-click context menu."""
        return pystray.Menu(
            pystray.MenuItem("WhisperKey", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _on_quit(self, icon, item):
        """Handle quit menu item click."""
        self.stop()
        self._quit_callback()

    def start(self):
        """Start the tray icon in a background thread."""
        if self._icon is not None:
            return

        self._icon = pystray.Icon(
            name="whisperkey",
            icon=self._icons["idle"],
            title="WhisperKey",
            menu=self._create_menu(),
        )

        # Run the icon in a daemon thread
        self._icon_thread = threading.Thread(target=self._icon.run, daemon=True)
        self._icon_thread.start()

    def stop(self):
        """Stop the tray icon."""
        if self._success_timer is not None:
            self._success_timer.cancel()
            self._success_timer = None

        if self._icon is not None:
            self._icon.stop()
            self._icon = None

    def _cancel_success_timer(self):
        """Cancel any pending success-to-idle timer."""
        if self._success_timer is not None:
            self._success_timer.cancel()
            self._success_timer = None

    def set_idle(self):
        """Set the icon to idle state (gray)."""
        self._cancel_success_timer()
        if self._icon is not None:
            self._icon.icon = self._icons["idle"]
            self._icon.title = "WhisperKey - Ready"

    def set_recording(self):
        """Set the icon to recording state (red)."""
        self._cancel_success_timer()
        if self._icon is not None:
            self._icon.icon = self._icons["recording"]
            self._icon.title = "WhisperKey - Recording..."

    def set_processing(self):
        """Set the icon to processing state (orange)."""
        self._cancel_success_timer()
        if self._icon is not None:
            self._icon.icon = self._icons["processing"]
            self._icon.title = "WhisperKey - Processing..."

    def set_success(self, duration: float = 2.0):
        """Set the icon to success state (green), then revert to idle.

        Args:
            duration: How long to show success state before reverting (seconds).
        """
        self._cancel_success_timer()

        if self._icon is not None:
            self._icon.icon = self._icons["success"]
            self._icon.title = "WhisperKey - Copied!"

        # Schedule revert to idle (use _set_idle_internal to avoid cancelling our own timer)
        self._success_timer = threading.Timer(duration, self._set_idle_from_timer)
        self._success_timer.daemon = True
        self._success_timer.start()

    def _set_idle_from_timer(self):
        """Internal method called by success timer - doesn't cancel the timer."""
        self._success_timer = None  # Clear reference since timer has fired
        if self._icon is not None:
            self._icon.icon = self._icons["idle"]
            self._icon.title = "WhisperKey - Ready"
