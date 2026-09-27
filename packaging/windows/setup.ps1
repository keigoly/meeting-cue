# setup.ps1 - prepare Meeting Cue! on Windows: the speech recognizer (STT helper) environment and its model.
# Safe to re-run: what is already there is skipped. -Force rebuilds the helper environment.
#
#   1. checks    : Windows 10 2004 (build 19041) or later, an NVIDIA GPU (nvidia-smi) and its memory
#   2. uv        : must be on PATH (https://docs.astral.sh/uv/). uv also provides Python 3.12.
#   3. venv      : <app>\stt-venv  <- helpers\windows\stt_helper\requirements.txt (pinned versions)
#   4. model     : <app>\models\faster-whisper-large-v3-turbo (pinned revision, about 1.6 GB)
#   5. self-test : loads the model on the GPU once
#   6. app venv  : <app>\app-venv <- helpers\windows\window_helper\requirements.txt (pywebview; the app itself runs here too)
#   7. shortcut  : Start menu "Meeting Cue!" -> app-venv pythonw.exe packaging\windows\launch.pyw (icon from docs\images\icon.png)
# <app> is $env:MEETCUE_HOME or %USERPROFILE%\.meeting-cue (where the app keeps its data).
# The first run downloads about 3.7 GB (CUDA libraries from PyPI, the model from Hugging Face).
# Nothing is downloaded during meetings: the helper runs with HF_HUB_OFFLINE=1.
# To uninstall: delete the Start menu shortcut and <app>\stt-venv, <app>\app-venv, <app>\models (your recordings stay in <app>\sessions).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\setup.ps1 [-Force] [-SkipGpuCheck] [-NoShortcut]
param([switch]$Force, [switch]$SkipGpuCheck, [switch]$NoShortcut)
$ErrorActionPreference = "Stop"

function Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "NG: $msg" -ForegroundColor Red; exit 1 }

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$AppDir = if ($env:MEETCUE_HOME) { $env:MEETCUE_HOME } else { Join-Path $env:USERPROFILE ".meeting-cue" }
$Venv = Join-Path $AppDir "stt-venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Helper = Join-Path $Repo "helpers\windows\stt_helper\stt_helper.py"
$Req = Join-Path $Repo "helpers\windows\stt_helper\requirements.txt"
$Model = Join-Path $AppDir "models\faster-whisper-large-v3-turbo"
$AppVenv = Join-Path $AppDir "app-venv"
$AppPy = Join-Path $AppVenv "Scripts\python.exe"
$AppPyw = Join-Path $AppVenv "Scripts\pythonw.exe"
$WinReq = Join-Path $Repo "helpers\windows\window_helper\requirements.txt"
$Launch = Join-Path $Repo "packaging\windows\launch.pyw"
$Ico = Join-Path $AppDir "MeetingCue.ico"

Step "checks"
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 19041) { Fail "Windows 10 version 2004 (build 19041) or later is required (this PC: build $build)." }
Write-Host "   Windows build $build"
if (-not $SkipGpuCheck) {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        Fail "No NVIDIA GPU driver (nvidia-smi) was found. The Windows speech recognizer needs an NVIDIA GPU."
    }
    $gpu = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | Select-Object -First 1
    $f = $gpu.Split(",") | ForEach-Object { $_.Trim() }
    Write-Host "   GPU $($f[0]) / $($f[1]) MiB / driver $($f[2])"
    if ([int]$f[1] -lt 6000) {
        Write-Warning "GPU memory is below 6 GB. One channel uses about 2.4 GB, microphone + meeting audio about 4.8 GB."
    }
}

Step "uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv is not on PATH. Install it (for example: winget install --id astral-sh.uv -e), open a new terminal and run this again."
}
Write-Host "   $(& uv --version)"

Step "helper environment: $Venv"
if ($Force -and (Test-Path $Venv)) { Remove-Item -Recurse -Force $Venv }
if (-not (Test-Path $Py)) {
    & uv venv --python 3.12 $Venv
    if ($LASTEXITCODE -ne 0) { Fail "uv venv failed." }
}
& uv pip install --python $Py -r $Req
if ($LASTEXITCODE -ne 0) { Fail "Installing the helper packages failed." }

Step "model: $Model"
& $Py $Helper --setup-model --model $Model
if ($LASTEXITCODE -ne 0) { Fail "Downloading the model failed." }

Step "self-test (loads the model on the GPU)"
& $Py $Helper --self-test --model $Model
if ($LASTEXITCODE -ne 0) { Fail "The helper could not load the model. See the JSON line above (phase=abort)." }

Step "app environment (window): $AppVenv"
if ($Force -and (Test-Path $AppVenv)) { Remove-Item -Recurse -Force $AppVenv }
if (-not (Test-Path $AppPy)) {
    & uv venv --python 3.12 $AppVenv
    if ($LASTEXITCODE -ne 0) { Fail "uv venv (app) failed." }
}
& uv pip install --python $AppPy -r $WinReq
if ($LASTEXITCODE -ne 0) { Fail "Installing the window packages failed." }

if (-not $NoShortcut) {
    Step "Start menu shortcut"
    # The icon: wrap the 256 px PNG in an .ico container (Windows reads PNG-compressed icons).
    $png = [IO.File]::ReadAllBytes((Join-Path $Repo "docs\images\icon.png"))
    $ms = New-Object IO.MemoryStream
    $bw = New-Object IO.BinaryWriter($ms)
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]1)
    $bw.Write([Byte]0); $bw.Write([Byte]0); $bw.Write([Byte]0); $bw.Write([Byte]0)
    $bw.Write([UInt16]1); $bw.Write([UInt16]32); $bw.Write([UInt32]$png.Length); $bw.Write([UInt32]22)
    $bw.Write($png); $bw.Flush()
    [IO.File]::WriteAllBytes($Ico, $ms.ToArray())
    $Lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Meeting Cue!.lnk"
    $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($Lnk)
    $sc.TargetPath = $AppPyw
    $sc.Arguments = '"' + $Launch + '"'
    $sc.WorkingDirectory = $Repo
    $sc.IconLocation = "$Ico,0"
    $sc.Description = "Meeting Cue!"
    $sc.Save()
    Write-Host "   $Lnk"
}

Write-Host ""
Write-Host "OK: Meeting Cue! is ready. Open it from the Start menu (Meeting Cue!)." -ForegroundColor Green
Write-Host "   helper python : $Py"
Write-Host "   model         : $Model"
Write-Host "   app python    : $AppPy"
