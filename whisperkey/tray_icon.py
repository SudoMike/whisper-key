"""System tray icon for WhisperKey using D-Bus StatusNotifierItem.

Uses the org.kde.StatusNotifierItem D-Bus interface directly,
avoiding any X11 connection that could cause hangs on lock screen.
"""

import logging
import os
import struct
import threading
from typing import Callable, Optional

import dbus
import dbus.service
from gi.repository import GLib

logger = logging.getLogger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

# SNI icon format: array of (width, height, ARGB-pixel-data)
# We generate simple 22x22 filled-circle icons.
ICON_SIZE = 22


def _make_circle_pixmap(r: int, g: int, b: int, a: int = 255) -> list:
    """Create an SNI IconPixmap entry: a colored circle on transparent bg."""
    size = ICON_SIZE
    cx = cy = size / 2.0
    radius_sq = (size / 2.0) ** 2
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            if (x - cx + 0.5) ** 2 + (y - cy + 0.5) ** 2 <= radius_sq:
                pixels.extend(struct.pack(">BBBB", a, r, g, b))
            else:
                pixels.extend(b"\x00\x00\x00\x00")
    return dbus.Struct(
        (dbus.Int32(size), dbus.Int32(size), dbus.ByteArray(bytes(pixels))),
        signature="iiay",
    )


# Pre-built pixmaps for each state
_PIXMAPS = {
    "idle": _make_circle_pixmap(128, 128, 128),
    "recording": _make_circle_pixmap(255, 0, 0),
    "processing": _make_circle_pixmap(255, 165, 0),
    "success": _make_circle_pixmap(0, 255, 0),
}


