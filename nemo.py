#!/usr/bin/env python3
"""
Nemo - offline dictation. Right Alt to talk, whisper large-v3-turbo on the GPU.

A small pill appears at the bottom of whichever screen the pointer is on while
the mic is live, shows a live level meter, then reports what happened and
disappears. The model is held resident in whisper-server.exe, so per-utterance
cost is encoder time (~65 ms) plus HTTP overhead, not a 1.35 s model load.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

# When frozen by PyInstaller, __file__ points inside the unpacked temp dir, so
# anchor on the exe instead. Sidecar folders (bin-vk, models) stay beside it.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
BIN = ROOT / "bin-vk"
SERVER_EXE = BIN / "whisper-server.exe"
MODELS_DIR = ROOT / "models"
LOGFILE = ROOT / "nemo.log"
# Written the first time Nemo gets as far as running, so the "hold Right Alt" hint
# is shown once and never again.
ONBOARD_MARK = ROOT / ".onboarded"
VOCAB_FILE = ROOT / "vocabulary.txt"
VOCAB_MAX_CHARS = 600     # whisper's prompt window is small; stay well inside it

APP_NAME = "Nemo"
APP_VERSION = "0.1"

# ---- knobs ----------------------------------------------------------------
HOTKEY_VK = 0xA5          # Right Alt (VK_RMENU)
SUPPRESS_HOTKEY = True    # stop Right Alt reaching the focused app (menus / AltGr)
LANGUAGE = "en"           # set to "" for auto-detect
THREADS = 6
PORT = 8129
SAMPLE_RATE = 16000
MIC_DEVICE = None         # None = system default; substring, or an int device index
MIN_SECONDS = 0.30
MIN_PEAK = 0.012          # int16, normalised: reject near-silence
PASTE = True
BEEP = False              # the pill is the feedback now; beeps off by default
OVERLAY = True
# "push": hold Right Alt to record, release to transcribe. "toggle": tap to start,
# tap again to stop. Push is the default because releasing is an unambiguous end.
DICTATION_MODE = "push"
# Append a space after each paste so consecutive dictations do not run together
# ("Hello.World"). The clipboard carries the same text that was pasted.
TRAILING_SPACE = True
# Which model file in models\ to serve. Anything in MODEL_CATALOG can be fetched
# with --fetch-model; the default is the most accurate that stays fast on a GPU.
MODEL_FILE = "ggml-large-v3-turbo.bin"

# A config.json beside the exe overrides any knob above, so the frozen build can
# be reconfigured (mic, language, hotkey) without rebuilding it.
_CONFIG_FILE = ROOT / "config.json"
_OVERRIDABLE = {"HOTKEY_VK", "SUPPRESS_HOTKEY", "LANGUAGE", "THREADS", "PORT",
                "SAMPLE_RATE", "MIC_DEVICE", "MIN_SECONDS", "MIN_PEAK",
                "PASTE", "BEEP", "OVERLAY", "DICTATION_MODE", "TRAILING_SPACE",
                "MODEL_FILE"}


def reload_config() -> bool:
    """Re-read config.json into this module's knobs. True if it was applied."""
    if not _CONFIG_FILE.exists():
        return False
    try:
        cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"config.json ignored: {exc}", file=sys.stderr)
        return False
    for key, val in cfg.items():
        if key in _OVERRIDABLE:
            globals()[key] = val
    return True


reload_config()

MODEL = MODELS_DIR / MODEL_FILE

# Canonical ggml models from huggingface.co/ggerganov/whisper.cpp. The size and
# sha256 for each are the git-lfs object id that repo publishes, which is a strong
# guarantee: the local copy of the default was checked and is byte-identical.
_HF = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
MODEL_CATALOG = {
    "large-v3-turbo": (
        "ggml-large-v3-turbo.bin", 1624555275,
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        "best quality, 1.6 GB; needs a GPU to stay fast"),
    "large-v3-turbo-q5_0": (
        "ggml-large-v3-turbo-q5_0.bin", 574041195,
        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        "one third the download, slightly less accurate"),
    "small.en": (
        "ggml-small.en.bin", 487614201,
        "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
        "English only, fine on a laptop CPU"),
    "tiny": (
        "ggml-tiny.bin", 77691713,
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
        "smallest; for checking the plumbing, not for real work"),
}


def model_key(filename: str | None = None) -> str:
    """Catalog name for a file name, so messages can suggest the right command."""
    target = Path(filename or MODEL_FILE).name
    for name, entry in MODEL_CATALOG.items():
        if entry[0] == target:
            return name
    return "large-v3-turbo"


HALLUCINATIONS = {
    "", ".", "..", "...", "you", "bye.", "thank you.", "thanks for watching!",
    "thank you for watching.", "thank you so much for watching.",
    "[blank_audio]", "(silence)", "[silence]", "subtitles by the amara.org community",
}


# ---- small helpers --------------------------------------------------------
def _ensure_console() -> None:
    """A --noconsole build has no stdout. Reattach to the launching terminal if
    there is one, so --list-devices / --file / --status / --selftest stay usable.

    Every line these modes print is also written to nemo.log by emit(), which is the
    reliable route: a windowless build's stdout depends on the bootloader's handle
    plumbing (PyInstaller leaves a dummy stream, and onefile vs onedir differ), so
    redirection to a file or a pipe can arrive empty. The log never does.
    """
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
    except Exception:
        pass


_log_writes = 0
_LOG_MAX_BYTES = 512 * 1024


