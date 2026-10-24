"""
Nemo's listening pill.

A borderless, always-on-top, click-through window rendered with Pillow at 4x and
downsampled with Lanczos, then blitted through UpdateLayeredWindow so the window
has real per-pixel alpha. That matters because a chroma-keyed Tk canvas can only
do whole-pixel transparency, which leaves visible stair-steps on a curve.

No Tk here: a bare Win32 window owns the HWND and this thread runs a small
message pump alongside the animation.
"""
from __future__ import annotations

import ctypes
import math
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- look -----------------------------------------------------------------
W, H = 252, 54
R = 20                     # corner radius, in final pixels
SS = 4                     # supersample factor
BAR_COUNT = 9
BAR_W, BAR_GAP = 3, 4
BAR_MIN, BAR_MAX = 4, 26

BG_TOP = (24, 29, 36)
BG_BOT = (15, 19, 25)
EDGE = (255, 255, 255, 34)
FG = (233, 238, 244)
ACCENT = (242, 178, 62)
MUTED = (150, 161, 173)
GREEN = (87, 211, 140)
RED = (226, 112, 96)

FONT_DIR = Path("C:/Windows/Fonts")


def _font(px: int):
    for name in ("bahnschrift.ttf", "seguisb.ttf", "segoeui.ttf", "arial.ttf"):
        p = FONT_DIR / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), px)
            except Exception:
                continue
    return ImageFont.load_default()


def _resample():
    """Area-average downsampling.

    SS is a power of two, so BOX averages whole pixel blocks: for flat fills and
    text it is at least as faithful as Lanczos (no ringing) and measured ~1.6 ms
    per frame faster, which is most of the render cost.
    """
    try:
        return Image.Resampling.BOX
    except AttributeError:  # Pillow < 9
        return Image.BOX


# ---- Win32 glue -----------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]