class _DBusMenu(dbus.service.Object):
    """Minimal com.canonical.dbusmenu implementation (Quit only)."""

    IFACE = "com.canonical.dbusmenu"

    def __init__(self, bus, path, quit_callback):
        super().__init__(bus, path)
        self._quit_callback = quit_callback
        self._revision = 1

    # -- Methods --

    @dbus.service.method(IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        # Root item with one child: "Quit"
        quit_item = dbus.Struct(
            (
                dbus.Int32(1),
                dbus.Dictionary(
                    {"label": dbus.String("Quit"), "enabled": dbus.Boolean(True)},
                    signature="sv",
                ),
                dbus.Array([], signature="v"),
            ),
            signature="ia{sv}av",
        )
        root = dbus.Struct(
            (
                dbus.Int32(0),
                dbus.Dictionary(
                    {"children-display": dbus.String("submenu")},
                    signature="sv",
                ),
                dbus.Array([dbus.Struct(quit_item, variant_level=1)], signature="v"),
            ),
            signature="ia{sv}av",
        )
        return dbus.UInt32(self._revision), root

    @dbus.service.method(IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_type, data, timestamp):
        if item_id == 1 and event_type == "clicked":
            self._quit_callback()

    @dbus.service.method(IFACE, in_signature="ia{sv}", out_signature="a{sv}")
    def GetGroupProperties(self, ids, property_names):
        return dbus.Array([], signature="a{sv}")

    @dbus.service.method(IFACE, in_signature="i", out_signature="")
    def AboutToShow(self, item_id):
        pass

    # -- Properties --

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        if interface == self.IFACE:
            if prop == "Version":
                return dbus.UInt32(3)
            if prop == "TextDirection":
                return dbus.String("ltr")
            if prop == "Status":
                return dbus.String("normal")
        raise dbus.exceptions.DBusException(
            f"Unknown property {interface}.{prop}",
            name="org.freedesktop.DBus.Error.UnknownProperty",
        )

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == self.IFACE:
            return {
                "Version": dbus.UInt32(3),
                "TextDirection": dbus.String("ltr"),
                "Status": dbus.String("normal"),
            }
        return {}


class _StatusNotifierItem(dbus.service.Object):
    """org.kde.StatusNotifierItem D-Bus object."""

    IFACE = "org.kde.StatusNotifierItem"

    def __init__(self, bus, path, bus_name):
        super().__init__(bus, path, bus_name=bus_name)
        self._state = "idle"

    @property
    def _pixmap(self):
        return dbus.Array([_PIXMAPS[self._state]], signature="(iiay)")

    @property
    def _tooltip(self):
        titles = {
            "idle": "WhisperKey - Ready",
            "recording": "WhisperKey - Recording...",
            "processing": "WhisperKey - Processing...",
            "success": "WhisperKey - Copied!",
        }
        return dbus.Struct(
            (
                dbus.String(""),  # icon name (empty = use pixmap)
                dbus.Array([], signature="(iiay)"),  # icon pixmap
                dbus.String(titles.get(self._state, "WhisperKey")),
                dbus.String(""),
            ),
            signature="sa(iiay)ss",
        )

    def set_state(self, state: str):
        self._state = state
        self.NewIcon()
        self.NewToolTip()

    # -- Signals --

    @dbus.service.signal(IFACE)
    def NewIcon(self):
        pass

    @dbus.service.signal(IFACE)
    def NewToolTip(self):
        pass

    @dbus.service.signal(IFACE)
    def NewStatus(self, status):
        pass

    # -- Properties --

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        if interface == self.IFACE:
            props = self._get_all_props()
            if prop in props:
                return props[prop]
        raise dbus.exceptions.DBusException(
            f"Unknown property {interface}.{prop}",
            name="org.freedesktop.DBus.Error.UnknownProperty",
        )

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == self.IFACE:
            return self._get_all_props()
        return {}

    def _get_all_props(self):
        return {
            "Category": dbus.String("ApplicationStatus"),
            "Id": dbus.String("whisperkey"),
            "Title": dbus.String("WhisperKey"),
            "Status": dbus.String("Active"),
            "IconPixmap": self._pixmap,
            "ToolTip": self._tooltip,
            "Menu": dbus.ObjectPath(MENU_PATH),
            "ItemIsMenu": dbus.Boolean(False),
        }


class TrayIcon:
    """Manages the system tray icon for WhisperKey via D-Bus SNI."""

    def __init__(
        self,
        quit_callback: Callable[[], None],
        get_transcripts: Optional[Callable[[], list]] = None,
        copy_transcript: Optional[Callable[[int, bool], None]] = None,
    ):
        self._quit_callback = quit_callback
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sni: Optional[_StatusNotifierItem] = None
        self._menu: Optional[_DBusMenu] = None
        self._success_timer: Optional[threading.Timer] = None

    def start(self):
        """Start the tray icon in a background thread."""
        if self._thread is not None:
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Set up D-Bus objects and run the GLib main loop."""
        try:
            bus = dbus.SessionBus()
        except dbus.exceptions.DBusException as exc:
            logger.error("Cannot connect to D-Bus session bus: %s", exc)
            return

        pid = os.getpid()
        bus_name_str = f"org.kde.StatusNotifierItem-{pid}-1"

        try:
            bus_name = dbus.service.BusName(bus_name_str, bus)
        except dbus.exceptions.DBusException as exc:
            logger.error("Cannot register D-Bus name %s: %s", bus_name_str, exc)
            return

        self._sni = _StatusNotifierItem(bus, ITEM_PATH, bus_name)
        self._menu = _DBusMenu(bus, MENU_PATH, self._quit_callback)

        # Register with the StatusNotifierWatcher
        try:
            watcher = bus.get_object(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
            )
            watcher.RegisterStatusNotifierItem(
                bus_name_str,
                dbus_interface="org.kde.StatusNotifierWatcher",
            )
            logger.info("Registered SNI with StatusNotifierWatcher")
        except dbus.exceptions.DBusException as exc:
            logger.warning(
                "Could not register with StatusNotifierWatcher: %s. "
                "The tray icon may not appear (is the AppIndicator/SNI "
                "shell extension enabled?).",
                exc,
            )

        self._loop = GLib.MainLoop()
        self._loop.run()

    def stop(self):
        """Stop the tray icon."""
        if self._success_timer is not None:
            self._success_timer.cancel()
            self._success_timer = None

        if self._loop is not None:
            self._loop.quit()
            self._loop = None

    def _cancel_success_timer(self):
        if self._success_timer is not None:
            self._success_timer.cancel()
            self._success_timer = None

    def set_idle(self):
        self._cancel_success_timer()
        if self._sni:
            self._sni.set_state("idle")

    def set_recording(self):
        self._cancel_success_timer()
        if self._sni:
            self._sni.set_state("recording")

    def set_processing(self):
        self._cancel_success_timer()
        if self._sni:
            self._sni.set_state("processing")

    def set_success(self, duration: float = 2.0):
        self._cancel_success_timer()
        if self._sni:
            self._sni.set_state("success")
        self._success_timer = threading.Timer(duration, self._idle_from_timer)
        self._success_timer.daemon = True
        self._success_timer.start()

    def _idle_from_timer(self):
        self._success_timer = None
        if self._sni:
            self._sni.set_state("idle")
