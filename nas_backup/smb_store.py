"""Direct SMB2/3 access so every OS works from the UI with no OS mount.

Only ``smb://`` paths come here, on any OS, authenticated fresh on every
operation with the UI credentials. Everything else (UNC paths, mounts,
local folders) uses the native filesystem with no auth handling: on
Windows the OS session decides, which avoids stale-session surprises.
Needs the ``smbprotocol`` package (repo ``requirements.txt``).
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

# smbprotocol logs every packet at INFO (plus a benign disconnect warning
# per operation). Only real errors should reach the app log.
logging.getLogger('smbprotocol').setLevel(logging.ERROR)

PROBE_NAME = '.huemix_write_test'
_CHUNK = 1024 * 1024


def _value(item, key):
    """Unwrap a response structure field to a plain Python value."""
    val = item[key]
    if hasattr(val, 'get_value'):
        val = val.get_value()
    return val


def parse_server_path(nas_path: str) -> dict[str, Any] | None:
    """Split ``smb://[user[:pass]@]host[:port]/share/path`` into parts.
    Returns None for anything else (native paths are not parsed)."""
    raw = (nas_path or '').strip()
    if not raw.lower().startswith('smb://'):
        return None
    parts = urlsplit(raw)
    host = parts.hostname or ''
    port = parts.port or 445
    user = unquote(parts.username) if parts.username else ''
    password = unquote(parts.password or '') if parts.username else ''
    segments = [s for s in parts.path.strip('/').split('/') if s not in ('', '.', '..')]
    if not host or not segments:
        return None
    return {
        'server': host,
        'port': port,
        'share': segments[0],
        'subpath': '/'.join(segments[1:]),
        'user': user,
        'password': password,
    }


def use_smb_backend(nas_path: str) -> bool:
    """True only for explicit ``smb://`` paths. UNC paths, mounts, and
    local folders always use the native filesystem."""
    return (nas_path or '').strip().lower().startswith('smb://')


def _smb() -> Any:
    try:
        import smbprotocol  # noqa: F401
    except ImportError as exc:
        raise OSError(
            "SMB support needs the 'smbprotocol' package — reinstall the "
            "plugin so its requirements install."
        ) from exc


def _friendly(action: str, display: str, exc: Exception) -> OSError:
    from smbprotocol.exceptions import (
        DiskFull,
        LogonFailure,
        NoSuchFile,
        ObjectNameNotFound,
        PasswordExpired,
        WrongPassword,
    )
    if isinstance(exc, (LogonFailure, WrongPassword, PasswordExpired)):
        return OSError(f"SMB login to {display} failed: check username/password")
    if isinstance(exc, (NoSuchFile, ObjectNameNotFound)):
        return OSError(f"SMB path {display} not found on the NAS")
    if isinstance(exc, DiskFull):
        return OSError(f"No free space left on {display}")
    return OSError(f"SMB {action} failed on {display}: {exc}")


class SmbBackupStore:
    """One NAS backup folder reached over SMB2/3 with UI credentials."""

    def __init__(self, server: str, port: int, share: str, subpath: str,
                 username: str = '', password: str = ''):
        self.server = server
        self.port = port
        self.share = share
        self.subpath = subpath.strip('/').replace('/', '\\')
        self.username = username or ''
        self._password = password or ''

    @property
    def display(self) -> str:
        """Human-readable location. Never contains the password."""
        base = f"\\\\{self.server}\\{self.share}"
        return f"{base}\\{self.subpath}" if self.subpath else base

    def _tree(self):
        _smb()

        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
        from smbprotocol.tree import TreeConnect

        connection = Connection(uuid.uuid4(), self.server, port=self.port)
        connection.connect()
        try:
            # Signing (connection default) stays required; encryption is
            # negotiated — demanding it breaks older NAS firmware and
            # loopback test servers. Same posture as Windows Explorer.
            if self.username:
                session = Session(connection, self.username, self._password,
                                  auth_protocol='ntlm', require_encryption=False)
            else:
                session = Session(connection, require_encryption=False)
            session.connect()
            tree = TreeConnect(session, f"\\\\{self.server}\\{self.share}")
            tree.connect()
        except Exception:
            try:
                connection.disconnect()
            except Exception:
                pass
            raise
        return connection, tree

    def _close(self, connection, tree) -> None:
        for obj in (tree, connection):
            try:
                obj.disconnect()
            except Exception:
                pass

    def _rel(self, *parts: str) -> str:
        rel = '\\'.join([self.subpath, *[p for p in parts if p]]).strip('\\')
        if not rel or '..' in rel.replace('/', '\\').split('\\'):
            raise ValueError(f"Unsafe SMB path: {'/'.join(parts)}")
        return rel

    @staticmethod
    def _safe_name(name: str) -> str:
        clean = (name or '').strip()
        if not clean or clean in ('.', '..') or '/' in clean or '\\' in clean:
            raise ValueError(f"Unsafe backup name: {name!r}")
        return clean

    def _open(self, tree, relpath: str, *, for_dir=False, write=False,
              overwrite=False, delete=False, disposition=None):
        from smbprotocol.open import (
            CreateDisposition,
            CreateOptions,
            DirectoryAccessMask,
            FileAttributes,
            ImpersonationLevel,
            Open,
            ShareAccess,
        )

        access = DirectoryAccessMask.GENERIC_WRITE if (write or delete) else DirectoryAccessMask.GENERIC_READ
        if delete:
            access |= DirectoryAccessMask.DELETE
        attrs = FileAttributes.FILE_ATTRIBUTE_DIRECTORY if for_dir else FileAttributes.FILE_ATTRIBUTE_NORMAL
        options = CreateOptions.FILE_DIRECTORY_FILE if for_dir else CreateOptions.FILE_NON_DIRECTORY_FILE
        if delete:
            options |= CreateOptions.FILE_DELETE_ON_CLOSE
        if disposition is not None:
            disp = disposition
        elif overwrite:
            disp = CreateDisposition.FILE_OVERWRITE_IF
        elif for_dir:
            disp = CreateDisposition.FILE_OPEN_IF
        else:
            disp = CreateDisposition.FILE_OPEN
        handle = Open(tree, relpath)
        handle.create(
            ImpersonationLevel.Impersonation,
            access,
            attrs,
            ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
            disp,
            options,
        )
        return handle

    def _open_replace(self, tree, relpath: str):
        """Create-or-replace a file without OVERWRITE_IF (notoriously
        misanswered by some servers): create, else delete + create."""
        from smbprotocol.exceptions import ObjectNameCollision
        from smbprotocol.open import CreateDisposition

        try:
            return self._open(tree, relpath, write=True,
                              disposition=CreateDisposition.FILE_CREATE)
        except ObjectNameCollision:
            pass
        handle = self._open(tree, relpath, delete=True)
        try:
            handle.close()
        except Exception:
            pass
        return self._open(tree, relpath, write=True,
                          disposition=CreateDisposition.FILE_CREATE)

    def _open_dir(self, tree, relpath: str):
        """Open a directory, creating it if missing. Tries CREATE then
        plain OPEN: some servers misanswer OPEN_IF on existing dirs."""
        from smbprotocol.exceptions import AccessDenied, ObjectNameCollision
        from smbprotocol.open import CreateDisposition

        try:
            return self._open(tree, relpath, for_dir=True,
                              disposition=CreateDisposition.FILE_CREATE)
        except ObjectNameCollision:
            pass
        except AccessDenied:
            pass
        return self._open(tree, relpath, for_dir=True,
                          disposition=CreateDisposition.FILE_OPEN)

    def makedirs(self) -> None:
        connection = tree = None
        try:
            connection, tree = self._tree()
            parts = [p for p in self.subpath.split('\\') if p]
            for i in range(1, len(parts) + 1):
                handle = self._open_dir(tree, '\\'.join(parts[:i]))
                try:
                    handle.close()
                except Exception:
                    pass
        except Exception as exc:
            raise _friendly('setup', self.display, exc)
        finally:
            self._close(connection, tree)

    def probe(self) -> None:
        connection = tree = None
        try:
            connection, tree = self._tree()
            rel = self._rel(PROBE_NAME)
            handle = self._open_replace(tree, rel)
            try:
                handle.write(b'ok', 0)
                handle.close()
            except Exception:
                try:
                    handle.close()
                except Exception:
                    pass
                raise
            self.remove(PROBE_NAME)
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise _friendly('write test', self.display, exc)
        finally:
            self._close(connection, tree)

    def store_file(self, local_path: str | os.PathLike) -> dict[str, Any]:
        """Upload an archive with size verification. Returns {name, size}."""
        src = Path(local_path)
        if not src.is_file():
            raise FileNotFoundError(f"Local backup not found: {src}")
        connection = tree = None
        try:
            connection, tree = self._tree()
            rel = self._rel(src.name)
            expected = src.stat().st_size
            handle = self._open_replace(tree, rel)
            # Honor the negotiated transfer limits (old/small servers
            # allow far less than our 1 MiB buffer).
            max_write = getattr(handle.connection, 'max_write_size', 65536) or 65536
            try:
                sent = 0
                with open(src, 'rb') as f:
                    while True:
                        chunk = f.read(min(_CHUNK, max_write))
                        if not chunk:
                            break
                        handle.write(chunk, sent)
                        sent += len(chunk)
                handle.close()
            except Exception:
                try:
                    handle.close()
                except Exception:
                    pass
                raise
            if sent != expected:
                try:
                    self.remove(src.name)
                except Exception:
                    pass
                raise OSError(
                    f"Verify failed after copy: local {expected} bytes != NAS {sent} bytes"
                )
            return {'name': src.name, 'size': sent}
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise _friendly('upload', self.display, exc)
        finally:
            self._close(connection, tree)

    def fetch(self, name: str, local_path: str | os.PathLike) -> Path:
        """Download an archive. Raises FileNotFoundError when missing."""
        from smbprotocol.exceptions import EndOfFile, NoSuchFile, ObjectNameNotFound

        clean = self._safe_name(name)
        connection = tree = None
        try:
            connection, tree = self._tree()
            rel = self._rel(clean)
            try:
                handle = self._open(tree, rel)
            except (NoSuchFile, ObjectNameNotFound) as exc:
                raise FileNotFoundError(f"Backup not found on NAS: {clean}") from exc
            max_read = getattr(handle.connection, 'max_read_size', 65536) or 65536
            try:
                dest = Path(local_path)
                offset = 0
                with open(dest, 'wb') as f:
                    while True:
                        try:
                            chunk = handle.read(offset, min(_CHUNK, max_read))
                        except EndOfFile:
                            break
                        if not chunk:
                            break
                        f.write(chunk)
                        offset += len(chunk)
                handle.close()
            except Exception:
                try:
                    handle.close()
                except Exception:
                    pass
                raise
            return dest
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise _friendly('download', self.display, exc)
        finally:
            self._close(connection, tree)

    def remove(self, name: str) -> None:
        clean = self._safe_name(name)
        connection = tree = None
        try:
            connection, tree = self._tree()
            handle = self._open(tree, self._rel(clean), delete=True)
            try:
                handle.close()
            except Exception:
                pass
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise _friendly('delete', self.display, exc)
        finally:
            self._close(connection, tree)

    def list_backups(self) -> list[dict[str, Any]]:
        from smbprotocol.open import FileInformationClass

        connection = tree = None
        try:
            connection, tree = self._tree()
            handle = self._open_dir(tree, self.subpath or '\\')
            try:
                entries = handle.query_directory(
                    '*', FileInformationClass.FILE_FULL_DIRECTORY_INFORMATION
                )
                handle.close()
            except Exception:
                try:
                    handle.close()
                except Exception:
                    pass
                raise
            out = []
            for item in entries:
                fname = _value(item, 'file_name')
                if isinstance(fname, bytes):
                    fname = fname.decode('utf-16-le', errors='replace')
                fname = str(fname).rstrip('\x00')
                if fname in ('.', '..'):
                    continue
                if not (fname.endswith('.tar.gz') or fname.endswith('.tgz')):
                    continue
                written = _value(item, 'last_write_time')
                try:
                    mtime = written.timestamp()
                except AttributeError:
                    try:
                        mtime = float(written or 0)
                    except (TypeError, ValueError):
                        mtime = 0
                try:
                    size = int(_value(item, 'end_of_file') or 0)
                except (TypeError, ValueError):
                    size = 0
                out.append({'name': fname, 'size': size, 'mtime': mtime})
            out.sort(key=lambda x: x['mtime'], reverse=True)
            return out
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise _friendly('list', self.display, exc)
        finally:
            self._close(connection, tree)

    def prune(self, keep: int) -> dict[str, Any]:
        backups = self.list_backups()
        deleted = []
        for item in backups[keep:] if keep >= 0 else []:
            try:
                self.remove(item['name'])
                deleted.append(item['name'])
            except Exception as exc:
                logger.warning(f"Failed to prune {item['name']}: {exc}")
        return {'kept': len(backups) - len(deleted), 'deleted': deleted}

    def check_space(self, needed_bytes: int) -> None:
        logger.debug("Free-space check skipped on SMB backend; copy errors surface.")


class LocalBackupStore:
    """Native-filesystem backup folder (Windows UNC, mounts, local dirs)."""

    def __init__(self, root: Path):
        from .nas_client import (
            check_free_space,
            copy_to_nas,
            list_nas_backups,
            prune_nas_backups,
        )

        self.root = root
        self._check_free_space = check_free_space
        self._copy_to_nas = copy_to_nas
        self._list = list_nas_backups
        self._prune = prune_nas_backups

    @property
    def display(self) -> str:
        return str(self.root)

    def probe(self) -> None:
        from .nas_client import _probe_dir

        _probe_dir(self.root)

    def store_file(self, local_path: str | os.PathLike) -> dict[str, Any]:
        return self._copy_to_nas(local_path, self.root, verify=True)

    def list_backups(self) -> list[dict[str, Any]]:
        return self._list(self.root)

    def fetch(self, name: str, local_path: str | os.PathLike) -> Path:
        clean = SmbBackupStore._safe_name(name)
        src = self.root / clean
        if not src.is_file():
            raise FileNotFoundError(f"Backup not found on NAS: {clean}")
        dest = Path(local_path)
        shutil.copy2(src, dest)
        return dest

    def remove(self, name: str) -> None:
        target = self.root / SmbBackupStore._safe_name(name)
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    def prune(self, keep: int) -> dict[str, Any]:
        return self._prune(self.root, keep)

    def check_space(self, needed_bytes: int) -> None:
        self._check_free_space(self.root, needed_bytes)


def open_backup_store(nas_path: str, username: str = '', password: str = '',
                      home_id: int | str = '', *,
                      probe: bool = False) -> SmbBackupStore | LocalBackupStore:
    """Backup folder for this install, over direct SMB or native FS."""
    from .nas_client import backup_dir_name, resolve_backup_dir

    if platform.system() != 'Windows' and (nas_path or '').strip().startswith('\\\\'):
        raise ValueError("Backslash paths are Windows-only — use smb://host/share here.")

    parsed = parse_server_path(nas_path)
    if parsed and use_smb_backend(nas_path):
        user = (username or '').strip() or parsed['user']
        pw = password or parsed['password']
        sub = '/'.join(p for p in (parsed['subpath'], backup_dir_name(home_id)) if p)
        store: SmbBackupStore | LocalBackupStore = SmbBackupStore(
            parsed['server'], parsed['port'], parsed['share'], sub, user, pw,
        )
        store.makedirs()
        if probe:
            store.probe()
        return store
    store = LocalBackupStore(
        resolve_backup_dir(nas_path, home_id)
    )
    if probe:
        store.probe()
    return store
