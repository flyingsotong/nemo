"""
Nemo's system tray icon.

Owns the things that have no other home: quit, the two settings files, reload,
start-at-login, and the version number. pystray gives a native Win32 tray icon
and popup menu, so it looks and scales like the rest of the shell.

`run()` blocks until Quit, which keeps the message loop on the main thread.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image


def _open_with_default(path: Path) -> bool:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - deliberate: user asked to edit
            return True
        return False
    except Exception:
        return False


def _faded(image):
    """A greyed, half-transparent copy of the icon: the visible signal that
    dictation is paused, since there is no other window to show it in."""
    try:
        rgba = image.convert("RGBA")
        grey = rgba.convert("L").convert("RGBA")
        grey.putalpha(rgba.getchannel("A").point(lambda a: int(a * 0.45)))
        return grey
    except Exception:
        return image


class Tray:
    def __init__(self, *, version: str, root: Path, startup_get, startup_set,
                 on_reload, icon_path: Path, log,
                 on_copy_last=None, on_insert_last=None, last_preview=None,
                 is_paused=None, on_toggle_pause=None, on_say=None,
                 status_lines=None):
        self.version = version
        self.root = root
        self.startup_get = startup_get
        self.startup_set = startup_set
        self.on_reload = on_reload
        self.icon_path = icon_path
        self.log = log
        self.on_copy_last = on_copy_last
        self.on_insert_last = on_insert_last
        self.last_preview = last_preview
        self.is_paused = is_paused
        self.on_toggle_pause = on_toggle_pause
        self.on_say = on_say
        self.status_lines = status_lines
        self._icon = None

    # -- status lines ----------------------------------------------------
    def _lines(self) -> list:
        if self.status_lines is None:
            return []
        try:
            return list(self.status_lines() or [])
        except Exception:
            return []

    def _status_text(self, index: int):
        def text(item=None) -> str:
            lines = self._lines()
            return lines[index] if index < len(lines) else ""
        return text

    def _status_visible(self, index: int):
        def visible(item=None) -> bool:
            return index < len(self._lines())
        return visible

    # -- pause -----------------------------------------------------------
    def _paused(self, item=None) -> bool:
        if self.is_paused is None:
            return False
        try:
            return bool(self.is_paused())
        except Exception:
            return False

    def _apply_state(self, paused: bool) -> None:
        """Swap the icon and tooltip so pause is visible without opening a menu."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = self._faded_icon if paused else self._normal_icon
            icon.title = (f"Nemo {self.version} - paused"
                          if paused else
                          f"Nemo {self.version} - hold Right Alt to talk")
        except Exception as exc:
            self.log(f"tray state update failed: {exc}")

    def _toggle_pause(self, icon=None, item=None):
        if self.on_toggle_pause is None:
            return
        try:
            now = bool(self.on_toggle_pause())
        except Exception as exc:
            self.log(f"pause toggle failed: {exc}")
            return
        self._apply_state(now)
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass
        self._notify("Paused" if now else "Resumed")

    # -- menu actions ----------------------------------------------------
    def _open_settings(self, icon=None, item=None):
        p = self.root / "config.json"
        if not _open_with_default(p):
            self.log(f"could not open {p}")

    def _open_vocabulary(self, icon=None, item=None):
        p = self.root / "vocabulary.txt"
        if not _open_with_default(p):
            self.log(f"could not open {p}")

    def _reload(self, icon=None, item=None):
        ok = self.on_reload()
        self.log(f"reload settings: {'ok' if ok else 'failed'}")
        self._notify("Reloaded" if ok else "Reload failed")

    def _toggle_startup(self, icon=None, item=None):
        enable = not bool(self.startup_get())
        ok, msg = self.startup_set(enable)
        self.log(f"start at login -> {msg}")
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass
        if not ok:
            self._notify("Autostart failed")
        else:
            self._notify("Autostart on" if enable else "Autostart off")

    def _last_label(self, item=None) -> str:
        text = ""
        if self.last_preview is not None:
            try:
                text = self.last_preview() or ""
            except Exception:
                text = ""
        if not text:
            return "Last: (nothing yet)"
        one = " ".join(text.split())
        return 'Last: "' + (one[:34] + "..." if len(one) > 34 else one) + '"'

    def _has_last(self, item=None) -> bool:
        if self.last_preview is None:
            return False
        try:
            return bool(self.last_preview())
        except Exception:
            return False

    def _copy_last(self, icon=None, item=None):
        ok = False
        if self.on_copy_last is not None:
            try:
                ok = bool(self.on_copy_last())
            except Exception as exc:
                self.log(f"copy last failed: {exc}")
        self._notify("Copied" if ok else "Nothing to copy")

    def _insert_last(self, icon=None, item=None):
        ok = False
        if self.on_insert_last is not None:
            try:
                ok = bool(self.on_insert_last())
            except Exception as exc:
                self.log(f"insert last failed: {exc}")
        self._notify("Inserted" if ok else "Nothing to insert")

    def _quit(self, icon=None, item=None):
        self.log("quit from tray")
        if icon is not None:
            icon.stop()

    def _notify(self, message: str) -> None:
        """Confirm on the pill, not with a Windows toast.

        A tray balloon goes through the Action Center and lands about a second
        after the click, which reads as lag; worse, Focus Assist can swallow it
        entirely. The pill is already the app's surface and queues in ~30 ms.
        Text is kept short because the pill is 252 px wide.
        """
        if self.on_say is None:
            return
        try:
            self.on_say(message)
        except Exception as exc:
            self.log(f"pill confirmation failed: {exc}")

    # -- lifecycle -------------------------------------------------------
    def run(self) -> None:
        import pystray
        from pystray import Menu, MenuItem

        try:
            image = Image.open(self.icon_path)
        except Exception:
            image = Image.new("RGBA", (64, 64), (242, 178, 62, 255))

        self._normal_icon = image
        self._faded_icon = _faded(image)

        menu = Menu(
            MenuItem(f"Nemo {self.version}", None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(self._status_text(0), None, enabled=False,
                     visible=self._status_visible(0)),
            MenuItem(self._status_text(1), None, enabled=False,
                     visible=self._status_visible(1)),
            MenuItem(self._status_text(2), None, enabled=False,
                     visible=self._status_visible(2)),
            Menu.SEPARATOR,
            MenuItem("Pause dictation", self._toggle_pause,
                     checked=self._paused, default=True),
            Menu.SEPARATOR,
            MenuItem(self._last_label, None, enabled=False),
            MenuItem("Copy last transcript", self._copy_last,
                     enabled=self._has_last),
            MenuItem("Insert last transcript", self._insert_last,
                     enabled=self._has_last),
            Menu.SEPARATOR,
            MenuItem("Open settings (config.json)", self._open_settings),
            MenuItem("Open vocabulary (vocabulary.txt)", self._open_vocabulary),
            MenuItem("Reload settings", self._reload),
            Menu.SEPARATOR,
            MenuItem("Start at login", self._toggle_startup,
                     checked=lambda item: bool(self.startup_get())),
            Menu.SEPARATOR,
            MenuItem("Quit Nemo", self._quit),
        )

        self._icon = pystray.Icon(
            "nemo", image, f"Nemo {self.version} - hold Right Alt to talk", menu)
        self._apply_state(self._paused())
        self.log(f"tray icon up (Nemo {self.version})")
        self._icon.run()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
