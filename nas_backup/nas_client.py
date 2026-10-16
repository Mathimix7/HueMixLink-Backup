"""Local and OS-mounted destination handling (plain filesystem)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKUP_FILE_PREFIX = 'huemix_link_backup_'


def resolve_destination(nas_path: str) -> Path:
    """Check the destination without creating files. Raises on failure."""
    clean = (nas_path or '').strip()
    if not clean:
        raise ValueError('nas_path is not configured')
    dest = Path(clean)

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise OSError(f"Cannot create NAS directory {dest}: {exc}") from exc
    if not dest.is_dir():
        raise OSError(f"NAS directory {dest} is not accessible")
    return dest


def _probe_dir(dest: Path) -> Path:
    probe = dest / '.huemix_write_test'
    last_error: Exception | None = None
    for _ in range(3):
        try:
            probe.write_text('ok', encoding='utf-8')
            try:
                probe.unlink(missing_ok=True)
            except Exception as exc:
                logger.debug(f"Leftover NAS probe file {probe}: {exc}")
            return dest
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise OSError(f"NAS directory {dest} is not writable: {last_error}") from last_error


def backup_dir_name(home_id: int | str) -> str:
    """``HueMixLink-<home_id>``. The id is stable (itself backed up)."""
    token = re.sub(r'[^A-Za-z0-9_-]', '', str(home_id or ''))[:16] or 'default'
    return f"HueMixLink-{token}"


def resolve_backup_dir(nas_path: str, home_id: int | str = '') -> Path:
    """Backup folder ``HueMixLink-<home_id>`` inside the destination.

    Created on first use. Archives left in the root by older versions are
    moved in. Listing, pruning, and restore are scoped to this folder.
    """
    base = resolve_destination(nas_path)
    sub = base / backup_dir_name(home_id)
    sub.mkdir(parents=True, exist_ok=True)
    _adopt_legacy_backups(base, sub)
    return sub


def _adopt_legacy_backups(base: Path, sub: Path) -> None:
    """Move root-level archives from older versions into the subfolder."""
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for child in entries:
        if not child.is_file():
            continue
        name = child.name
        if name.startswith(BACKUP_FILE_PREFIX) and (
            name.endswith('.tar.gz') or name.endswith('.tgz')
        ):
            try:
                child.rename(sub / name)
                logger.info(f"Adopted legacy backup {name} into {sub.name}")
            except OSError as exc:
                logger.warning(f"Could not adopt {name}: {exc}")


def check_free_space(dest_dir: str | os.PathLike, needed_bytes: int) -> None:
    """Raise when the NAS cannot fit the archive."""
    try:
        free = shutil.disk_usage(dest_dir).free
    except OSError as exc:
        raise OSError(f"Cannot check free space on {dest_dir}: {exc}") from exc
    if free < needed_bytes:
        raise OSError(
            f"Not enough free space on NAS "
            f"(need {_human(needed_bytes)}, {_human(free)} free)"
        )


def _human(n: int) -> str:
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(max(0, n))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def copy_to_nas(local_file: str | os.PathLike, nas_dir: str | os.PathLike,
                verify: bool = True) -> dict[str, Any]:
    """Copy an archive to the NAS. Returns {name, size}."""
    src = Path(local_file)
    if not src.is_file():
        raise FileNotFoundError(f"Local backup not found: {src}")
    dest_dir = Path(nas_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    if verify:
        src_size = src.stat().st_size
        dest_size = dest.stat().st_size
        if src_size != dest_size:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise OSError(
                f"Verify failed after copy: local {src_size} bytes != NAS {dest_size} bytes"
            )
    return {'name': dest.name, 'path': str(dest), 'size': dest.stat().st_size}


def list_nas_backups(nas_dir: str | os.PathLike) -> list[dict[str, Any]]:
    """Archives on the NAS, newest first."""
    dest_dir = Path(nas_dir)
    if not dest_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        entries = list(dest_dir.iterdir())
    except OSError:
        return []
    for child in entries:
        name = child.name
        if not child.is_file():
            continue
        if not (name.endswith('.tar.gz') or name.endswith('.tgz')):
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        out.append({'name': name, 'size': stat.st_size, 'mtime': stat.st_mtime})
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


def prune_nas_backups(nas_dir: str | os.PathLike, keep: int) -> dict[str, Any]:
    """Delete oldest archives beyond `keep`. Returns {kept, deleted}."""
    backups = list_nas_backups(nas_dir)
    to_delete = backups[keep:] if keep >= 0 else []
    deleted: list[str] = []
    for item in to_delete:
        try:
            (Path(nas_dir) / item['name']).unlink()
            deleted.append(item['name'])
        except OSError as exc:
            logger.warning(f"Failed to prune {item['name']}: {exc}")
    return {'kept': len(backups) - len(deleted), 'deleted': deleted}