def _trim_log() -> None:
    """Keep the log bounded. It is append-only and otherwise grows for ever."""
    try:
        if not LOGFILE.exists() or LOGFILE.stat().st_size <= _LOG_MAX_BYTES:
            return
        tail = LOGFILE.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        LOGFILE.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass


def log(msg: str) -> None:
    global _log_writes
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    _log_writes += 1
    if _log_writes % 500 == 0:
        _trim_log()
    try:
        with LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def emit(msg: str) -> None:
    """Print where possible, and always keep a copy in the log."""
    log(msg)
    try:
        print(msg, flush=True)
    except Exception:
        pass


def beep(kind: str) -> None:
    if not BEEP:
        return
    seq = {"start": [(880, 60)], "stop": [(660, 50)], "error": [(320, 110)],
           "done": [(1100, 35)]}.get(kind, [])

    def run():
        try:
            import winsound
            for freq, ms in seq:
                winsound.Beep(freq, ms)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---- the listening pill ---------------------------------------------------
# Rendering lives in overlay.py: a per-pixel-alpha layered window drawn with
# Pillow at 4x and downsampled, so curves are genuinely smooth instead of
# whole-pixel steps. It sets DPI awareness itself, but this call stays because it
# must happen before any window is created in the process.
try:
    from overlay import PillWindow as Overlay
    _OVERLAY_ERROR = None
except Exception as _exc:  # display is optional; dictation still works without it
    Overlay = None
    _OVERLAY_ERROR = _exc

try:
    from tray import Tray
except Exception as _exc:  # tray is optional too; it carries quit and settings
    Tray = None
    _TRAY_ERROR = _exc


