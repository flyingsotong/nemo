# Nemo - build record

Version 0.1, 2026-10-23. Published 2026-10-24 at
<https://github.com/flyingsotong/nemo> (MIT), release `v0.1`. This is the account of
how the thing was put together: what it is made of, why each choice was made, what
went wrong, and how to rebuild it. `README.md` is the user manual; this is the
engineering record.

Built on ALAN-WINDOWS (Windows 11, Ryzen 5 7500F, 31.6 GB RAM, Radeon RX 9070 XT,
display 3840x2160 at 150%).

## Published

<https://github.com/flyingsotong/nemo> - MIT, release `v0.1`, asset
`Nemo-0.1-win64.zip` (48 MB: `Nemo.exe` plus `bin-vk`; the model is not included and
is fetched on first run).

Checked from this machine after the push, rather than assumed: 19 files, no model,
no binaries and no personal files in the tree; MIT detected by GitHub; and a zip
rebuilt here hashes to
`0bf2f3d2bf426036d53901ae7ce41eb241b96bcd9e624e45d9b5eda262981d8c`, which is exactly
the digest GitHub records for the uploaded asset. That is the useful check, because
it proves the published binary is this machine's build. Rebuilding a zip is not
guaranteed to be byte-stable in general (timestamps and entry order), so a mismatch
would have been inconclusive - a match is conclusive.

**The build is not reproducible, and that bounds what can be claimed.** Two onefile
builds from identical source, no edits between them, measured:

| Build | Bytes | SHA-256 (first 12) |
|---|---|---|
| 1 | 28,781,870 | `e97592c58a87` |
| 2 | 28,781,576 | `bd933221bad5` |

PyInstaller embeds per-build data, so the hash changes and even the size differs by
a few hundred bytes. The consequence is worth stating plainly, because the obvious
verification story is false: "rebuild it and compare hashes" does not work, and
telling a user to do it would be a claim they could disprove in two commands. What
is true and checkable is narrower - the published file's hash matches the digest in
the release notes, and that same hash is what the VirusTotal scan covered. Anyone
wanting source-level assurance should run `python nemo.py` rather than compare a
rebuild.

**A note on the antivirus reports.** VirusTotal's first pass on v0.1 was 5/70, all
machine-learning heuristics: Microsoft's `Wacatac...Iml`, SentinelOne's "Static AI -
Suspicious PE" (static, so never executed), SecureAge's bare "Malicious", Zillya's
"Blank" template, Bkav's generic packed-binary signature. The mechanism is the
onefile build itself: it unpacks ~200 MB into `%TEMP%\_MEIxxxxx` at every launch,
which is the shape dropper heuristics look for, and an unsigned binary with no
reputation has nothing to offset it. Defender on this machine scans the exact file
with current signatures and reports nothing. `build.ps1 -Onedir` exists as the
structural answer: no temp extraction, so nothing to trigger on.

## Why it exists

Superwhisper was the incumbent: a 156 MB Tauri application, signed by SuperUltra
Inc., with 213 crates of dependencies, ONNX Runtime, llama.cpp, Parakeet, cloud
providers, a SQLite history and a model manager. Alan used one feature of it.
Nemo is that one feature: hold a key, speak, release, the words appear.

The target was not "a smaller Superwhisper" but "the fastest possible path from
letting go of a key to text in the document", offline, with nothing to configure.

## What it is made of

    Nemo.exe            PyInstaller onefile: Python 3.13 + deps + Pillow, ~27 MB
    bin-vk\             whisper.cpp b5454 server, llama.cpp b11819 Vulkan ggml
    models\             ggml-large-v3-turbo.bin (1.5 GB, reused from Superwhisper)
    nemo.py             hotkey, recording, HTTP client, pasting, settings
    overlay.py          the pill: a Win32 layered window drawn with Pillow
    tray.py             tray icon via pystray: quit, settings, history, version
    make_icon.py        generates nemo.ico / nemo.png
    build.ps1           the whole build in one command
    nemo.manifest       DPI-awareness manifest, embedded in the exe

