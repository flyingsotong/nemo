# Third-party notices

Nemo itself is MIT licensed; see `LICENSE`. It bundles other people's work, and
those components keep their own licences. This file lists them.

## Bundled executables and libraries (`bin-vk\`)

| Component | Licence | Source |
|---|---|---|
| whisper.cpp (`whisper-server.exe`, `whisper.dll`) | MIT | https://github.com/ggerganov/whisper.cpp |
| llama.cpp (`ggml.dll`, `ggml-base.dll`, `ggml-vulkan.dll`, `ggml-cpu-*.dll`, `libomp.dll`) | MIT | https://github.com/ggerganov/llama.cpp |

The ggml libraries are llama.cpp's build substituted for whisper.cpp's, because
whisper.cpp publishes no Vulkan Windows build. Both projects are MIT and share the
same ggml ABI; see `RECORD.md` for how the import surface was verified.

## Python packages compiled into `Nemo.exe`

| Package | Version | Licence |
|---|---|---|
| Python | 3.13 | PSF-2.0 |
| numpy | 2.5.4 | BSD-3-Clause |
| Pillow | 13.0.0 | MIT-CMU |
| sounddevice | 0.5.6 | MIT |
| pyperclip | 1.11.0 | BSD |
| six | 1.17 | MIT |
| **pynput** | 1.8.2 | **LGPLv3** |
| **pystray** | 0.19.5 | **LGPLv3** |
| PyInstaller (build tool only) | 6.22.3 | GPLv2 with the bootloader exception, which explicitly permits distributing non-free bundled programs |

### On the two LGPLv3 components

`pynput` (keyboard hook and the Ctrl+V keystroke) and `pystray` (the tray icon) are
LGPLv3, not MIT. They are redistributed **unmodified**. The LGPL asks that a
recipient be able to replace them, so this repository ships the whole build:

- `requirements.txt` pins the exact versions.
- `build.ps1` reproduces the packaged exe.

Rebuilding with a modified `pynput` or `pystray` produces a working `Nemo.exe`, which
is the relinking route the licence describes. If that is not sufficient for your
jurisdiction, build with `--onedir` instead of `--onefile` and the modules can be
swapped in the output directory directly.

Source for both: https://github.com/moses-palmer/pynput and
https://github.com/moses-palmer/pystray.

## The speech model

Not distributed with Nemo. `--fetch-model` downloads it from
https://huggingface.co/ggerganov/whisper.cpp, and the ggml conversions there are the
MIT-licensed OpenAI Whisper weights.
