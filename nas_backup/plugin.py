"""NAS backup plugin for HueMix-Link: daily off-box backups of the data
directory to an external NAS, with restore-from-NAS."""
from __future__ import annotations

import logging
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from .scheduler import DailyScheduler
from .smb_store import open_backup_store, use_smb_backend
from .storage import NasBackupStorage

logger = logging.getLogger(__name__)


@dataclass
class NasBackupPlugin:
    """Daily off-box backups of the HueMix-Link data directory."""

    options: dict[str, Any] = field(default_factory=dict)
    context: Any = None
    manifest: Any = None

    def __post_init__(self) -> None:
        self.blueprint = Blueprint(
            'nas_backup',
            __name__,
            url_prefix='/plugins/nas_backup',
            template_folder='templates',
            static_folder='static',
        )
        self.blueprint.add_url_rule('/', 'index', self.index)
        self.blueprint.add_url_rule('/status', 'status', self.api_status)
        self.blueprint.add_url_rule('/api/config', 'api_get_config', self.api_get_config)
        self.blueprint.add_url_rule(
            '/api/config', 'api_save_config', self.api_save_config, methods=['POST']
        )
        self.blueprint.add_url_rule(
            '/api/test', 'api_test', self.api_test, methods=['POST']
        )
        self.blueprint.add_url_rule(
            '/api/backup-now', 'api_backup_now', self.api_backup_now, methods=['POST']
        )
        self.blueprint.add_url_rule('/api/nas-list', 'api_nas_list', self.api_nas_list)
        self.blueprint.add_url_rule(
            '/api/restore', 'api_restore', self.api_restore, methods=['POST']
        )

        self._storage: NasBackupStorage | None = None
        self._scheduler: DailyScheduler | None = None
        self._run_lock = threading.Lock()
        self._running = False

    def register(self, app, context=None):
        self.context = context or self.context
        app.register_blueprint(self.blueprint)

    def start(self, context=None):
        self.context = context or self.context
        self._storage = NasBackupStorage()
        self._scheduler = DailyScheduler(
            run_backup=self._run_backup_job,
            get_backup_time=lambda: self._storage.get_config(True).get('backup_time', '02:00'),
            is_enabled=lambda: self._is_active(),
            should_run_today=self._should_run_today,
        )
        self._scheduler.start()
        self._log('NAS backup plugin started.')

    def stop(self, context=None):
        if self._scheduler:
            try:
                self._scheduler.stop()
            except Exception:
                pass
            self._scheduler = None
        self._log('NAS backup plugin stopped.')

    def index(self):
        return render_template('nas_backup.html')

    def api_status(self):
        storage = self._require_storage()
        config = storage.get_config()
        configured = storage.is_configured()
        last_run = storage.get_last_run()
        nas_files: list[dict[str, Any]] = []
        nas_error: str | None = None
        if configured:
            try:
                dest = self._backup_dir()
                nas_files = dest.list_backups()[:5]
            except Exception as exc:
                nas_error = self._short_error(exc)
        return jsonify({
            'config': config,
            'configured': configured,
            'schedule_active': configured and self._scheduler is not None,
            'next_run': self._scheduler.next_run_iso() if self._scheduler else None,
            'running': self._running,
            'last_run': last_run,
            'nas_count': len(nas_files),
            'nas_latest': nas_files[:5],
            'nas_error': nas_error,
        })

    def api_get_config(self):
        return jsonify({'config': self._require_storage().get_config()})

    def api_save_config(self):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'success': False, 'error': 'Invalid payload'}), 400
        try:
            config = self._require_storage().update_config(payload)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500
        # Rejected destinations still save, with a warning. Skipped when
        # clearing the path back to unconfigured.
        try:
            full = self._require_storage().get_config(include_secret=True)
            if full.get('nas_path'):
                self._backup_dir(probe=True)
                user_set = bool(full.get('username'))
                pass_set = bool(full.get('password'))
                if (user_set or pass_set) and not use_smb_backend(full.get('nas_path') or ''):
                    return jsonify({
                        'success': True,
                        'config': config,
                        'warning': 'Username and password only apply to smb:// paths — ignored here.',
                    })
                if user_set and not pass_set:
                    return jsonify({
                        'success': True,
                        'config': config,
                        'warning': 'Username needs a password.',
                    })
        except Exception as exc:
            return jsonify({
                'success': True,
                'config': config,
                'warning': f"Saved, but NAS is not reachable: {self._short_error(exc)}",
            })
        self._log(f"NAS backup config saved (path={config.get('nas_path')})")
        return jsonify({'success': True, 'config': config})

    def api_test(self):
        storage = self._require_storage()
        if not storage.is_configured():
            return jsonify({'success': False, 'error': 'Set a NAS destination first.'}), 400
        try:
            dest = self._backup_dir(probe=True)
            existing = dest.list_backups()
            return jsonify({
                'success': True,
                'message': f"NAS reachable at {dest.display} ({len(existing)} backup(s) found).",
                'path': dest.display,
                'count': len(existing),
            })
        except Exception as exc:
            return jsonify({'success': False, 'error': self._short_error(exc)}), 400

    def api_backup_now(self):
        if not self._require_storage().is_configured():
            return jsonify({'success': False, 'error': 'Set a NAS destination first.'}), 400
        if self._running:
            return jsonify({'success': False, 'error': 'A backup is already running'}), 409
        threading.Thread(
            target=self._run_backup_job, args=('manual',), daemon=True,
            name='NasBackupManual',
        ).start()
        return jsonify({'success': True, 'message': 'Backup started in background.'})

    def api_nas_list(self):
        storage = self._require_storage()
        if not storage.is_configured():
            return jsonify({'success': True, 'configured': False, 'path': '', 'backups': []})
        try:
            dest = self._backup_dir()
            return jsonify({'success': True, 'path': dest.display, 'backups': dest.list_backups()})
        except Exception as exc:
            return jsonify({'success': False, 'error': self._short_error(exc)}), 400

    def api_restore(self):
        """Restore an archive from the NAS via BackupManager."""
        payload = request.get_json(silent=True) or {}
        name = str(payload.get('name') or '').strip()
        if not name or '..' in name or '/' in name or '\\' in name:
            return jsonify({'success': False, 'error': 'Valid backup file name is required'}), 400
        storage = self._require_storage()
        if not storage.is_configured():
            return jsonify({'success': False, 'error': 'Set a NAS destination first.'}), 400
        try:
            dest = self._backup_dir()
            tmp = Path(tempfile.gettempdir()) / f"huemix_restore_{name}"
            try:
                dest.fetch(name, tmp)
            except FileNotFoundError:
                return jsonify({'success': False, 'error': 'Backup not found on NAS'}), 404
            from services.backup_manager import backup_manager
            backup_manager.restore_backup(str(tmp))
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception as exc:
            self._log(f"NAS restore failed for {name}: {exc}")
            return jsonify({'success': False, 'error': self._short_error(exc)}), 500
        self._log(f"Restored backup {name} from NAS.")
        return jsonify({'success': True, 'message': f"Restored {name}."})

    def _require_storage(self) -> NasBackupStorage:
        if self._storage is None:
            self._storage = NasBackupStorage()
        return self._storage

    def _is_configured(self) -> bool:
        try:
            return self._require_storage().is_configured()
        except Exception:
            return False

    def _backup_dir(self, probe: bool = False):
        """Backup store for this install: native FS or direct SMB."""
        from services.home_id_manager import home_id_manager
        config = self._require_storage().get_config(include_secret=True)
        return open_backup_store(
            config.get('nas_path') or '',
            config.get('username', ''),
            config.get('password', ''),
            home_id_manager.get_or_create_home_id(),
            probe=probe,
        )

    def _is_active(self) -> bool:
        return self._is_configured()

    @staticmethod
    def _short_error(exc: Exception, limit: int = 140) -> str:
        """First line of the error, without WinError noise, capped."""
        import re
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        msg = re.sub(r'\[WinError \d+\]\s*', '', msg)
        if len(msg) > limit:
            msg = msg[:limit - 1].rstrip() + '…'
        return msg

    def _should_run_today(self) -> bool:
        """True unless a scheduled run already succeeded today. Failed
        scheduled attempts retry after a 15-minute cooldown."""
        try:
            state = self._require_storage().get_schedule_state()
        except Exception:
            return True
        today = datetime.now().date().isoformat()
        if state.get('last_success_day') == today:
            return False
        last_attempt = state.get('last_attempt')
        if last_attempt:
            try:
                ago = (datetime.now() - datetime.fromisoformat(last_attempt)).total_seconds()
                if ago < 15 * 60:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    def _run_backup_job(self, trigger: str) -> None:
        if not self._is_configured():
            return
        if not self._run_lock.acquire(blocking=False):
            self._log('Backup job skipped: another run in progress.')
            return
        self._running = True
        started = datetime.now()
        entry: dict[str, Any] = {
            'started_at': started.isoformat(),
            'trigger': trigger,
            'status': 'failed',
        }
        tmp_path: Path | None = None
        try:
            storage = self._require_storage()
            dest = self._backup_dir(probe=True)
            from services.backup_manager import backup_manager
            tmp_path = Path(tempfile.gettempdir()) / (
                f"huemix_nas_{started.strftime('%Y%m%d_%H%M%S')}.tar.gz"
            )
            local_path = backup_manager.create_backup(out_path=str(tmp_path))
            local_size = Path(local_path).stat().st_size
            dest.check_space(local_size)
            copied = dest.store_file(local_path)
            retention = int(storage.get_config().get('retention_count', 30))
            prune = dest.prune(retention)
            duration = (datetime.now() - started).total_seconds()
            entry.update({
                'status': 'success',
                'file': copied['name'],
                'size': copied['size'],
                'local_size': local_size,
                'destination': dest.display,
                'duration_seconds': round(duration, 1),
                'pruned': prune.get('deleted', []),
                'finished_at': datetime.now().isoformat(),
            })
            self._log(
                f"NAS backup ({trigger}) complete: {copied['name']} "
                f"({copied['size']} bytes) -> {dest}"
            )
            # Temp file is always removed; nothing accumulates locally.
            try:
                Path(local_path).unlink(missing_ok=True)
                tmp_path = None
            except Exception as exc:
                self._log(f"Could not remove temp backup {local_path}: {exc}")
        except Exception as exc:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            entry.update({
                'status': 'failed',
                'error': self._short_error(exc),
                'finished_at': datetime.now().isoformat(),
                'duration_seconds': round((datetime.now() - started).total_seconds(), 1),
            })
            self._log(f"NAS backup ({trigger}) failed: {exc}")
        finally:
            try:
                self._require_storage().record_run(entry)
            except Exception as exc:
                self._log(f"Failed to record last run: {exc}")
            if trigger == 'scheduled':
                try:
                    self._require_storage().note_scheduled_attempt(
                        entry.get('status') == 'success', entry.get('started_at') or ''
                    )
                except Exception as exc:
                    self._log(f"Failed to record schedule state: {exc}")
            self._running = False
            self._run_lock.release()

    def _log(self, message: str) -> None:
        log = getattr(self.context, 'logger', None) if self.context is not None else None
        if log is None:
            log = logging.getLogger('plugins.nas_backup')
        try:
            log.info(message)
        except Exception:
            pass


def create_plugin(options=None, context=None, manifest=None, host=None, **_kwargs):
    return NasBackupPlugin(options=options or {}, context=context or host, manifest=manifest)
