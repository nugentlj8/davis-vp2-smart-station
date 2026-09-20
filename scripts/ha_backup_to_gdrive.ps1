# =====================================================================
#  Home Assistant -> Google Drive backup  (Windows / Docker host)
# =====================================================================
#  Stops the HA container briefly (so the database copies cleanly),
#  zips the config folder, restarts HA, uploads the zip to Google Drive
#  with rclone, and prunes old backups (local + remote).
#
#  ONE-TIME SETUP:  see docs/backup-setup.md
# =====================================================================

# ----------------------- EDIT THESE -----------------------
$ContainerName = "homeassistant"                              # your HA container name (docker ps)
$ConfigDir     = "C:\ServerTools\HomeAssistant\config"        # the HA-files folder (has configuration.yaml)
$LocalBackups  = "C:\ServerTools\HA-Backups"                  # where zips are staged locally
$RcloneRemote  = "gdrive:HA-Backups"                          # rclone remote:folder (set up in rclone config)
$KeepDays      = 30                                           # delete backups older than this (local + remote)
# ----------------------------------------------------------

$ErrorActionPreference = "Stop"
$ts   = Get-Date -Format "yyyy-MM-dd_HHmm"
$zip  = Join-Path $LocalBackups "ha-backup-$ts.zip"
$log  = Join-Path $LocalBackups "backup.log"
function Log($m) { "$((Get-Date).ToString('s'))  $m" | Tee-Object -FilePath $log -Append }

New-Item -ItemType Directory -Force -Path $LocalBackups | Out-Null
Log "=== Backup start ==="

try {
    Log "Stopping $ContainerName ..."
    docker stop $ContainerName | Out-Null

    Log "Zipping $ConfigDir -> $zip"
    Compress-Archive -Path (Join-Path $ConfigDir '*') -DestinationPath $zip -CompressionLevel Optimal -Force
}
finally {
    Log "Starting $ContainerName ..."
    docker start $ContainerName | Out-Null   # always restart HA, even if zip failed
}

Log "Uploading to $RcloneRemote ..."
rclone copy $zip $RcloneRemote --progress
if ($LASTEXITCODE -ne 0) { Log "ERROR: rclone upload failed (exit $LASTEXITCODE)"; exit 1 }

Log "Pruning backups older than $KeepDays days ..."
# remote
rclone delete $RcloneRemote --min-age "${KeepDays}d" 2>&1 | Out-Null
# local
Get-ChildItem $LocalBackups -Filter "ha-backup-*.zip" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force

Log "=== Backup done: $zip ==="
