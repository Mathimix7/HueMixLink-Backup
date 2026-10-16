"""Background daily scheduler for NAS backups."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)


class DailyScheduler(threading.Thread):
    """Fire `run_backup` once per day at the configured HH:MM.

    `should_run_today` gates the run; missed runs (PC was off) fire on the
    next tick after startup.
    """

    def __init__(
        self,
        run_backup: Callable[[str], None],
        get_backup_time: Callable[[], str],
        is_enabled: Callable[[], bool],
        should_run_today: Callable[[], bool],
        interval_seconds: int = 30,
    ) -> None:
        super().__init__(name='NasBackupScheduler', daemon=True)
        self._run_backup = run_backup
        self._get_backup_time = get_backup_time
        self._is_enabled = is_enabled
        self._should_run_today = should_run_today
        self._interval = max(10, int(interval_seconds or 30))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def next_run_iso(self) -> str | None:
        """Next run in local ISO format, or None when disabled."""
        try:
            if not self._is_enabled():
                return None
            hh, mm = self._get_backup_time().split(':')
            now = datetime.now()
            candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if candidate <= now and not self._should_run_today():
                # Already ran today -> tomorrow.
                candidate += timedelta(days=1)
            elif candidate <= now:
                # Overdue today -> run on next tick.
                candidate = now
            return candidate.isoformat()
        except Exception:
            return None

    def run(self) -> None:
        logger.info('NAS backup scheduler started.')
        while not self._stop.wait(self._interval):
            try:
                if not self._is_enabled():
                    continue
                if not self._should_run_today():
                    continue
                hh, mm = self._get_backup_time().split(':')
                now = datetime.now()
                due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if now >= due:
                    self._run_backup('scheduled')
            except Exception as exc:
                logger.warning(f"NAS backup scheduler tick failed: {exc}")
        logger.info('NAS backup scheduler stopped.')