def _enable_dpi_awareness() -> None:
    """Must run before any window is created.

    On a scaled display (this box is 3840x2160 at 150%), an unaware process gets
    window geometry in logical units while GetMonitorInfo reports physical ones,
    which puts the pill off the bottom of the screen.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return  # PER_MONITOR_AWARE_V2
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ---- microphone -----------------------------------------------------------
def resolve_device(name):
    """Accept None, an explicit device index, or a case-insensitive substring."""
    if name is None or name == "":
        return None
    if isinstance(name, int):
        return name
    try:
        import sounddevice as sd
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and str(name).lower() in dev["name"].lower():
                return idx
    except Exception as exc:
        log(f"device lookup failed: {exc}")
    return None


class Recorder:
    def __init__(self) -> None:
        self._blocks: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self._rate = SAMPLE_RATE
        self._on_level = None

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self, device, on_level=None) -> None:
        """Open the mic, tolerating devices that refuse 16 kHz mono.

        Many Windows endpoints only open at their native 48 kHz, sometimes
        stereo. We remember what worked and resample on stop.
        """
        import sounddevice as sd
        with self._lock:
            self._blocks = []
        self._rate = SAMPLE_RATE
        self._on_level = on_level

        def callback(indata, frames, time_info, status):
            if status:
                log(f"audio status: {status}")
            with self._lock:
                self._blocks.append(indata.copy())
            if self._on_level:
                block = indata[:, 0] if indata.ndim > 1 else indata
                rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
                try:
                    self._on_level(min(1.0, (rms / 0.10) ** 0.5))
                except Exception:
                    pass

        last: Exception | None = None
        for rate, ch in ((16000, 1), (48000, 1), (48000, 2), (44100, 1)):
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=ch, dtype="int16",
                    device=device, callback=callback, blocksize=1024,
                )
                stream.start()
            except Exception as exc:
                last = exc
                continue
            self._stream = stream
            self._rate = rate
            if (rate, ch) != (16000, 1):
                log(f"mic opened at {rate} Hz x{ch} (will resample to 16k)")
            return
        raise RuntimeError(f"no usable input format: {last}")

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def stop(self) -> np.ndarray | None:
        self._close()
        with self._lock:
            if not self._blocks:
                return None
            pcm = np.concatenate(self._blocks, axis=0)
        if pcm.ndim > 1 and pcm.shape[1] > 1:
            pcm = pcm.mean(axis=1).astype(np.int16)
        if self._rate != SAMPLE_RATE and len(pcm):
            factor = self._rate / SAMPLE_RATE
            n_out = int(round(len(pcm) / factor))
            if abs(factor - 3.0) < 1e-9:  # exact 48k -> 16k decimation
                pcm = pcm[: n_out * 3].reshape(-1, 3).mean(axis=1).astype(np.int16)
            else:
                idx = np.linspace(0, len(pcm) - 1, n_out)
                pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.int16)
        return pcm.reshape(-1)

    def cancel(self) -> None:
        self._close()
        with self._lock:
            self._blocks = []


def pcm_to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---- whisper server -------------------------------------------------------
class Server:
    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        # What the server says about itself at startup. Kept because the tray can
        # then report the real backend instead of guessing, and because a user
        # wondering "is this using my GPU?" deserves an answer from the process,
        # not from a hard-coded string.
        self._lines: collections.deque[str] = collections.deque(maxlen=200)
        self._gpu_loaded = False
        self._gpu_device = ""
        self._cpu_backend = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _listening(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def _absorb(self, line: str) -> None:
        """Read the banner whisper-server prints: which backends it loaded."""
        self._lines.append(line)
        low = line.lower()
        if "vulkan backend" in low and "loaded" in low:
            self._gpu_loaded = True
        elif line.startswith("ggml_vulkan: 0 ="):
            name = line.split("0 =", 1)[1].split("(")[0].strip()
            if name:
                self._gpu_device = name
        elif "cpu backend" in low and "loaded" in low:
            tail = line.rsplit("ggml-cpu-", 1)
            if len(tail) == 2:
                self._cpu_backend = tail[1].replace(".dll", "").strip()

    def _drain(self, stream) -> None:
        try:
            for raw in stream:
                self._absorb(raw.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    def backend(self) -> dict:
        """What is actually doing the work, according to the server itself.

        The banner is authoritative once it has printed, so the common path does
        no socket work; the connect is only needed when nothing has been seen yet.
        """
        if self._gpu_loaded or self._cpu_backend:
            if self._gpu_loaded:
                return {"ready": True, "accelerator": "Vulkan",
                        "device": self._gpu_device or "GPU"}
            return {"ready": True, "accelerator": "CPU",
                    "device": f"CPU only ({self._cpu_backend})"}
        if not self._listening():
            return {"ready": False, "accelerator": "?", "device": ""}
        # Something is listening but we did not see it start, so we do not know
        # what it is using. Say that, rather than guessing CPU - a wrong answer
        # here is worse than an honest unknown.
        return {"ready": True, "accelerator": "?", "device": "Engine: backend unknown"}


    def ensure(self, wait: float = 180.0) -> bool:
        if self._listening():
            return True
        # If another thread is already starting it (tray reload during a
        # dictation, say), wait for it to come up rather than serialising behind
        # the lock for up to `wait` seconds.
        if not self._lock.acquire(blocking=False):
            deadline = time.time() + wait
            while time.time() < deadline:
                if self._listening():
                    return True
                time.sleep(0.15)
            return False
        try:
            if self._listening():
                return True
            if not SERVER_EXE.exists():
                log(f"missing {SERVER_EXE}")
                return False
            args = [str(SERVER_EXE), "-m", str(MODEL),
                    "--host", "127.0.0.1", "--port", str(self.port),
                    "-t", str(THREADS)]
            if LANGUAGE:
                args += ["-l", LANGUAGE]
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            log(f"starting server: {' '.join(args)}")
            self.proc = subprocess.Popen(
                args, cwd=str(BIN), stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=flags,
            )
            # stderr must be drained or the pipe fills and the server blocks, so
            # a reader thread runs for the life of the process. It is also how the
            # tray learns whether Vulkan came up.
            threading.Thread(target=self._drain, args=(self.proc.stderr,),
                             daemon=True).start()
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    log(f"server exited with {self.proc.returncode}")
                    return False
                if self._listening():
                    log(f"server ready on port {self.port}")
                    return True
                time.sleep(0.15)
            log("server did not become ready")
            return False
        finally:
            self._lock.release()

    def transcribe(self, wav_bytes: bytes, prompt: str = "") -> str:
        boundary = "----nemo" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n".encode()
            )

        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file";'
            f' filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
            + wav_bytes + b"\r\n"
        )
        field("response_format", "text")
        field("temperature", "0")
        if LANGUAGE:
            field("language", LANGUAGE)
        if prompt:
            # whisper's initial prompt: biases spelling and casing toward these
            # terms. Verified against this build - same prompt, same output.
            field("prompt", prompt)
        parts.append(f"--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            f"{self.url}/inference", data=b"".join(parts), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read().decode("utf-8", "replace")

    def shutdown(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def recommend_model(info: dict | None = None) -> tuple[str, str]:
    """Which model suits this machine, and the reason, based on a real probe.

    Measured on this hardware with a 3.7 s phrase: large-v3-turbo takes 8.7 s on
    CPU but 114 ms on Vulkan; small.en takes 1.7 s on CPU and 112 ms on the GPU.
    So on a GPU the bigger model is free, and on CPU it is five times the wait.
    """
    if info is None:
        info = probe_backend()
    english = (not LANGUAGE) or str(LANGUAGE).lower().startswith("en")
    if info.get("accelerator") == "Vulkan":
        return "large-v3-turbo", f"Vulkan GPU found: {info['device']}"
    if info.get("accelerator") == "CPU":
        if english:
            return "small.en", "no Vulkan GPU, so the CPU is doing the work"
        return "large-v3-turbo-q5_0", ("no Vulkan GPU, so the CPU is doing the work; "
                                       "small.en is English-only")
    return "large-v3-turbo", "could not determine which engine will run"


def remember_model(filename: str) -> None:
    """Record the installed model in config.json so the next start picks it."""
    try:
        data = {}
        if _CONFIG_FILE.exists():
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        data["MODEL_FILE"] = filename
        _CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
        globals()["MODEL_FILE"] = filename
        globals()["MODEL"] = MODELS_DIR / filename
        log(f"config.json MODEL_FILE -> {filename}")
    except Exception as exc:
        log(f"could not record MODEL_FILE: {exc}")


# ---- getting the model ----------------------------------------------------
def fetch_model(name: str = "large-v3-turbo", progress=None) -> Path | None:
    """Download a model into models\\ and verify it.

    Streams to a .part file, hashing as it arrives, and only renames on an exact
    size and sha256 match. An interrupted or tampered download therefore cannot be
    mistaken for a working model, which matters because the failure it would cause
    downstream is a silent empty transcript.
    """
    entry = MODEL_CATALOG.get(name)
    if entry is None:
        emit(f"unknown model {name!r}. choices: {', '.join(MODEL_CATALOG)}")
        return None
    filename, expected_bytes, expected_sha, note = entry
    dest = MODELS_DIR / filename
    if dest.exists() and dest.stat().st_size == expected_bytes:
        emit(f"{filename} is already here ({expected_bytes / 1e9:.2f} GB): {dest}")
        return dest

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    url = _HF + filename
    emit(f"downloading {filename} ({expected_bytes / 1e9:.2f} GB) - {note}")
    emit(f"from {url}")

    digest = hashlib.sha256()
    got = 0
    shown = -10
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                pct = int(got * 100 / expected_bytes)
                if pct >= shown + 5:
                    shown = pct
                    emit(f"  {pct:3d}%  {got / 1e9:.2f} / {expected_bytes / 1e9:.2f} GB")
                    if progress:
                        try:
                            progress(pct)
                        except Exception:
                            pass
    except Exception as exc:
        emit(f"download failed: {exc}")
        _discard(part)
        return None

    if got != expected_bytes:
        emit(f"wrong size: got {got} bytes, expected {expected_bytes}")
        _discard(part)
        return None
    if digest.hexdigest() != expected_sha:
        emit(f"checksum mismatch:\n  got      {digest.hexdigest()}\n  expected {expected_sha}")
        _discard(part)
        return None
    os.replace(part, dest)
    emit(f"verified and saved: {dest}")
    return dest


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def sweep_partials(max_age: float = 3600.0) -> None:
    """Delete `.part` files left by a download that was killed.

    Nothing resumes a partial download, so a stale one is only ever wasted disk -
    and it is invisible, which is worse. A 1.6 GB model interrupted at 90 per cent
    would otherwise leave 1.4 GB that no screen in the app mentions.
    """
    try:
        if not MODELS_DIR.exists():
            return
        cutoff = time.time() - max_age
        for part in MODELS_DIR.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    gb = part.stat().st_size / 1e9
                    part.unlink()
                    log(f"removed abandoned partial download: {part.name} ({gb:.2f} GB)")
            except OSError:
                pass
    except OSError:
        pass


def _offer_model_download(overlay) -> bool:
    """First run with no model: say so, recommend one for this machine, install it.

    Two things this fixes. Exiting silently was the old behaviour, and a
    --noconsole build has no stderr to show, so a new user saw nothing at all when
    double-clicking the exe. And the old prompt always offered the big model, which
    on a machine without Vulkan means eight seconds per phrase - so the offer is now
    shaped by what the engine can actually do here.
    """
    if os.name != "nt":
        return False
    info = probe_backend()
    want, reason = recommend_model(info)
    gpu = info.get("accelerator") == "Vulkan"
    unknown = info.get("accelerator") not in ("Vulkan", "CPU")
    other = "small.en" if want != "small.en" else "large-v3-turbo"
    if want == "large-v3-turbo-q5_0":
        other = "large-v3-turbo"

    tail = (f"Yes  = download {want}\n"
            f"No   = download {other} instead\n"
            "Cancel = not now")
    if gpu:
        body = (f"A Vulkan GPU was found: {info.get('device') or 'GPU'}\n\n"
                "Recommended: large-v3-turbo (1.62 GB), about a tenth of a second\n"
                "per phrase. On a GPU the smaller model is no faster, so there is\n"
                "nothing to gain from downloading less.\n\n" + tail)
    elif unknown:
        body = ("Could not tell whether a GPU is available, so here are both:\n\n"
                "  small.en         0.49 GB   faster, English only\n"
                "  large-v3-turbo   1.62 GB   more accurate, needs a GPU to be quick\n\n"
                + tail)
    else:
        body = ("No Vulkan GPU was found, so the CPU will do the transcribing.\n"
                "That changes which model is worth having:\n\n"
                "  small.en         0.49 GB   about 2 seconds per phrase\n"
                "  large-v3-turbo   1.62 GB   about 9 seconds per phrase\n\n"
                "Measured on a Ryzen 5 7500F; a laptop CPU will be slower.\n\n" + tail)
    text = f"Nemo needs a speech model.\n\n{body}"
    try:
        import ctypes
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, f"{APP_NAME} {APP_VERSION}", 0x03 | 0x40)  # YESNOCANCEL | INFO
    except Exception as exc:
        log(f"could not ask about the model: {exc}")
        return False

    if answer == 6:      # IDYES
        pick = want
    elif answer == 7:    # IDNO
        pick = other
    else:                # IDCANCEL / closed
        log(f"model download declined; put {MODEL.name} in {MODELS_DIR}")
        return False

    def show(pct: int) -> None:
        if overlay:
            overlay.state(f"say:Fetching {pct}%")

    log(f"first run: installing {pick} ({reason})")
    got = fetch_model(pick, progress=show)
    if got is None:
        return False
    remember_model(got.name)
    return True


# ---- text out -------------------------------------------------------------
def clean(text: str) -> str:
    text = " ".join(text.split())
    return "" if text.strip().lower() in HALLUCINATIONS else text


def copy_to_clipboard(text: str) -> bool:
    """Put text on the clipboard. True if it landed."""
    try:
        import pyperclip
    except Exception as exc:
        log(f"clipboard unavailable: {exc}")
        return False
    for _ in range(5):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            time.sleep(0.05)
    log("clipboard copy failed")
    return False


def paste(text: str) -> None:
    """Paste into the focused window, adding the joining space we owe the next
    utterance. Both the clipboard and the keystroke carry the same text, so what
    was pasted is what was copied.

    The old code slept a flat 50 ms after copying, to let the clipboard settle.
    Measured, that sleep was the single largest fixed cost in the whole path -
    bigger than encoding - and it was padding, not a requirement. Instead, wait
    only until the clipboard actually holds our text, which is usually immediate.
    """
    payload = text
    if TRAILING_SPACE and payload and not payload.endswith(" "):
        payload += " "
    if not copy_to_clipboard(payload):
        return
    try:
        import pyperclip
        for _ in range(20):            # bounded: give up after ~100 ms
            if pyperclip.paste() == payload:
                break
            time.sleep(0.005)
    except Exception:
        time.sleep(0.02)               # clipboard unreadable; behave like before
    from pynput.keyboard import Controller, Key
    kb = Controller()
    with kb.pressed(Key.ctrl):
        kb.press("v")
        kb.release("v")


# ---- vocabulary -----------------------------------------------------------
_vocab_cache: dict = {"stamp": None, "prompt": ""}


def vocabulary_prompt() -> str:
    """Terms from vocabulary.txt, joined into whisper's initial prompt.

    Cached against the file's mtime and size so the usual case - unchanged file,
    one read per utterance - costs a stat instead of a read and a parse. Kept
    short on purpose: a long prompt skews style and eats the model's prompt
    window.
    """
    try:
        if not VOCAB_FILE.exists():
            return ""
        st = VOCAB_FILE.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if _vocab_cache["stamp"] == stamp:
            return _vocab_cache["prompt"]
        terms = []
        for raw in VOCAB_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            terms.append(line)
        prompt = ", ".join(terms)
        if len(prompt) > VOCAB_MAX_CHARS:
            prompt = prompt[:VOCAB_MAX_CHARS].rsplit(",", 1)[0]
        _vocab_cache["stamp"] = stamp
        _vocab_cache["prompt"] = prompt
        return prompt
    except Exception as exc:
        log(f"vocabulary unreadable: {exc}")
        return ""


# ---- run at login ---------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "Nemo"


def startup_get():
    """The current HKCU Run value for Nemo, or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            try:
                return winreg.QueryValueEx(k, _RUN_NAME)[0]
            except FileNotFoundError:
                return None
    except OSError:
        return None


