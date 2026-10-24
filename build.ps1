# Rebuild Nemo.exe.
#
# The build had been living in shell history. It matters that it is exact: the
# app needs the DPI manifest (or the pill lands in the wrong place), the icon, and
# three hidden imports that PyInstaller cannot infer.
#
#   pwsh -File build.ps1
#
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    throw "No venv at $py. Create it first: python -m venv .venv; .venv\Scripts\pip install sounddevice numpy pynput pyperclip pillow pystray pyinstaller"
}

Get-Process Nemo, whisper-server -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

# PyInstaller logs to stderr, and with $ErrorActionPreference = 'Stop' PowerShell
# turns a native command's stderr into a terminating error. Relax it for the
# call and judge success by the exit code instead.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$output = & $py -m PyInstaller --noconfirm --onefile --noconsole --name Nemo --noupx `
    --icon "$root\nemo.ico" `
    --manifest "$root\nemo.manifest" `
    --hidden-import overlay --hidden-import tray --hidden-import pystray._win32 `
    --distpath $root --workpath "$root\.build" --specpath "$root\.build" `
    --log-level ERROR "$root\nemo.py" 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = $previous

if ($code -ne 0) {
    $output | Select-Object -Last 25
    throw "PyInstaller failed (exit $code)"
}

Remove-Item "$root\.build", "$root\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue

$exe = Get-Item "$root\Nemo.exe"
"built {0}  {1:N1} MB  {2}" -f $exe.Name, ($exe.Length / 1MB), $exe.LastWriteTime
