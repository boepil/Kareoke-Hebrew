$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

$buildPython = $env:KARAOKE_BUILD_PYTHON
if ([string]::IsNullOrWhiteSpace($buildPython)) {
    $projectPython = Join-Path $ProjectRoot ".venv-core-build\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython) {
        $buildPython = $projectPython
    } else {
        $buildPython = "python"
    }
}
if (-not (Test-Path -LiteralPath $buildPython) -and $buildPython -eq "python") {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python was not found. Install Python 3.11 or set KARAOKE_BUILD_PYTHON."
    }
}
& $buildPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed for $buildPython. Run: $buildPython -m pip install -r packaging/requirements-core-build.txt"
}

$ffmpeg = $env:KARAOKE_FFMPEG_PATH
if ([string]::IsNullOrWhiteSpace($ffmpeg)) {
    $ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ffmpegCommand) {
        $ffmpeg = $ffmpegCommand.Source
    }
}
if ([string]::IsNullOrWhiteSpace($ffmpeg) -or -not (Test-Path -LiteralPath $ffmpeg)) {
    throw "FFmpeg was not found. Set KARAOKE_FFMPEG_PATH to ffmpeg.exe or add FFmpeg to PATH."
}
$env:KARAOKE_FFMPEG_PATH = (Resolve-Path -LiteralPath $ffmpeg).Path

& $buildPython -m PyInstaller --clean --noconfirm packaging/core.spec

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    iscc packaging/core_installer.iss
} else {
    Write-Host "PyInstaller build completed. Install Inno Setup to produce the setup.exe installer."
}
