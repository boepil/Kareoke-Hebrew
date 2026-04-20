param(
  [ValidateSet("auto", "cpu", "cu128")]
  [string]$TorchChannel = "auto",
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

function Resolve-TorchChannel {
  param([string]$Requested)
  if ($Requested -ne "auto") {
    return $Requested
  }
  $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if ($null -ne $nvidiaSmi) {
    return "cu128"
  }
  return "cpu"
}

function Invoke-Python {
  param(
    [string]$Executable,
    [string[]]$Arguments
  )
  & $Executable @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed: $Executable $($Arguments -join ' ')"
  }
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"
$AutoRequirementsPath = Join-Path $RepoRoot "requirements-autosync.txt"
$LogsDir = Join-Path $RepoRoot "logs"
$CheckScript = Join-Path $PSScriptRoot "check_autosync.py"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

$ResolvedTorchChannel = Resolve-TorchChannel -Requested $TorchChannel
Write-Host "Installing autosync stack in $RepoRoot"
Write-Host "Torch channel: $ResolvedTorchChannel"

Invoke-Python -Executable $PythonExe -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
Invoke-Python -Executable $PythonExe -Arguments @("-m", "pip", "install", "-r", $RequirementsPath)

if ($ResolvedTorchChannel -eq "cpu") {
  Invoke-Python -Executable $PythonExe -Arguments @(
    "-m", "pip", "install",
    "--index-url", "https://download.pytorch.org/whl/cpu",
    "torch", "torchaudio"
  )
}
else {
  Invoke-Python -Executable $PythonExe -Arguments @(
    "-m", "pip", "install",
    "--index-url", "https://download.pytorch.org/whl/$ResolvedTorchChannel",
    "torch", "torchaudio"
  )
}

Invoke-Python -Executable $PythonExe -Arguments @("-m", "pip", "install", "-r", $AutoRequirementsPath)
Invoke-Python -Executable $PythonExe -Arguments @($CheckScript, "--output", (Join-Path $LogsDir "autosync_env_check.json"))

Write-Host "Autosync setup completed."
