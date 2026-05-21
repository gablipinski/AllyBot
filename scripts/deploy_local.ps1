param(
  [switch]$SkipInstall,
  [switch]$NoRun,
  [switch]$ForceRecreateVenv
)

$ErrorActionPreference = "Stop"

# In PowerShell 7+, avoid converting native stderr output into terminating errors.
# We handle native command failures via $LASTEXITCODE checks in this script.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}

function Resolve-HostPython {
  $pyCmd = Get-Command "py" -ErrorAction SilentlyContinue
  if ($pyCmd) {
    foreach ($candidate in @("-3.12", "-3.11", "-3.10", "-3")) {
      & $pyCmd.Source $candidate -c "import sys; print(sys.version_info[:2])" 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) {
        return ,@($pyCmd.Source, $candidate)
      }
    }
  }

  $pythonCmd = Get-Command "python" -ErrorAction SilentlyContinue
  if ($pythonCmd) {
    return ,@($pythonCmd.Source)
  }

  throw "Python not found. Install Python 3 or add it to PATH."
}

function Test-DependenciesInstalled {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
  )

  $outFile = [System.IO.Path]::GetTempFileName()
  $errFile = [System.IO.Path]::GetTempFileName()

  $probe = Start-Process -FilePath $PythonExe -ArgumentList @("-c", "import discord, dotenv, aiosqlite") -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
  $ok = ($probe.ExitCode -eq 0)

  Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
  return $ok
}

function Ensure-PipAvailable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
  )

  $outFile = [System.IO.Path]::GetTempFileName()
  $errFile = [System.IO.Path]::GetTempFileName()

  $pipProbe = Start-Process -FilePath $PythonExe -ArgumentList @("-m", "pip", "--version") -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
  if ($pipProbe.ExitCode -eq 0) {
    Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
    return
  }

  Write-Host "pip not found in venv. Bootstrapping pip with ensurepip..."

  $ensurePip = Start-Process -FilePath $PythonExe -ArgumentList @("-m", "ensurepip", "--upgrade") -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
  if ($ensurePip.ExitCode -ne 0) {
    $errText = ""
    try {
      $errText = (Get-Content -Path $errFile -Raw -ErrorAction SilentlyContinue).Trim()
    } catch {
      $errText = ""
    }

    Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue

    if ($errText) {
      throw "Could not bootstrap pip in venv. ensurepip stderr: $errText"
    }

    throw "Could not bootstrap pip in venv. Try reinstalling Python or use Python 3.12/3.11, then run .\\scripts\\deploy_local.ps1 -ForceRecreateVenv"
  }

  Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
}

function Get-PythonMajorMinor {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
  )

  $versionOut = (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($versionOut)) {
    throw "Could not determine Python version from $PythonExe"
  }

  return $versionOut.Trim()
}

$root = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $root ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $root "requirements_inatividade.txt"
$botPath = Join-Path $root "main_bot.py"
$envPath = Join-Path $root ".env"
$envExamplePath = Join-Path $root "env_inatividade.example"

if (-not (Test-Path $botPath)) {
  throw "Bot file not found: $botPath"
}
if (-not (Test-Path $requirementsPath)) {
  throw "Requirements file not found: $requirementsPath"
}

if ($ForceRecreateVenv -and (Test-Path $venvPath)) {
  Write-Host "Removing existing virtual environment at $venvPath"
  Remove-Item -Recurse -Force $venvPath
}

if (-not (Test-Path $venvPython)) {
  Write-Host "Creating local virtual environment (.venv)..."
  $hostPython = @(Resolve-HostPython)
  $hostPythonExe = $hostPython[0]

  if ($hostPython.Count -gt 1) {
    $launcherArgs = $hostPython[1..($hostPython.Count - 1)]
    & $hostPythonExe @launcherArgs -m venv --without-pip $venvPath
  } else {
    & $hostPythonExe -m venv --without-pip $venvPath
  }

  if (-not (Test-Path $venvPython)) {
    throw "Failed to create virtual environment at $venvPath"
  }
}

$pythonMajorMinor = Get-PythonMajorMinor -PythonExe $venvPython
Write-Host "Using venv Python $pythonMajorMinor"
if ([version]$pythonMajorMinor -ge [version]"3.14") {
  Write-Warning "Python $pythonMajorMinor in .venv is not recommended for discord.py in this project."
  Write-Host "Recommended: install Python 3.12/3.11 and run: .\scripts\deploy_local.ps1 -ForceRecreateVenv"
  Write-Host "Continuing anyway with current interpreter..."
}

if (-not $SkipInstall) {
  Ensure-PipAvailable -PythonExe $venvPython

  if (Test-DependenciesInstalled -PythonExe $venvPython) {
    Write-Host "Dependencies already installed. Skipping pip install."
  } else {
    Write-Host "Installing dependencies from requirements_inatividade.txt..."
    try {
      & $venvPython -m pip install --disable-pip-version-check --upgrade pip
      if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed with exit code $LASTEXITCODE"
      }

      & $venvPython -m pip install --disable-pip-version-check -r $requirementsPath
      if ($LASTEXITCODE -ne 0) {
        throw "pip install failed with exit code $LASTEXITCODE"
      }
    } catch {
      Write-Error "Dependency installation interrupted or failed. Re-run with -SkipInstall if already installed, or run again to retry installation."
      throw
    }
  }
}

if (-not (Test-Path $envPath)) {
  if (Test-Path $envExamplePath) {
    Copy-Item $envExamplePath $envPath
    Write-Warning "Created .env from env_inatividade.example. Update .env with real values before running the bot."
  } else {
    throw ".env not found and env_inatividade.example is missing."
  }
}

if ($NoRun) {
  Write-Host "Setup complete. NoRun was provided, so the bot was not started."
  exit 0
}

$env:PYTHONUNBUFFERED = "1"
Write-Host "Starting bot locally..."
& $venvPython $botPath

if ($LASTEXITCODE -ne 0) {
  Write-Warning "Bot exited with code $LASTEXITCODE."
  Write-Host "If you see 'PrivilegedIntentsRequired', either:"
  Write-Host "  1) Enable Server Members Intent and Message Content Intent in Discord Developer Portal, or"
  Write-Host "  2) Set ENABLE_MESSAGE_CONTENT_INTENT=false and ENABLE_MEMBERS_INTENT=false in .env"
  exit $LASTEXITCODE
}
