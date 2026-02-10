import os
import threading
import logging
from typing import Callable, Optional

from evdev import InputDevice, categorize, ecodes, list_devices

logger = logging.getLogger(__name__)


class KeyboardHandler:
    """Global keyboard shortcut handler for WhisperKey using evdev.

    Reads directly from /dev/input/ devices instead of X11, avoiding
    system hangs when the screen locks/unlocks.
    """

    # Key combos defined as sets of evdev ecodes
    # Ctrl+Alt+G  – standard transcription
    START_STOP_KEYS = {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_G}
    # Ctrl+Alt+F  – cleanup transcription
    CLEANUP_KEYS = {ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_F}

    # Also accept right-hand modifiers
    _MOD_ALIASES = {
        ecodes.KEY_RIGHTCTRL: ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTALT: ecodes.KEY_LEFTALT,
    }

    def __init__(
        self,
        toggle_recording_callback: Callable,
        toggle_recording_cleanup_callback: Optional[Callable] = None,
    ):
        self.toggle_recording_callback = toggle_recording_callback
        self.toggle_recording_cleanup_callback = toggle_recording_cleanup_callback
        self._pressed: set[int] = set()
        self._threads: list[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def setup_keyboard_listener(self) -> bool:
        """Discover keyboard devices and start listener threads."""
        keyboards = self._find_keyboards()
        if not keyboards:
            logger.error(
                "No keyboard input devices found. "
                "Make sure the current user is in the 'input' group: "
                "sudo usermod -aG input $USER  (then log out/in). "
                "Run with --verbose (-v) to see detailed device diagnostics."
            )
            return False

        self._running = True
        for dev_path in keyboards:
            t = threading.Thread(
                target=self._listen, args=(dev_path,), daemon=True
            )
            t.start()
            self._threads.append(t)
            logger.info("Listening on %s", dev_path)

        return True

    def stop(self):
        """Signal listener threads to stop."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _find_keyboards() -> list[str]:
        """Return paths of evdev devices that have keyboard capabilities."""
        all_paths = list_devices()
        logger.warning("evdev: found %d input device(s) in /dev/input/", len(all_paths))

        if not all_paths:
            logger.warning(
                "evdev: list_devices() returned nothing. "
                "Check that /dev/input/ exists and is readable: "
                "ls -la /dev/input/"
            )

        keyboards: list[str] = []
        for path in all_paths:
            try:
                dev = InputDevice(path)
            except PermissionError:
                logger.warning(
                    "evdev: permission denied opening %s – "
                    "is the user in the 'input' group? "
                    "(groups: %s)",
                    path,
                    os.popen("groups").read().strip(),
                )
                continue
            except OSError as exc:
                logger.warning("evdev: OS error opening %s: %s", path, exc)
                continue

            try:
                caps = dev.capabilities(verbose=False)
                ev_key_caps = caps.get(ecodes.EV_KEY, [])
                has_keys = ecodes.KEY_A in ev_key_caps and ecodes.KEY_Z in ev_key_caps

                logger.info(
                    "evdev: %s (%s) – EV_KEY codes: %d, has A-Z: %s",
                    path,
                    dev.name,
                    len(ev_key_caps),
                    has_keys,
                )

                if has_keys:
                    keyboards.append(path)
                    logger.warning("evdev: ✓ keyboard detected: %s (%s)", path, dev.name)

                dev.close()
            except Exception as exc:
                logger.warning("evdev: error reading caps for %s: %s", path, exc)
                try:
                    dev.close()
                except Exception:
                    pass

        if not keyboards:
            logger.warning(
                "evdev: no keyboard devices matched out of %d device(s). "
                "Listing all devices for diagnosis:",
                len(all_paths),
            )
            for path in all_paths:
                try:
                    dev = InputDevice(path)
                    caps = dev.capabilities(verbose=False)
                    cap_types = [ecodes.EV[t] if t in ecodes.EV else str(t) for t in caps.keys()]
                    logger.warning(
                        "  %s | %-40s | caps: %s",
                        path,
                        dev.name,
                        ", ".join(cap_types),
                    )
                    dev.close()
                except Exception as exc:
                    logger.warning("  %s | (cannot open: %s)", path, exc)

        return keyboards

    def _normalise(self, code: int) -> int:
        """Map right-hand modifiers to their left-hand equivalents."""
        return self._MOD_ALIASES.get(code, code)

    def _listen(self, dev_path: str):
        """Read events from a single device in a loop (runs in a thread)."""
        try:
            dev = InputDevice(dev_path)
        except (PermissionError, OSError) as exc:
            logger.warning("Cannot open %s: %s", dev_path, exc)
            return

        logger.info("Keyboard listener started on %s (%s)", dev.name, dev_path)

        try:
            for event in dev.read_loop():
                if not self._running:
                    break
                if event.type != ecodes.EV_KEY:
                    continue

                key_event = categorize(event)
                code = self._normalise(event.code)

                if key_event.keystate in (
                    key_event.key_down,
                    key_event.key_hold,
                ):
                    self._pressed.add(code)
                    self._check_combos()
                elif key_event.keystate == key_event.key_up:
                    self._pressed.discard(code)
        except OSError:
            # Device disconnected – happens e.g. on USB unplug; just exit quietly.
            logger.info("Device %s disconnected", dev_path)
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _check_combos(self):
        """Check whether a registered key combo is currently held."""
        if self.START_STOP_KEYS.issubset(self._pressed):
            self.toggle_recording_callback()
        elif (
            self.toggle_recording_cleanup_callback
            and self.CLEANUP_KEYS.issubset(self._pressed)
        ):
            self.toggle_recording_cleanup_callback()
