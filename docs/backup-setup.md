# Home Assistant → Google Drive backup (Docker on Windows)

This backs up your **entire HA config folder** — `configuration.yaml`, dashboards,
and `home-assistant_v2.db` (where ALL your statistics live: live sensors + the
20-year imported archive) — to Google Drive, on a schedule.

## 1. Install rclone
- Download rclone for Windows: https://rclone.org/downloads/  (the Windows .zip)
- Unzip it and copy `rclone.exe` somewhere on your PATH, e.g. `C:\Windows\System32\`
  (or note its full path and use that in the script).

## 2. Connect rclone to Google Drive (one time)
Open PowerShell and run:

    rclone config

Then:
- `n` (new remote)
- name: **gdrive**
- storage: choose **Google Drive** (type its number, currently "drive")
- client_id / secret: press Enter to leave blank (uses rclone's defaults)
- scope: choose **1** (full access) or **drive.file**
- leave advanced config "no"; for "Use web browser to authenticate?" choose **Yes**
  — a browser opens, sign in to your Google account, allow access.
- "Configure this as a team drive?" → **No**
- confirm `y`, then `q` to quit.

Test it:

    rclone mkdir gdrive:HA-Backups
    rclone lsd gdrive:

## 3. Point the script at your setup
Open `ha_backup_to_gdrive.ps1` and set the variables at the top:
- `$ContainerName` — run `docker ps` to confirm (likely `homeassistant`).
- `$ConfigDir`     — the HA-files folder that contains `configuration.yaml`.
                     (Check your docker-compose volumes line, e.g. `- ./config:/config`.)
- `$RcloneRemote`  — leave as `gdrive:HA-Backups` if you used the name above.
- `$KeepDays`      — how long to keep old backups (default 30).

Run it once manually to confirm it works:

    powershell -ExecutionPolicy Bypass -File "C:\path\to\ha_backup_to_gdrive.ps1"

You should see HA stop, a zip get made, HA start, and the file upload. Check
Google Drive → HA-Backups for the .zip, and `HA-Backups\backup.log` for the log.

## 4. Schedule it nightly (Task Scheduler)
- Open **Task Scheduler** → Create Task.
- General: name "HA Google Drive Backup"; check **Run whether user is logged on or not**.
- Triggers: New → Daily → 3:00 AM.
- Actions: New → Program/script: `powershell.exe`
  Arguments:
      -ExecutionPolicy Bypass -File "C:\path\to\ha_backup_to_gdrive.ps1"
- Settings: allow it to run on demand; "Stop if runs longer than 1 hour".
- OK.

## Notes
- HA is down only for the few seconds it takes to zip the config — scheduled at 3 AM
  so it won't bother you. This guarantees a clean, non-corrupt database copy.
- To **restore**: stop HA, replace the config folder contents with the unzipped
  backup, start HA.
- 30 daily zips of a weather DB are small; adjust `$KeepDays` if you want more/less.
