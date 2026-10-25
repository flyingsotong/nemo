# Rebuild Nemo.exe.
#
# The build had been living in shell history. It matters that it is exact: the
# app needs the DPI manifest (or the pill lands in the wrong place), the icon, and
# three hidden imports that PyInstaller cannot infer.
#
#   pwsh -File build.ps1              one file: Nemo.exe beside bin-vk
#   pwsh -File build.ps1 -Onedir      a folder: dist\Nemo\ with _internal and bin-vk
#
# Why the second mode exists: a onefile build is a self-extracting archive, so it
# unpacks Python into %TEMP% and runs from there, which is exactly the behaviour
# antivirus heuristics look for and the reason some engines flag the release. An
# onedir build never unpacks to temp, so it is far less likely to be flagged, at the
# cost of being a folder rather than a single executable. The app behaves the same
# either way: ROOT is the folder holding the exe, so settings, model and logs live
# beside it.
param(
    [switch]$Onedir
)

$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    throw "No venv at $py. Create it first: python -m venv .venv; .venv\Scripts\pip install sounddevice numpy pynput pyperclip pillow pystray pyinstaller"
}

Get-Process Nemo, whisper-server -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

$mode = if ($Onedir) { "--onedir" } else { "--onefile" }
$distpath = if ($Onedir) { Join-Path $root "dist" } else { $root }

# PyInstaller logs to stderr, and with $ErrorActionPreference = 'Stop' PowerShell
# turns a native command's stderr into a terminating error. Relax it for the
# call and judge success by the exit code instead.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$output = & $py -m PyInstaller --noconfirm $mode --noconsole --name Nemo --noupx `
    --icon "$root\nemo.ico" `
    --manifest "$root\nemo.manifest" `
    --hidden-import overlay --hidden-import tray --hidden-import pystray._win32 `
    --distpath $distpath --workpath "$root\.build" --specpath "$root\.build" `
    --log-level ERROR "$root\nemo.py" 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = $previous

if ($code -ne 0) {
    $output | Select-Object -Last 25
    throw "PyInstaller failed (exit $code)"
}

Remove-Item "$root\.build", "$root\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue

if ($Onedir) {
    # bin-vk goes beside the exe, because that is where ROOT points at runtime.
    $appdir = Join-Path $distpath "Nemo"
    if (-not (Test-Path (Join-Path $appdir "bin-vk"))) {
        Copy-Item (Join-Path $root "bin-vk") (Join-Path $appdir "bin-vk") -Recurse
    }
    $exe = Get-Item (Join-Path $appdir "Nemo.exe")
    $total = (Get-ChildItem $appdir -Recurse -File | Measure-Object -Sum Length).Sum
    "built {0}\Nemo.exe  {1:N1} MB  folder {2:N1} MB  {3}" -f $appdir, ($exe.Length / 1MB), ($total / 1MB), $exe.LastWriteTime
    "zip it with: Compress-Archive -Path '{0}' -DestinationPath '{1}'" -f `
        $appdir, (Join-Path $distpath "Nemo-0.1-win64-onedir.zip")
} else {
    $exe = Get-Item "$root\Nemo.exe"
    "built {0}  {1:N1} MB  {2}" -f $exe.Name, ($exe.Length / 1MB), $exe.LastWriteTime
}