Runtime: one resident process (Nemo.exe) plus a child whisper-server that holds
the model in VRAM and answers on 127.0.0.1:8129. Python deps: sounddevice (capture),
numpy, pynput (keyboard hook and Ctrl+V), pyperclip, pystray, Pillow.

## The path from key to text

1. The Win32 low-level keyboard hook sees Right Alt before the focused app does.
   It latches on the first key-down so auto-repeat cannot restart the mic.
2. `Recorder.start()` opens an InputStream. It tries 16 kHz mono, and falls back
   to 48 kHz mono, 48 kHz stereo, then 44.1 kHz, because Windows endpoints refuse
   formats they do not like. Whatever opens is decimated to 16 kHz on stop.
3. The audio callback also computes an RMS and hands it to the pill, which is why
   the level meter is real rather than decorative.
4. On key-up, `Recorder.stop()` returns int16 PCM, which becomes a WAV in memory.
5. `Server.transcribe()` posts it to `/inference` as multipart, with
   `response_format=text`, `temperature=0`, the language, and an `prompt` field
   built from `vocabulary.txt`.
6. The reply is whitespace-collapsed, checked against a small hallucination list,
   appended to a 10-entry history, then pasted: clipboard, then Ctrl+V, with a
   trailing space so consecutive dictations do not run together.

Encoder cost dominates: ~65 ms of GPU for the 30-second window whisper always
processes, whatever the utterance length.

## Decisions and why

**whisper.cpp rather than faster-whisper.** The ggml model already existed on disk,
so this avoided a 3 GB conversion and download. It also left room for the GPU.

