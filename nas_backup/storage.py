"""JSON persistence layer for NAS backup plugin config + last run."""
from __future__ import annotations

import logging
import threading
from typing import Any

from services.data_manager import data_manager

logger = logging.getLogger(__name__)

FILE_NAS_BACKUP = 'nas_backup.json'

DEFAULT_CONFIG: dict[str, Any] = {
    # Empty = idle ("Not set up") until a destination is saved.
    # UNC share (\\NAS\HueMixBackup), mapped drive, or local folder.
    'nas_path': '',
    'username': '',
    # Plaintext in data/nas_backup.json (itself backed up). Use a
    # dedicated low-privilege NAS account.
    'password': '',
    'backup_time': '02:00',
    'retention_count': 30,
}


def _default_state() -> dict[str, Any]:
    return {
        'config': dict(DEFAULT_CONFIG),
        'last_run': None,
        # Separate from run state so manual runs never cancel the daily run.
        'schedule': {'last_success_day': None, 'last_attempt': None},
    }


class NasBackupStorage:
    """Read/write NAS backup config and last-run state from a JSON data file."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ensure_file()

    def _ensure_file(self) -> None:
        try:
            fp = data_manager._get_filepath(FILE_NAS_BACKUP)
            if not fp.exists():
                data_manager.write_json(FILE_NAS_BACKUP, _default_state())
                logger.info(f"Created missing data file: {fp}")
        except Exception as e:
            logger.error(f"Failed to ensure data file {FILE_NAS_BACKUP}: {e}")

    def _load_state(self) -> dict[str, Any]:
        data = data_manager.read_json(FILE_NAS_BACKUP, default=_default_state())
        if not isinstance(data, dict):
            return _default_state()
        config = data.get('config')
        if not isinstance(config, dict):
            config = {}
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})
        schedule = data.get('schedule')
        if not isinstance(schedule, dict):
            schedule = {'last_success_day': None, 'last_attempt': None}
        return {
            'config': merged,
            'last_run': data.get('last_run'),
            'schedule': {
                'last_success_day': schedule.get('last_success_day'),
                'last_attempt': schedule.get('last_attempt'),
            },
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        data_manager.write_json(FILE_NAS_BACKUP, state)

    def get_config(self, include_secret: bool = False) -> dict[str, Any]:
        with self._lock:
            config = dict(self._load_state()['config'])
        if not include_secret:
            config = dict(config)
            config['password'] = '***' if config.get('password') else ''
        return config

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist partial config updates."""
        with self._lock:
            state = self._load_state()
            config = state['config']

            if 'nas_path' in updates:
                config['nas_path'] = str(updates.get('nas_path') or '').strip()

            if 'username' in updates:
                config['username'] = str(updates.get('username') or '').strip()

            # '__CLEAR__' wipes; blank keeps the existing password.
            if 'password' in updates:
                pw = updates.get('password')
                if pw == '__CLEAR__':
                    config['password'] = ''
                elif isinstance(pw, str) and pw and pw != '***':
                    config['password'] = pw

            if 'backup_time' in updates:
                from datetime import datetime as _dt
                candidate = str(updates.get('backup_time') or '').strip()
                try:
                    _dt.strptime(candidate, '%H:%M')
                except ValueError:
                    raise ValueError('backup_time must be in HH:MM format')
                config['backup_time'] = candidate

            if 'retention_count' in updates:
                try:
                    keep = int(updates.get('retention_count'))
                except (TypeError, ValueError):
                    raise ValueError('retention_count must be a number')
                if keep < 1 or keep > 365:
                    raise ValueError('retention_count must be between 1 and 365')
                config['retention_count'] = keep

            if 'scheduler_enabled' in updates:
                config['scheduler_enabled'] = bool(updates.get('scheduler_enabled'))

            if 'verify_after_copy' in updates:
                config['verify_after_copy'] = bool(updates.get('verify_after_copy'))

            state['config'] = config
            self._save_state(state)
            return self.get_config()

    def is_configured(self) -> bool:
        with self._lock:
            return bool(self._load_state()['config'].get('nas_path'))

    def record_run(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Remember the latest run (drives the status cards)."""
        with self._lock:
            state = self._load_state()
            state['last_run'] = entry
            self._save_state(state)
            return entry

    def get_last_run(self) -> dict[str, Any] | None:
        with self._lock:
            last = self._load_state().get('last_run')
        return last if isinstance(last, dict) else None

    def get_schedule_state(self) -> dict[str, Any]:
        with self._lock:
            sched = self._load_state().get('schedule') or {}
        return {
            'last_success_day': sched.get('last_success_day'),
            'last_attempt': sched.get('last_attempt'),
        }

    def note_scheduled_attempt(self, success: bool, started_at: str) -> None:
        """Record a scheduled run without affecting manual runs."""
        from datetime import datetime as _dt
        with self._lock:
            state = self._load_state()
            sched = state.get('schedule') or {}
            sched['last_attempt'] = _dt.now().isoformat()
            if success:
                try:
                    day = _dt.fromisoformat(started_at).date().isoformat()
                except (TypeError, ValueError):
                    day = _dt.now().date().isoformat()
                sched['last_success_day'] = day
            state['schedule'] = sched
            self._save_state(state)
