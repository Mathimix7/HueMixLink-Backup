# HueMixLink NAS Backup plugin

Daily off-box backups of the HueMix-Link data directory to an external NAS.
Same external-repo pattern as `custom_automation`: real code here, exposed
via a junction at `python/plugins/nas_backup` (recreate with):

```powershell
New-Item -ItemType Junction `
  -Path "D:\dev\HueMix-Link\python\plugins\nas_backup" `
  -Target "D:\dev\HueMixLink-Backup\nas_backup"
```

## What it backs up

The exact content of Settings → Backup (`BackupManager.create_backup()`),
copied into `HueMixLink-<home_id>` inside the destination and size-verified.
The archive is built in the system temp dir and always deleted afterwards —
no local copy accumulates. Older root-level archives are moved into the
subfolder on first run.

## Destination

Two kinds, nothing in between:

- `smb://host/share/path` — signs in with the plugin username/password on
  any OS, fresh on every operation. The only way to use explicit credentials.
- Anything else (UNC path, mapped drive, local folder) — plain filesystem,
  no auth handling. On Windows the OS session decides (Explorer access =
  plugin access).

Set the destination on the plugin page (`/plugins/nas_backup`) along with
daily time (default `02:00`) and backups to keep (default 30). Fresh
installs stay idle until a destination is saved. Config lives in
`data/nas_backup.json`.

Direct SMB needs the `smbprotocol` package (`requirements.txt` here — the
plugin installer installs it on install/update).

## Schedule

One run per calendar day. Missed runs (PC was off) fire on next startup;
failed runs retry after 15 minutes; manual backups never block the daily run.

## Retention

Oldest NAS copies beyond the keep count are deleted. Only the latest run is
stored in `data/nas_backup.json` for the status cards.

## Restore

Pick a backup in the Restore modal. Current data is set aside first.

## Security note

The NAS password is plaintext in `data/nas_backup.json` (itself backed up).
Use a dedicated low-privilege NAS account with write access only to the
backup share.