class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _dpi_aware() -> None:
    """Coordinates must be physical, or the pill lands off the bottom edge on a
    scaled display. Safe to call more than once."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def cursor_workarea():
    """(left, top, right, bottom) of the monitor holding the pointer."""
    try:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


# ---- drawing --------------------------------------------------------------
_font_cache: dict[int, object] = {}


def _text_font():
    size = 15 * SS
    if size not in _font_cache:
        _font_cache[size] = _font(size)
    return _font_cache[size]


def _background() -> Image.Image:
    """Static layer: gradient inside a rounded rect, plus a hairline border."""
    w, h, r = W * SS, H * SS, R * SS
    grad = Image.new("RGB", (w, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1)
        gd.line([(0, y), (w, y)], fill=tuple(
            int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    ImageDraw.Draw(img).rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                                          outline=EDGE, width=SS // 2 or 1)
    return img


_BG = None


def _bar_geo():
    total = BAR_COUNT * BAR_W + (BAR_COUNT - 1) * BAR_GAP
    x0 = W - 20 - total
    return x0, total


def render(label: str, phase: float, hist) -> Image.Image:
    global _BG
    if _BG is None:
        _BG = _background()
    img = _BG.copy()
    d = ImageDraw.Draw(img)
    cy = H * SS // 2

    pulse = (math.sin(phase * 3.4) + 1) / 2

    dot_col, text, show_bars, ellipsis = MUTED, "", False, False
    if label == "listening":
        dot_col, text, show_bars = ACCENT, "Listening", True
        dot_r = (4.2 + 1.4 * pulse) * SS
    elif label == "transcribing":
        dot_col, text, ellipsis = MUTED, "Transcribing", True
        dot_r = (4.0 + 0.9 * pulse) * SS
    elif label == "done":
        dot_col, text = GREEN, "Copied"
        dot_r = 4.6 * SS
    elif label == "error":
        dot_col, text = RED, "No speech"
        dot_r = 4.6 * SS
    elif label == "paused":
        dot_col, text = MUTED, "Paused"
        dot_r = 4.6 * SS
    elif label.startswith("say:"):
        # generic one-line confirmation for tray actions, shown on the app's own
        # surface instead of a Windows toast (which lags about a second)
        dot_col, text = ACCENT, label[4:]
        dot_r = 4.6 * SS
    elif label.startswith("hint:"):
        # the first-run hint: same look, but it stays long enough to read
        dot_col, text = ACCENT, label[5:]
        dot_r = 4.6 * SS
    else:
        dot_r = 4.0 * SS

    # dot with a soft halo while listening
    if show_bars and not ellipsis:
        halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
        hd = ImageDraw.Draw(halo)
        hr = dot_r * 2.2
        hd.ellipse([17 * SS - hr, cy - hr, 17 * SS + hr, cy + hr],
                   fill=(dot_col[0], dot_col[1], dot_col[2], 40))
        img.alpha_composite(halo)
    d.ellipse([17 * SS - dot_r, cy - dot_r, 17 * SS + dot_r, cy + dot_r],
              fill=dot_col)

    if text:
        f = _text_font()
        d.text((32 * SS, cy), text, font=f, fill=FG, anchor="lm")

    if ellipsis:
        # three fading dots, marching
        x0, _ = _bar_geo()
        for i in range(3):
            k = (phase * 2.2 - i * 0.45) % 3.0
            alpha = int(60 + 180 * max(0.0, math.cos((k - 1.5) * 1.6)))
            x = (x0 + 6 + i * 12) * SS
            d.ellipse([x - 3 * SS, cy - 3 * SS, x + 3 * SS, cy + 3 * SS],
                      fill=(MUTED[0], MUTED[1], MUTED[2], alpha))
    elif show_bars:
        x0, _ = _bar_geo()
        for i, lvl in enumerate(hist):
            hgt = (BAR_MIN + lvl * (BAR_MAX - BAR_MIN)) * SS
            x = (x0 + i * (BAR_W + BAR_GAP)) * SS
            d.rounded_rectangle([x, cy - hgt / 2, x + BAR_W * SS, cy + hgt / 2],
                                radius=BAR_W * SS / 2, fill=ACCENT)

    return img.resize((W, H), _resample())


def _to_premultiplied_bgra(img: Image.Image) -> bytes:
    a = np.asarray(img.convert("RGBA"), dtype=np.uint8)
    alpha = a[:, :, 3:4].astype(np.float32) / 255.0
    rgb = (a[:, :, :3].astype(np.float32) * alpha).astype(np.uint8)
    out = np.empty((H, W, 4), dtype=np.uint8)
    out[:, :, 0] = rgb[:, :, 2]
    out[:, :, 1] = rgb[:, :, 1]
    out[:, :, 2] = rgb[:, :, 0]
    out[:, :, 3] = a[:, :, 3]
    return out.tobytes()


# ---- the window -----------------------------------------------------------
class PillWindow:
    """Queue-driven pill. start() spawns its own thread and message pump."""

    def __init__(self, log=None) -> None:
        self.q: queue.Queue = queue.Queue()
        self._log = log or (lambda m: None)
        self._ready = threading.Event()
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="pill", daemon=True)

    # public API, callable from any thread
    def start(self) -> None:
        self._thread.start()
        self._ready.wait(4.0)

    def state(self, name: str) -> None:
        self.q.put(("state", name))

    def level(self, value: float) -> None:
        # Levels arrive every ~64 ms and are only meaningful for the current
        # frame. If the renderer ever stalls, dropping them keeps memory flat
        # instead of growing a queue without bound.
        if self.q.qsize() < 64:
            self.q.put(("level", value))

    def _run(self) -> None:
        try:
            self._build()
        except Exception as exc:
            self._log(f"pill setup failed: {exc}")
            self._ready.set()
            return
        self._ready.set()

        hist = [0.0] * BAR_COUNT
        label, phase, until, visible = "hidden", 0.0, 0.0, False
        last = 0.0

        while self._alive:
            try:
                while True:
                    kind, value = self.q.get_nowait()
                    if kind == "state":
                        label = value
                        if (value in ("done", "error", "paused")
                                or value.startswith("say:")):
                            until = time.time() + 0.9
                        elif value.startswith("hint:"):
                            until = time.time() + 4.5
                    elif kind == "level":
                        hist = hist[1:] + [float(value)]
            except queue.Empty:
                pass
            except Exception as exc:
                self._log(f"pill queue error: {exc}")

            if (label in ("done", "error", "paused")
                    or label.startswith(("say:", "hint:"))) and time.time() > until:
                label = "hidden"

            now = time.time()
            if now - last >= 0.033:
                last = now
                phase += 0.033
                try:
                    if label == "hidden":
                        if visible:
                            user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
                            visible = False
                    else:
                        if not visible:
                            self._move()
                            user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                            # re-assert Z-order: another window can otherwise sit
                            # on top of a perfectly good layered window
                            user32.SetWindowPos(self.hwnd, wintypes.HWND(-1),
                                                self._pos.x, self._pos.y, W, H,
                                                0x0010 | 0x0040)  # NOACTIVATE|SHOW
                            visible = True
                        self._blit(render(label, phase, hist))
                except Exception as exc:
                    self._log(f"pill render error: {exc}")

            # pump messages so the window stays responsive to the shell
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            # 30 Hz while visible; while hidden there is nothing to animate, so
            # idle slowly and still notice a state change within ~30 ms
            time.sleep(0.008 if visible else 0.03)

    def _build(self) -> None:
        _dpi_aware()
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        cls = WNDCLASSW()
        cls.style = 0
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "NemoPill"
        if not user32.RegisterClassW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err not in (1410,):  # class already exists
                raise OSError(f"RegisterClassW failed ({err})")

        # LAYERED | TRANSPARENT | TOPMOST | NOACTIVATE | TOOLWINDOW
        # NB: NOACTIVATE is 0x08000000. 0x02000000 is COMPOSITED, which a layered
        # window rejects with ERROR_INVALID_PARAMETER on UpdateLayeredWindow.
        ex = 0x00080000 | 0x00000020 | 0x00000008 | 0x08000000 | 0x00000080
        self.hwnd = user32.CreateWindowExW(
            ex, "NemoPill", "Nemo", 0x80000000, 0, 0, W, H,
            None, None, hinst, None)  # WS_POPUP
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")

        self._screen = user32.GetDC(None)
        self._memdc = gdi32.CreateCompatibleDC(self._screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(
            self._screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not self._hbmp:
            raise OSError("CreateDIBSection failed")
        gdi32.SelectObject(self._memdc, self._hbmp)
        self._bits = bits
        self._blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        self._size = wintypes.SIZE(W, H)
        self._src = wintypes.POINT(0, 0)

    def _move(self) -> None:
        wa = cursor_workarea()
        if wa:
            left, top, right, bottom = wa
        else:
            left, top, right, bottom = 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        x = left + (right - left - W) // 2
        y = bottom - H - 88
        self._pos = wintypes.POINT(int(x), int(y))

    def _blit(self, img: Image.Image) -> None:
        data = _to_premultiplied_bgra(img)
        ctypes.memmove(self._bits, data, len(data))
        ok = user32.UpdateLayeredWindow(
            self.hwnd, self._screen, ctypes.byref(self._pos),
            ctypes.byref(self._size), self._memdc, ctypes.byref(self._src),
            0, ctypes.byref(self._blend), 2)  # ULW_ALPHA
        if not ok:
            err = ctypes.get_last_error()
            self._log(f"UpdateLayeredWindow failed ({err})")

    def stop(self) -> None:
        self._alive = False
