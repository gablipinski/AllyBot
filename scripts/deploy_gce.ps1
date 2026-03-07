param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectId,

  [Parameter(Mandatory = $true)]
  [string]$Zone,

  [Parameter(Mandatory = $true)]
  [string]$Instance,

  [string]$RemoteDir = "~/allybot",
  [string]$ServiceName = "allybot",
  [string]$PythonFile = "main_bot.py",
  [switch]$CreateInstanceIfMissing,
  [string]$MachineType = "e2-micro",
  [string]$ImageFamily = "debian-12",
  [string]$ImageProject = "debian-cloud"
)

$ErrorActionPreference = "Stop"

function Resolve-GCloudCommand {
  $cmd = Get-Command "gcloud" -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }

  $candidates = @(
    "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    "$env:ProgramFiles\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
  )

  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }

  throw "Required command not found: gcloud. Install Google Cloud SDK or add gcloud to PATH."
}

$GCloudCmd = Resolve-GCloudCommand

$root = Split-Path -Parent $PSScriptRoot
$botFilePath = Join-Path $root $PythonFile
$requirementsPath = Join-Path $root "requirements_inatividade.txt"
$envPath = Join-Path $root ".env"
$bootstrapPath = Join-Path $PSScriptRoot "bootstrap_gce.sh"

if (-not (Test-Path $botFilePath)) {
  throw "Bot file not found: $botFilePath"
}
if (-not (Test-Path $requirementsPath)) {
  throw "Requirements file not found: $requirementsPath"
}
if (-not (Test-Path $bootstrapPath)) {
  throw "Bootstrap script not found: $bootstrapPath"
}

Write-Host "Setting gcloud project to $ProjectId"
& $GCloudCmd config set project $ProjectId | Out-Null

$instanceExists = $true
try {
  & $GCloudCmd compute instances describe $Instance --zone $Zone | Out-Null
} catch {
  $instanceExists = $false
}

if (-not $instanceExists) {
  if ($CreateInstanceIfMissing) {
    Write-Host "Creating instance $Instance in $Zone"
    & $GCloudCmd compute instances create $Instance `
      --zone $Zone `
      --machine-type $MachineType `
      --image-family $ImageFamily `
      --image-project $ImageProject
  } else {
    throw "Instance '$Instance' not found in zone '$Zone'. Use -CreateInstanceIfMissing or create it manually."
  }
}

$instanceStatus = (& $GCloudCmd compute instances describe $Instance --zone $Zone --format="value(status)").Trim()
if ($instanceStatus -eq "TERMINATED") {
  Write-Host "Instance is TERMINATED. Starting $Instance..."
  & $GCloudCmd compute instances start $Instance --zone $Zone | Out-Null
}

# Wait until the VM is RUNNING before SSH/SCP to avoid "resource is not ready" failures.
$maxAttempts = 30
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
  $instanceStatus = (& $GCloudCmd compute instances describe $Instance --zone $Zone --format="value(status)").Trim()
  if ($instanceStatus -eq "RUNNING") {
    break
  }

  Write-Host "Waiting for instance to be RUNNING (current: $instanceStatus, attempt $attempt/$maxAttempts)..."
  Start-Sleep -Seconds 5
}

if ($instanceStatus -ne "RUNNING") {
  throw "Instance '$Instance' is not ready (status: $instanceStatus). Try again after it finishes starting."
}

# Resolve remote home and convert ~/ paths to absolute paths for consistent SSH/SCP behavior.
# Use single-quoted command strings so PowerShell does not expand $HOME locally.
$remoteHome = (& $GCloudCmd compute ssh $Instance --zone $Zone --command 'printf %s "$HOME"').Trim()
if ([string]::IsNullOrWhiteSpace($remoteHome) -or -not $remoteHome.StartsWith('/')) {
  $remoteHome = (& $GCloudCmd compute ssh $Instance --zone $Zone --command 'getent passwd "$(id -un)" | cut -d: -f6').Trim()
}
if ([string]::IsNullOrWhiteSpace($remoteHome) -or -not $remoteHome.StartsWith('/')) {
  throw "Could not resolve a valid remote HOME directory for instance '$Instance'. Got: '$remoteHome'"
}

$effectiveRemoteDir = $RemoteDir
if ($RemoteDir.StartsWith("~/")) {
  $effectiveRemoteDir = "$remoteHome/$($RemoteDir.Substring(2))"
}

Write-Host "Preparing remote directory $effectiveRemoteDir"
& $GCloudCmd compute ssh $Instance --zone $Zone --command "mkdir -p '$effectiveRemoteDir'"

$uploadFiles = @($botFilePath, $requirementsPath)
if (Test-Path $envPath) {
  $uploadFiles += $envPath
} else {
  Write-Warning ".env not found at $envPath. Service will start but fail until .env is uploaded."
}

Write-Host "Uploading bot files"
foreach ($file in $uploadFiles) {
  Write-Host "  -> Uploading $(Split-Path -Leaf $file)"
  & $GCloudCmd compute scp --zone $Zone $file "$($Instance):$effectiveRemoteDir"
}

Write-Host "Uploading bootstrap script"
& $GCloudCmd compute scp --zone $Zone $bootstrapPath "$($Instance):/tmp/bootstrap_gce.sh"

$remoteCmd = "chmod +x /tmp/bootstrap_gce.sh; sudo /tmp/bootstrap_gce.sh --app-dir '$effectiveRemoteDir' --service '$ServiceName' --python-file '$PythonFile'"
Write-Host "Running remote bootstrap"
& $GCloudCmd compute ssh $Instance --zone $Zone --command $remoteCmd

Write-Host "Done. Useful commands:"
Write-Host "  gcloud compute ssh $Instance --zone $Zone --command 'sudo systemctl status $ServiceName'"
Write-Host "  gcloud compute ssh $Instance --zone $Zone --command 'sudo journalctl -u $ServiceName -f'"
