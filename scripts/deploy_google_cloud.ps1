$ErrorActionPreference = "Stop"

# ==============================
# Hardcoded deployment settings
# ==============================
$ProjectId = "high-plating-484520-g4"
$Zone = "us-central1-c"
$Instance = "1750903512185116790"

$RemoteDir = "~/allybot"
$ServiceName = "allybot"
$PythonFile = "main_bot.py"

# Set to $true if you want to auto-create the VM when it does not exist.
$CreateInstanceIfMissing = $true
$MachineType = "e2-micro"
$ImageFamily = "debian-12"
$ImageProject = "debian-cloud"

$deployScript = Join-Path $PSScriptRoot "deploy_gce.ps1"
if (-not (Test-Path $deployScript)) {
  throw "Base deploy script not found: $deployScript"
}

$params = @{
  ProjectId = $ProjectId
  Zone = $Zone
  Instance = $Instance
  RemoteDir = $RemoteDir
  ServiceName = $ServiceName
  PythonFile = $PythonFile
  MachineType = $MachineType
  ImageFamily = $ImageFamily
  ImageProject = $ImageProject
}

if ($CreateInstanceIfMissing) {
  $params.CreateInstanceIfMissing = $true
}

Write-Host "Starting one-click deploy with hardcoded settings..."
& $deployScript @params