**No GPU build existed, so the ggml was overlaid.** whisper.cpp publishes CPU,
CUDA and Win32 builds, not Vulkan. This machine has no CUDA (AMD card) and no MSVC
or cmake, so compiling was not free. whisper.cpp and llama.cpp share ggml, and
llama.cpp *does* ship a Vulkan build. Before swapping anything, the PE import
tables were compared: `whisper.dll` imports 6 of `ggml.dll`'s 16 exports and 92 of
`ggml-base.dll`'s 690, all present in llama.cpp's build. Then llama.cpp's
`ggml.dll`, `ggml-base.dll`, `ggml-vulkan.dll`, `libomp.dll` and `ggml-cpu-*` were
dropped into `bin-vk\`. Vulkan came up on the RX 9070 XT with `KHR_coopmat`.
whisper's own `ggml-blas.dll` was removed so no backend was left built against the
other ggml ABI.

**A resident server rather than one process per utterance.** Model load is 1.35 s,
which dwarfed inference. Keeping whisper-server alive in the background took an
11-second clip from 1.73 s to 0.13 s.

**No Tk in the shipped app.** Tk was used for the first pill and removed. Its canvas
has no anti-aliasing, and the `-transparentcolor` trick it needs can only key whole
pixels, so every arc was a staircase. The pill is now a bare Win32 window with
per-pixel alpha: Pillow draws at 4x, BOX-downsamples, and the bytes go out through
`UpdateLayeredWindow` as premultiplied BGRA.

**Push-to-talk as the default.** A toggle can be left running by accident and makes
you press twice for every thought. Releasing is an unambiguous end. Toggle remains
available as `DICTATION_MODE`.

**Vocabulary as whisper's initial prompt.** The server accepts a per-request
`prompt` field. Proved it is honoured before relying on it: identical prompts gave
identical output from identical audio, different prompts gave different output, and
on one sentence "Tolaria" transcribed as "Toleria" without the file and correctly
with it.

**The model is fetched, not shipped.** The default model is 1.62 GB, which cannot
live in git and should not ride in a release asset. `--fetch-model` streams it from
the official `ggerganov/whisper.cpp` collection to a `.part` file, hashes it as it
arrives, and renames it into place only when both size and SHA-256 match. A first
run with no model asks whether to download it.

Where the original came from matters for honesty: the model that has been running
since the first build was **copied out of Superwhisper's own model folder**
(`%LOCALAPPDATA%\com.superwhisper.app\models\`), because it was already on disk and
Nemo had no way to fetch one. It was later verified against Hugging Face: the local
file is byte-identical to the published `ggml-large-v3-turbo.bin`, matching both its
1,624,555,275-byte size and its LFS SHA-256
`1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69`. Superwhisper's
copy hashes identically, so all three files are the same object.

Two libraries in `bin-vk\` were left over from that borrow: `llama.dll` (from
llama.cpp's release) and `parakeet.dll` (a Superwhisper component). Neither is
referenced by `whisper-server.exe`; removing both and transcribing still works, so
they now sit in `.unused\` rather than shipping.

Superwhisper was uninstalled on 2026-10-24, which settles the dependency question
empirically: Nemo transcribed in 126 ms with it gone, its model folder deleted and no
process running. That path in the paragraph above no longer exists on this machine;
`--fetch-model` is now the only way to obtain the weights, and it verifies them
against the digest recorded here.

**The menu reports the engine, it does not claim one.** The tray shows three facts:
which accelerator and device, which model file and how big, and that audio stays
local. The engine line is parsed from the whisper-server banner itself (`loaded
Vulkan backend from ...ggml-vulkan.dll`, `ggml_vulkan: 0 = <device>`), so it names
the real device the driver reported rather than a string this app wrote. That meant
capturing the server's stderr instead of discarding it, which is also the only way to
tell the difference between "GPU failed and quietly fell back to CPU" and "GPU". When
the app cannot see the banner - a second instance attaching to a server it did not
start - it says **backend unknown**. An earlier draft said "CPU only" in that case,
which is a guess dressed as a fact, and the one thing a diagnostics line must never
do is guess.

**`--status` exists so the claims are checkable outside the app.** The tray menu can
say what the engine is, but a menu is not evidence you can hand to someone else. The
command prints the engine, the loopback endpoint, the model path and size, the
microphone and the privacy line; `--verify` adds a SHA-256 of the model compared
against the published digest. It probes the engine by running
`whisper-server.exe -h`, which enumerates Vulkan devices and loads backends before
printing usage, so it answers "will this use my GPU" in ~0.5 s without loading the
model or touching VRAM - a full start would take 2.5 s and hold 1.6 GB of VRAM just
to print a status line.

**The first run recommends a model for the machine, not for the website.** Measured on
a 3.7 s phrase, same machine, GPU removed between runs:

| Model | Vulkan | CPU only |
|---|---|---|
| large-v3-turbo | 114 ms | 8669 ms |
| small.en | 112 ms | 1716 ms |

Two things follow. On a GPU the model size is free, so recommending anything smaller
would cost accuracy for nothing. On CPU the big model is five times the wait, and
8.7 s per phrase is the difference between "useful" and "why is this broken". So
`--fetch-model` with no name probes the engine (0.5 s, no model load) and picks
turbo when Vulkan is present, `small.en` when it is not, and `large-v3-turbo-q5_0`
when the language is not English, because `small.en` cannot speak it. The first-run
dialog offers the recommendation as Yes and the alternative as No, so the choice
stays the user's. Whatever gets installed is written back to `MODEL_FILE` - otherwise
a CPU user who accepted `small.en` would be asked again next start, with the app still
looking for a file called `large-v3-turbo.bin`.

**The first run says the hotkey out loud, once.** A windowless build has no console
to print "hold Right Alt to talk" into, and Windows 11 files new tray icons into the
overflow where they are invisible until someone opens the chevron. Testing onboarding
as a stranger made the gap obvious: the dialog installs a model, the tray appears
somewhere unseen, and the one thing the user must know is the one thing nothing said.
So the first successful start shows a pill reading **Hold Right Alt to talk** for
4.5 s, guarded by a `.onboarded` marker so it never nags again. Verified by capturing
the screen on a first start (pill present) and on a second (absent, no new log line).

**Log in the plain text file, not a database.** The app should be debuggable by
reading one file. It is capped at 512 KB / last 400 lines.

**Every confirmation goes on the pill, not through a Windows toast.** Tray balloons
go through the Action Center and land about a second after the click, which reads as
lag rather than feedback, and Focus Assist can suppress them outright. The pill is
already the app's surface and queues in ~30 ms, so tray actions use it: a `say:<text>`
state that auto-hides after 0.9 s. The only cost is that confirmations vanish when
`OVERLAY` is false, which is consistent with silent mode.

**MIT for Nemo, notices for everything it bundles.** Alan chose MIT. That covers
this code only: `bin-vk\` is whisper.cpp and llama.cpp (both MIT), and the Python
dependencies are mostly MIT/BSD but include **`pynput` and `pystray`, which are
LGPLv3**. They are redistributed unmodified, and because the LGPL asks that a
recipient be able to replace them, the repo ships `requirements.txt` and `build.ps1`
so the whole bundle can be rebuilt against a modified copy; `--onedir` is the other
route. `THIRD-PARTY-NOTICES.md` records all of it. The model is never redistributed,
which keeps the heaviest licence question - the weights - out of the release.

Personal data stays out of the repository by construction: `config.json` (mic,
device names) and `vocabulary.txt` (drafted around Alan's own brands) are both
git-ignored, with `vocabulary.example.txt` as the shipped sample.

## Traps, in the order they cost time

1. **whisper.cpp has no Vulkan Windows build.** Solved by the ggml overlay above,
   but only after verifying the import surface rather than hoping.
2. **A tray icon's default action is a trap.** The menu needs a default item, and I
   first pointed it at "Open settings" - so a stray left click on the icon launched
   whatever handles `.json` (VS Code here). The default action now pauses dictation
   instead, which is the tool's only real state and also restores the Alt key.
3. **`suppress_event()` inside pynput's hook also suppresses the callbacks.** A
   suppressed hotkey therefore never fires. The action has to be driven from
   `win32_event_filter` itself, and always off-thread: the hook must return within
   a few hundred milliseconds or Windows silently drops it.
4. **A Tk canvas cannot do partial transparency.** Chroma key is whole-pixel only.
   Hence the layered window.
5. **`WS_EX_NOACTIVATE` is `0x08000000`.** `0x02000000` is `WS_EX_COMPOSITED`, and a
   layered window carrying it makes `UpdateLayeredWindow` fail with
   `ERROR_INVALID_PARAMETER` (87) - which looks like "layered windows are broken".
6. **A layered window still needs `WS_EX_TOPMOST`, re-asserted on every show.** Without
   it the pill renders perfectly and is then partly covered by whatever window owns
   that part of the screen. It looks exactly like a broken renderer.
7. **DPI awareness must be in the exe's manifest.** `python.exe` ships a DPI-aware
   manifest, so running from source looked right, while the PyInstaller exe got
   logical window geometry against physical monitor metrics and put the pill off the
   bottom edge at the wrong size. A runtime `SetProcessDpiAwarenessContext` call does
   not rescue a process whose manifest has already spoken. `nemo.manifest` declares
   `permonitorv2`.
8. **The display here is 3840x2160 at 150%.** .NET reported 2560x1440 because it is
   DPI-virtualised, which sent me hunting a bug that did not exist.
9. **argparse fires during `parse_args()`.** `--version` printed nothing in a
   `--noconsole` build because the console attach happened afterwards. Fixed in 0.1
   by attaching before the parser runs.
10. **PowerShell turns a native command's stderr into a terminating error** under
   `$ErrorActionPreference = 'Stop'`, which aborted `build.ps1` on PyInstaller's
   ordinary logging. The script relaxes it for that call and judges by `$LASTEXITCODE`.
11. **`Get-Content`/`Set-Content` round trips corrupt UTF-8.** Seven em dashes in the
    README became mojibake. Files on this machine are now written with the file tool,
    and the docs avoid em dashes.
12. **The default output device is "FxSound Speakers", not the soundbar.** Testing by
    playing a clip to the default device leaves the soundbar mic silent, which looks
    exactly like a dead microphone. Play to the soundbar explicitly.
13. **`PASTE: false` did nothing at all** until 0.1: the only route to the clipboard
    was inside the paste function, which that flag skips. Now the copy and the paste
    are separate steps.
14. **A `--noconsole` build has no stderr, so exiting with a message is exiting
    silently.** The missing-model check printed to stderr and returned 2. Double
    clicking the exe therefore did nothing at all - no tray, no window, no error. A
    first run now shows a MessageBox and offers the download. Any fatal condition in
    a windowless build has to surface as a dialog or a log line, never as a print.
15. **A killed download leaves an invisible `.part`.** Found 198 MB of it in `models\`
    after a test process was killed at 10 per cent. Nothing resumes a partial, so on
    a 1.6 GB model interrupted at 90 per cent a user would be down 1.4 GB that no
    screen in the app mentions. `sweep_partials()` now deletes stale ones at startup,
    with an age threshold so a download running in another process is not clobbered.

## Measurements

11-second clip, large-v3-turbo, same audio throughout:

| Path | Time |
|---|---|
| CPU, stock whisper.cpp build | 8.14 s |
| CPU, zen4 backend from the overlay | 6.15 s |
| GPU, one process per utterance (1.35 s load + inference) | 1.73 s |
| GPU, resident server | **0.13 s** |

Other numbers worth keeping:

| What | Value |
|---|---|
| Real dictations, end to end (press to pasted) | ~140-160 ms |
| Pill frame, before / after switching LANCZOS to BOX | 3.26 ms / 1.69 ms |
| Hook live after launch | ~1.1 s (was ~3 s, behind model load) |
| Cold start to usable (server ready + tray) | ~3 s |
| exe size | 27.4 MB |
| Runtime payload, excluding the model | 27.4 MB exe + 65.8 MB bin-vk |
| Model download (`tiny`) through the frozen exe | 4.1 s, hash verified |

Why the transcript time barely moves with the length of the utterance. Same
sentence, padded with silence so the words and tokens are identical:

| Audio length | Transcript time |
|---|---|
| 3.9 s | 101 ms |
| 9.4 s | 107 ms |
| 21.4 s | 119 ms |

5.5x the audio costs 18 ms, because whisper always encodes a fixed 30-second
window. The encoder pass dominates and it is the same pass regardless. So a short
dictation is not faster than a long one, which is why the tool feels constant-cost.

What each part of one dictation costs, measured:

| Stage | Cost |
|---|---|
| encode + decode (server round trip) | 101-119 ms |
| clipboard write + verify + Ctrl+V | 3.2 ms (was 52.8 ms) |
| total, typical | ~105-125 ms |

The output path was a flat `time.sleep(0.05)` after copying, which measured 52.8 ms
of a ~155 ms dictation - larger than the encoder's variability and pure padding. It
now polls the clipboard until it actually holds the text (typically immediate, capped
at 100 ms) and pastes at once: 3.2 ms median, verified by timing both versions into a
Tk text widget and reading the text back to confirm it still arrives.

## Rebuilding

    pwsh -File build.ps1

The manifest, icon and three hidden imports (`overlay`, `tray`, `pystray._win32`)
are in the script because they are not optional. Recreating the venv:

    python -m venv .venv
    .venv\Scripts\pip install sounddevice numpy pynput pyperclip pillow pystray pyinstaller

## Deliberately not done

Streaming partial text (pointless at 130 ms), gating on whisper's `no_speech_prob`
(measured: on silence the model reported 1.9e-05, i.e. it was certain its
hallucination was speech, so the guard would never fire), multi-model switching
(turbo is already fast, and a second knob can be wrong), and any cloud path.

## Known limits at 0.1

- No quit affordance except the tray menu; no tray-less exit.
- No spoken formatting ("new line", "new paragraph"). This is the next thing worth
  doing: whisper gives punctuation but no structure.
- No microphone picker. Switching inputs means editing `config.json` and using
  Reload settings.
- History is in memory only, ten entries, gone on quit.
- Paste is Ctrl+V. Targets that want Ctrl+Shift+V (terminals) will not receive it.
- If the soundbar mic reads below `MIN_PEAK` (0.012), speech is rejected as
  "No speech". Playback through it measured 0.0117, so quiet speech is the first
  suspect if that ever appears.
