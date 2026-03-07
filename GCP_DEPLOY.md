# Deploy on Google Cloud VM (Compute Engine)

This project is a long-running Discord bot, so a VM + systemd service is the most reliable GCP setup.

## 1) Prerequisites

- Install Google Cloud CLI (`gcloud`) on your machine.
- Authenticate:

```powershell
gcloud auth login
gcloud auth application-default login
```

- Ensure Compute Engine API is enabled in your project.
- Create a real `.env` file in the project root (same folder as `lena_fiscal_bot_inatividade.py`).

## 2) Run deployment script

From project root (`c:\Projects\AllyBot`):

```powershell
.\scripts\deploy_gce.ps1 `
  -ProjectId "YOUR_GCP_PROJECT_ID" `
  -Zone "us-central1-a" `
  -Instance "allybot-vm" `
  -CreateInstanceIfMissing
```

Optional params:

- `-RemoteDir "~/allybot"`
- `-ServiceName "allybot"`
- `-PythonFile "lena_fiscal_bot_inatividade.py"`

## 3) Check service

```powershell
gcloud compute ssh allybot-vm --zone us-central1-a --command "sudo systemctl status allybot"
gcloud compute ssh allybot-vm --zone us-central1-a --command "sudo journalctl -u allybot -f"
```

## 4) Update deployment after code changes

Re-run the same deploy command. It uploads files and restarts the service.

## Notes

- Required files uploaded by script:
  - `lena_fiscal_bot_inatividade.py`
  - `requirements_inatividade.txt`
  - `.env` (if present)
- If `.env` is missing, service will fail until you upload one.