def startup_set(on: bool) -> tuple[bool, str]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                exe = ROOT / "Nemo.exe"
                if not exe.exists():
                    return False, f"not enabling: {exe} does not exist"
                winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, f'"{exe}"')
                return True, f'enabled: "{exe}" at login'
            try:
                winreg.DeleteValue(k, _RUN_NAME)
            except FileNotFoundError:
                return True, "was not enabled; nothing to remove"
            return True, "disabled"
    except OSError as exc:
        return False, f"registry error: {exc}"


# ---- app ------------------------------------------------------------------
class Nemo:
    def __init__(self, device, server: Server, overlay: Overlay | None) -> None:
        self.server = server
        self.overlay = overlay
        self.recorder = Recorder()
        self.device = device
        self._busy = threading.Lock()
        self.history: collections.deque[str] = collections.deque(maxlen=10)
        self._paused = threading.Event()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def set_paused(self, on: bool) -> bool:
        """Pause dictation. While paused Right Alt is let through to Windows
        instead of being swallowed, which is the only way to get a real Alt key
        back without quitting."""
        if on:
            self._paused.set()
            if self.recorder.active:
                self.recorder.cancel()
                self._state("hidden")
            log("paused: Right Alt passes through, nothing is transcribed")
        else:
            self._paused.clear()
            log("resumed: hold Right Alt to talk")
        return self.paused

    def toggle_pause(self) -> bool:
        return self.set_paused(not self.paused)

    def last_text(self) -> str:
        return self.history[-1] if self.history else ""

    def _state(self, name: str) -> None:
        if self.overlay:
            self.overlay.state(name)

    def say(self, text: str) -> None:
        """Short confirmation in the pill. The pill is this app's own voice and
        appears in about 30 ms; a Windows toast for the same message arrives
        roughly a second later, which reads as lag rather than feedback."""
        if self.overlay:
            self.overlay.state("say:" + text)

    def press(self) -> None:
        """Begin recording. Safe to call when already recording (key auto-repeat)."""
        if self.paused or self.recorder.active:
            return
        if self._busy.locked():
            # the previous utterance is still going out; starting now would
            # transcribe two things into one paste
            log("previous utterance still processing; press ignored")
            return
        try:
            on_level = self.overlay.level if self.overlay else None
            self.recorder.start(self.device, on_level)
            self._state("listening")
            beep("start")
        except Exception as exc:
            log(f"could not start recording: {exc}")
            self._state("error")
            beep("error")
            self.recorder.cancel()

    def release(self) -> None:
        """Stop and transcribe. No-op when nothing is recording, which is the
        normal case for a stray tap, an Esc cancel, or a refused press."""
        if not self.recorder.active:
            return
        if not self._busy.acquire(blocking=False):
            self.recorder.cancel()
            return
        try:
            pcm = self.recorder.stop()
            self._state("transcribing")
            self._finish(pcm)
        except Exception as exc:
            log(f"release failed: {exc}")
            self._state("error")
            self.recorder.cancel()
        finally:
            self._busy.release()

    def toggle(self) -> None:
        if self.recorder.active:
            self.release()
        else:
            self.press()

    def _finish(self, pcm: np.ndarray | None) -> None:
        if pcm is None or len(pcm) < SAMPLE_RATE * MIN_SECONDS:
            log("too short, ignored")
            self._state("hidden")
            return
        peak = float(np.abs(pcm.astype(np.int32)).max()) / 32768.0
        if peak < MIN_PEAK:
            log(f"too quiet (peak {peak:.4f}), ignored")
            self._state("error")
            beep("error")
            return
        if not self.server.ensure():
            self._state("error")
            beep("error")
            return
        t0 = time.perf_counter()
        try:
            raw = self.server.transcribe(pcm_to_wav(pcm), vocabulary_prompt())
        except Exception as exc:
            log(f"transcribe failed: {exc}")
            self._state("error")
            beep("error")
            return
        text = clean(raw)
        elapsed = (time.perf_counter() - t0) * 1000
        if not text:
            log(f"audio {len(pcm)/SAMPLE_RATE:.1f}s -> {elapsed:.0f}ms: ''")
            self._state("error")
            beep("error")
            return
        self.history.append(text)
        if PASTE:
            paste(text)
        else:
            # "clipboard only" mode: no keystrokes, but the text is still put
            # somewhere he can reach it. This used to do nothing at all.
            copy_to_clipboard(text)
        # Report the whole path, not just the server call: the output half is a
        # real part of what he feels and it used to be invisible in the log.
        total = (time.perf_counter() - t0) * 1000
        log(f"audio {len(pcm)/SAMPLE_RATE:.1f}s -> {elapsed:.0f}ms "
            f"+{total - elapsed:.0f}ms out = {total:.0f}ms: {text!r}")
        self._state("done")
        beep("done")

    def cancel(self) -> None:
        if self.recorder.active:
            self.recorder.cancel()
            log("cancelled")
            self._state("hidden")
            beep("error")


def run_listener(app: Nemo):
    """Install the keyboard hook.

    On Windows the low-level hook (win32_event_filter) is the authority: it sees
    Right Alt before anything else, can swallow it, and does not depend on
    pynput's key-name mapping. Work is handed to a worker thread because the hook
    callback must return within a few hundred ms or Windows drops the hook.

    suppress_event() also stops pynput's on_press/on_release for that event, so
    the toggle has to be driven from the hook itself.
    """
    from pynput import keyboard

    holder: dict = {}
    alt_down = threading.Event()
    push = str(DICTATION_MODE).strip().lower() != "toggle"

    def spawn(fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def on_win32(msg, data):
        if data.vkCode != HOTKEY_VK:
            return True
        down = msg in (0x0100, 0x0104)   # WM_KEYDOWN, WM_SYSKEYDOWN
        up = msg in (0x0101, 0x0105)     # WM_KEYUP,   WM_SYSKEYUP
        if app.paused:
            # Paused means the key is none of our business: let Windows have it.
            # The brief pill is the reminder that dictation is off, so a paused
            # app never fails silently.
            if down and not alt_down.is_set():
                alt_down.set()
                app._state("paused")
            elif up:
                alt_down.clear()
            return True
        if down and not alt_down.is_set():
            alt_down.set()               # latch: auto-repeat must not restart
            spawn(app.press if push else app.toggle)
        elif up:
            alt_down.clear()
            if push:
                spawn(app.release)       # releasing ends the utterance
        if SUPPRESS_HOTKEY and holder.get("listener"):
            holder["listener"].suppress_event()
        return False  # handled here; keep it away from on_press

    def on_press(key):
        if key is keyboard.Key.esc:
            app.cancel()

    listener = keyboard.Listener(
        on_press=on_press,
        win32_event_filter=on_win32 if os.name == "nt" else None,
        suppress=False,
    )
    holder["listener"] = listener
    listener.start()
    return listener


# ---- what the app says about itself ---------------------------------------
def engine_phrase(info: dict) -> str:
    """One line describing the accelerator, from a backend()/probe_backend() dict."""
    if info.get("accelerator") == "Vulkan":
        return f"GPU: {info['device']} (Vulkan)"
    if info.get("accelerator") == "CPU":
        return info["device"] or "CPU only"
    return info.get("device") or "backend unknown"


def probe_backend(timeout: float = 20.0) -> dict:
    """Ask the engine binary which backends it can load, without loading a model.

    `whisper-server -h` enumerates the Vulkan devices and loads the backends before
    it prints usage, so this answers "will it use my GPU" in a fraction of a second
    and without touching the model or VRAM.
    """
    probe = Server(0)                    # parser only; this one never listens
    if not SERVER_EXE.exists():
        return {"ready": False, "accelerator": "?",
                "device": f"engine binary missing: {SERVER_EXE}"}
    try:
        out = subprocess.run(
            [str(SERVER_EXE), "-h"], cwd=str(BIN), timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        text = (out.stderr or b"").decode("utf-8", "replace")
    except Exception as exc:
        return {"ready": False, "accelerator": "?", "device": f"probe failed: {exc}"}
    for line in text.splitlines():
        probe._absorb(line.rstrip())
    if probe._gpu_loaded:
        return {"ready": True, "accelerator": "Vulkan",
                "device": probe._gpu_device or "GPU"}
    if probe._cpu_backend:
        return {"ready": True, "accelerator": "CPU",
                "device": f"CPU only ({probe._cpu_backend})"}
    return {"ready": False, "accelerator": "?",
            "device": "engine reported no usable backend"}


def status_report(verify: bool = False) -> int:
    """Everything someone would need in a bug report, printed as plain lines."""
    srv = Server(PORT)
    running = srv._listening()
    emit(f"{APP_NAME} {APP_VERSION}")
    emit(f"{'running':12s} : {'yes - something is serving port ' + str(PORT) if running else 'no'}")

    info = srv.backend()
    if info.get("accelerator") == "?":
        info = probe_backend()           # we cannot see the banner; ask the binary
        note = " (probed)"
    else:
        note = ""
    emit(f"{'engine':12s} : {engine_phrase(info)}{note}")
    emit(f"{'endpoint':12s} : http://127.0.0.1:{PORT} (loopback only)")
    emit(f"{'privacy':12s} : audio never leaves this PC; the only outbound call is --fetch-model")

    if MODEL.exists():
        mb = MODEL.stat().st_size / 1e9
        emit(f"{'model':12s} : {MODEL.name} ({mb:.2f} GB)")
        emit(f"{'model path':12s} : {MODEL}")
        expected = MODEL_CATALOG.get(model_key(), (None, None, None, None))[2]
        if verify and expected:
            digest = hashlib.sha256()
            with MODEL.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            got = digest.hexdigest()
            emit(f"{'model sha256':12s} : {got}")
            emit(f"{'verified':12s} : {'yes, matches the published digest' if got == expected else 'NO - does not match ' + expected}")
        else:
            emit(f"{'model sha256':12s} : unverified (run --status --verify)")
    else:
        emit(f"{'model':12s} : MISSING - run --fetch-model")

    # If the model on disk is the wrong shape for this machine, say so plainly:
    # "it feels slow" is usually just the big model on a CPU.
    want, reason = recommend_model(info)
    if want != model_key():
        emit(f"{'advice':12s} : {reason}; {want} would suit this machine better")

    device = resolve_device(MIC_DEVICE)
    try:
        import sounddevice as sd
        if device is None:
            dev_name = "system default"
        else:
            raw_name = sd.query_devices(device)["name"].strip()
            # Some MME endpoints report a truncated name with an unclosed bracket
            if raw_name.count("(") > raw_name.count(")"):
                raw_name = raw_name.split("(")[0].strip()
            dev_name = f"{raw_name} [device {device}]"
    except Exception:
        dev_name = "unknown"
    emit(f"{'microphone':12s} : {MIC_DEVICE!r} -> {dev_name}")

    hold = str(DICTATION_MODE).strip().lower() != "toggle"
    emit(f"{'hotkey':12s} : Right Alt, {'hold to talk (push)' if hold else 'tap to toggle'}"
         f"{', swallowed' if SUPPRESS_HOTKEY else ', passed through'}")
    emit(f"{'output':12s} : "
         f"{'clipboard + Ctrl+V' if PASTE else 'clipboard only'}"
         f"{', trailing space' if TRAILING_SPACE else ''}")
    return 0


# ---- what the app says about itself ---------------------------------------
def describe_engine(server: "Server") -> list:
    """Three facts for the tray menu, each one true of the running process.

    Written as a module function rather than a closure so it can be tested: the
    point of these lines is that they report reality, so "does it say the right
    thing" has to be checkable.
    """
    info = server.backend()
    if not info.get("ready"):
        engine = "Engine: starting..."
    elif info["accelerator"] == "Vulkan":
        engine = f"GPU: {info['device']} (Vulkan)"
    elif info["accelerator"] == "CPU":
        engine = info["device"] or "CPU only"
    else:
        engine = info["device"] or "Engine: backend unknown"

    model_name = Path(MODEL_FILE).stem.replace("ggml-", "")
    try:
        gb = MODEL.stat().st_size / 1e9
        model = f"Model: {model_name} ({gb:.1f} GB, on this PC)"
    except OSError:
        model = f"Model: {model_name}"

    # The claim worth making, and the one a user can check: the audio goes to a
    # loopback port answered by a process on this machine, and nowhere else.
    privacy = "Local only: audio never leaves this PC"
    return [engine, model, privacy]


def main() -> int:
    # Before argparse: --version and --help print during parsing, and a
    # --noconsole build has no stdout until we attach to the parent console.
    _ensure_console()
    ap = argparse.ArgumentParser(description=f"{APP_NAME} - Right-Alt offline dictation")
    ap.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    ap.add_argument("--device", default=None, help="microphone name substring or index")
    ap.add_argument("--file", default=None, help="transcribe a wav file and print (no hotkey)")
    ap.add_argument("--selftest", type=float, default=None, metavar="SECONDS",
                    help="record from the mic for N seconds, print level + transcript, exit")
    ap.add_argument("--once", action="store_true", help="with --file, also paste the result")
    ap.add_argument("--startup", choices=["on", "off", "status"], default=None,
                    help="start Nemo at login: on, off, or status")
    ap.add_argument("--fetch-model", nargs="?", const="auto", default=None,
                    metavar="NAME",
                    help="download a speech model and exit; with no name, the one "
                         "that suits this machine. names: " + ", ".join(MODEL_CATALOG))
    ap.add_argument("--status", action="store_true",
                    help="print engine, model, microphone and privacy facts, then exit")
    ap.add_argument("--verify", action="store_true",
                    help="with --status, hash the model and compare it to the published digest")
    ap.add_argument("--version", action="version",
                    version=f"{APP_NAME} {APP_VERSION}")
    args = ap.parse_args()
    _enable_dpi_awareness()  # before any Tk window exists
    if (args.list_devices or args.file or args.selftest is not None
            or args.startup or args.fetch_model or args.status):
        _ensure_console()

    # Housekeeping before anything else: a killed download leaves a .part file that
    # nothing else would ever mention.
    sweep_partials()

    if args.status:
        return status_report(verify=args.verify)

    if args.fetch_model:
        name = args.fetch_model
        if name == "auto":
            name, reason = recommend_model()
            emit(f"this machine: {reason}")
            emit(f"so it wants: {name}")
        dest = fetch_model(name)
        if dest is None:
            return 1
        remember_model(dest.name)
        return 0

    if args.startup:
        if args.startup == "status":
            cur = startup_get()
            emit(f"start at login: {'yes  ' + cur if cur else 'no'}")
        else:
            ok, msg = startup_set(args.startup == "on")
            emit(msg)
            if not ok:
                return 1
        return 0

    if args.list_devices:
        import sounddevice as sd
        default_idx = None
        try:
            default_idx = sd.default.device[0]
        except Exception:
            pass
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                mark = "  <= default" if idx == default_idx else ""
                emit(f"[{idx:3d}] {dev['name']}{mark}")
        return 0

    if not MODEL.exists() and (args.file or args.selftest is not None):
        emit(f"missing model: {MODEL}")
        emit(f"get it with: {APP_NAME}.exe --fetch-model {model_key()}")
        return 2

    server = Server(PORT)

    if args.selftest is not None:
        device = resolve_device(args.device if args.device is not None else MIC_DEVICE)
        rec = Recorder()
        emit(f"recording {args.selftest:.1f}s from device {device} ...")
        rec.start(device)
        time.sleep(args.selftest)
        pcm = rec.stop()
        if pcm is None or len(pcm) == 0:
            print("no audio captured", file=sys.stderr)
            return 1
        peak = float(np.abs(pcm.astype(np.int32)).max()) / 32768.0
        rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))
        emit(f"captured {len(pcm)/SAMPLE_RATE:.2f}s  peak={peak:.4f}  rms={rms:.4f}")
        if peak < MIN_PEAK:
            print("!! level below MIN_PEAK - pick another mic with --device", file=sys.stderr)
        if not server.ensure():
            print("whisper server failed to start", file=sys.stderr)
            return 1
        t0 = time.perf_counter()
        try:
            text = clean(server.transcribe(pcm_to_wav(pcm), vocabulary_prompt()))
        finally:
            server.shutdown()
        emit(f"[{(time.perf_counter()-t0)*1000:.0f} ms] {text!r}")
        return 0

    if args.file:
        wav = Path(args.file).read_bytes()
        t0 = time.perf_counter()
        if not server.ensure():
            print("whisper server failed to start", file=sys.stderr)
            return 1
        try:
            text = clean(server.transcribe(wav, vocabulary_prompt()))
        finally:
            server.shutdown()
        emit(f"[{(time.perf_counter()-t0)*1000:.0f} ms] {text}")
        if args.once and text:
            paste(text)
        return 0

    overlay = None
    if Overlay is None:
        log(f"overlay unavailable, running silently: {_OVERLAY_ERROR}")
    elif OVERLAY:
        overlay = Overlay(log=log)
        overlay.start()

    if Tray is None:
        # without the tray there is no way to quit or change settings from the
        # app, so say so rather than leaving him to guess
        log(f"tray unavailable (no quit menu): {_TRAY_ERROR}")

    # First run: say the model is missing and offer to fetch it. This happens after
    # the overlay exists so the download has somewhere to report progress.
    if not MODEL.exists():
        log(f"missing model: {MODEL}")
        emit(f"missing model: {MODEL}")
        if not _offer_model_download(overlay):
            return 2
        log("model ready")

    device = resolve_device(args.device if args.device is not None else MIC_DEVICE)
    if MIC_DEVICE and device is None:
        log(f"microphone {MIC_DEVICE!r} not found; using the system default")
    app = Nemo(device, server, overlay)

    # First run: this is the only place a new user learns the hotkey. The build is
    # windowless, so the console line goes nowhere, and Windows hides new tray icons
    # in the overflow - without this, someone finishes onboarding with an icon they
    # cannot see and no idea what to press.
    if overlay and not ONBOARD_MARK.exists():
        overlay.state("hint:Hold Right Alt to talk")
        log("first run: showing the hotkey hint")
        try:
            ONBOARD_MARK.write_text("shown\n", encoding="utf-8")
        except OSError as exc:
            log(f"could not record the first-run marker: {exc}")

    # Start the hook before warming the model: loading takes ~1.4 s, and a tap
    # during that window should queue behind ensure() rather than be dropped
    # because no listener existed yet.
    listener = run_listener(app)
    log(f"{APP_NAME} {APP_VERSION} listening: "
        f"{'hold' if str(DICTATION_MODE).lower() != 'toggle' else 'tap'} right Alt "
        f"to talk, Esc to cancel (device={device})")
    emit(f"{APP_NAME} {APP_VERSION} running - "
         f"{'hold' if str(DICTATION_MODE).lower() != 'toggle' else 'tap'} Right Alt to talk.")

    if not server.ensure():
        log("warning: whisper server did not start; first use will retry")

    def do_reload() -> bool:
        """Re-read config.json, then restart the whisper server so a changed
        language or thread count actually takes effect."""
        if not reload_config():
            return False
        try:
            server.shutdown()
            server.ensure()
        except Exception as exc:
            log(f"server restart after reload failed: {exc}")
        return True

    def status_lines() -> list:
        return describe_engine(server)

    tray = None
    if Tray is not None:
        def copy_last() -> bool:
            t = app.last_text()
            return copy_to_clipboard(t) if t else False

        def insert_last() -> bool:
            t = app.last_text()
            if not t:
                return False
            paste(t)
            return True

        tray = Tray(version=APP_VERSION, root=ROOT,
                    startup_get=startup_get, startup_set=startup_set,
                    on_reload=do_reload,
                    on_copy_last=copy_last, on_insert_last=insert_last,
                    last_preview=app.last_text,
                    is_paused=lambda: app.paused,
                    on_toggle_pause=app.toggle_pause,
                    on_say=app.say,
                    status_lines=status_lines,
                    icon_path=ROOT / "nemo.png", log=log)

    try:
        if tray is not None:
            tray.run()          # blocks until Quit is chosen from the menu
        else:
            listener.join()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log(f"tray loop failed ({exc}); falling back to listener only")
        try:
            listener.join()
        except KeyboardInterrupt:
            pass
    finally:
        listener.stop()
        app.recorder.cancel()
        if tray is not None:
            tray.stop()
        server.shutdown()
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
