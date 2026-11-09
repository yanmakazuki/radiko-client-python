"""session.json と searches.json。原子的な置換、Unix では 0700 / 0600。"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from pathlib import Path

from radiko.domain import SearchCriteria, criteria_from_file
from radiko.text import has_control

MAX_SESSION_BYTES = 16 * 1024
MAX_SEARCH_BYTES = 128 * 1024
MAX_NAMES = 64

_COOKIE_OCTETS = set(range(0x21, 0x7F)) - {0x22, 0x2C, 0x3B, 0x5C}


class MemberSession:
    """再利用できるセッショントークンだけを持つ。パスワードは持たない。"""

    def __init__(self, token: str) -> None:
        if not _valid_token(token):
            raise ValueError("セッション形式が不正です")
        self._token = token

    @property
    def token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return "MemberSession([REDACTED])"

    def to_dict(self) -> dict:
        return {"token": self._token}


def _valid_token(token: str) -> bool:
    if not token or len(token.encode()) > 4096:
        return False
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(byte in _COOKIE_OCTETS for byte in raw)


class Membership:
    def __init__(self, area_free: bool | None, time_free_30: bool | None) -> None:
        self.area_free = area_free
        self.time_free_30 = time_free_30

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Membership)
            and self.area_free == other.area_free
            and self.time_free_30 == other.time_free_30
        )


class StorageError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(self.text)

    @property
    def text(self) -> str:
        if self.code == "io":
            return f"セッションファイルの操作に失敗しました ({self.detail})"
        if self.code == "invalid":
            return "セッションファイルが破損しています"
        if self.code == "version":
            return "未対応のセッション保存形式です"
        if self.code == "permissions":
            return "セッションのディレクトリは0700、ファイルは0600にしてください"
        if self.code == "path":
            return "セッション保存先にシンボリックリンクや通常ファイル以外は使用できません"
        if self.code == "nodir":
            return "データ保存先を解決できません。--data-dirを指定してください"
        return "セッションファイルの操作に失敗しました"


class SearchError(Exception):
    def __init__(self, code: str, storage: StorageError | None = None) -> None:
        self.code = code
        self.storage = storage
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.code == "name":
            return "条件名は前後空白・制御文字なしの1〜64文字で指定してください"
        if self.code == "missing":
            return "指定した名前の検索条件がありません"
        if self.code == "many":
            return "名前付き検索条件は最大64件です"
        error = self.storage or StorageError("invalid")
        if error.code == "io" and error.detail == "WouldBlock":
            return "別の処理が検索条件を更新中です。再実行してください"
        if error.code == "io":
            return f"検索条件ファイルの操作に失敗しました ({error.detail})"
        if error.code == "invalid":
            return "検索条件ファイルの形式・値・サイズが不正です"
        if error.code == "version":
            return "未対応の検索条件保存形式です"
        if error.code == "permissions":
            return "検索条件のディレクトリは0700、ファイルは0600にしてください"
        if error.code == "path":
            return "検索条件保存先にリンクや通常ファイル以外は使用できません"
        if error.code == "nodir":
            return "検索条件保存先を解決できません。--data-dirを指定してください"
        return "検索条件ファイルの操作に失敗しました"


def io_kind(error: OSError) -> str:
    if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
        return "NotFound"
    if isinstance(error, PermissionError) or error.errno == errno.EACCES:
        return "PermissionDenied"
    if isinstance(error, BlockingIOError) or error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
        return "WouldBlock"
    if error.errno == errno.ENOSPC:
        return "StorageFull"
    return error.__class__.__name__


def default_data_dir() -> Path:
    """directories 6 の data_local_dir と同じ場所。"""
    if os.name == "posix" and os.uname().sysname == "Darwin":
        home = Path.home()
        return home / "Library" / "Application Support" / "jp.radiko-client.radiko-client"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "radiko-client"
    return Path.home() / ".local" / "share" / "radiko-client"


class DataDir:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @classmethod
    def default(cls) -> DataDir:
        try:
            return cls(default_data_dir())
        except OSError as error:
            raise StorageError("nodir") from error

    def sessions(self) -> SessionStore:
        return SessionStore(self.directory)

    def searches(self) -> SearchStore:
        return SearchStore(self.directory)


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @classmethod
    def default(cls) -> SessionStore:
        return DataDir.default().sessions()

    @property
    def path(self) -> Path:
        return self.directory / "session.json"

    def load(self) -> MemberSession | None:
        if not self._directory_exists():
            return None
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        _regular(metadata)
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        try:
            opened = os.fstat(fd)
            _regular(opened)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise StorageError("permissions")
            data = os.read(fd, MAX_SESSION_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_SESSION_BYTES:
            raise StorageError("invalid")
        return _decode_session(data)

    def save(self, session: MemberSession) -> None:
        if not _valid_token(session.token):
            raise StorageError("invalid")
        self._ensure_directory()
        if self.path.exists() or self.path.is_symlink():
            _regular(self.path.lstat())
        payload = (
            json.dumps(
                {"version": 1, "session": session.to_dict()},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        _atomic_write(self.directory, self.path, payload)

    def delete(self) -> None:
        if not self._directory_exists():
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        _sync_directory(self.directory)

    def _directory_exists(self) -> bool:
        try:
            metadata = self.directory.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise StorageError("path")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise StorageError("permissions")
        return True

    def _ensure_directory(self) -> None:
        if self._directory_exists():
            return
        try:
            self.directory.mkdir(mode=0o700, parents=True)
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        os.chmod(self.directory, 0o700)
        if not self._directory_exists():
            raise StorageError("path")


class Library:
    def __init__(
        self, last: SearchCriteria | None = None, named: dict[str, SearchCriteria] | None = None
    ):
        self.last = last
        self.named = named or {}


class SearchStore:
    def __init__(self, directory: Path) -> None:
        self.base = SessionStore(directory)

    @property
    def path(self) -> Path:
        return self.base.directory / "searches.json"

    def load(self) -> Library:
        try:
            if not self.base._directory_exists():
                return Library()
            try:
                metadata = self.path.lstat()
            except FileNotFoundError:
                return Library()
            _regular(metadata)
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except StorageError as error:
            raise SearchError("storage", error) from error
        except OSError as error:
            raise SearchError("storage", StorageError("io", io_kind(error))) from error
        try:
            opened = os.fstat(fd)
            _regular(opened)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise SearchError("storage", StorageError("permissions"))
            data = os.read(fd, MAX_SEARCH_BYTES + 1)
        except SearchError:
            raise
        except OSError as error:
            raise SearchError("storage", StorageError("io", io_kind(error))) from error
        finally:
            os.close(fd)
        if len(data) > MAX_SEARCH_BYTES:
            raise SearchError("storage", StorageError("invalid"))
        return _decode_library(data)

    def save_last(self, criteria: SearchCriteria) -> None:
        with self._lock():
            library = self.load()
            library.last = criteria
            self._write(library)

    def save_named(self, name: str, criteria: SearchCriteria) -> None:
        _check_name(name)
        with self._lock():
            library = self.load()
            library.named[name] = criteria
            if len(library.named) > MAX_NAMES:
                raise SearchError("many")
            self._write(library)

    def delete_named(self, name: str) -> None:
        _check_name(name)
        with self._lock():
            library = self.load()
            if name not in library.named:
                raise SearchError("missing")
            del library.named[name]
            self._write(library)

    def _lock(self):
        try:
            self.base._ensure_directory()
        except StorageError as error:
            raise SearchError("storage", error) from error
        path = self.base.directory / "searches.lock"
        if path.is_symlink():
            raise SearchError("storage", StorageError("path"))
        try:
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError as error:
            raise SearchError("storage", StorageError("io", io_kind(error))) from error
        try:
            opened = os.fstat(fd)
            _regular(opened)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise SearchError("storage", StorageError("permissions"))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except SearchError:
            os.close(fd)
            raise
        except BlockingIOError as error:
            os.close(fd)
            raise SearchError("storage", StorageError("io", "WouldBlock")) from error
        except OSError as error:
            os.close(fd)
            raise SearchError("storage", StorageError("io", io_kind(error))) from error
        return _Lock(fd)

    def _write(self, library: Library) -> None:
        named = {
            name: criteria.to_file_dict()
            for name, criteria in sorted(library.named.items(), key=lambda item: item[0])
        }
        stored = {
            "version": 1,
            "last": None if library.last is None else library.last.to_file_dict(),
            "named": named,
        }
        payload = json.dumps(stored, ensure_ascii=False, indent=2).encode() + b"\n"
        if len(payload) > MAX_SEARCH_BYTES:
            raise SearchError("storage", StorageError("invalid"))
        try:
            if self.path.exists() or self.path.is_symlink():
                _regular(self.path.lstat())
            _atomic_write(self.base.directory, self.path, payload)
        except StorageError as error:
            raise SearchError("storage", error) from error


class _Lock:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self) -> _Lock:
        return self

    def __exit__(self, *_) -> None:
        os.close(self.fd)


def _regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise StorageError("path")


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(directory: Path, path: Path, payload: bytes) -> None:
    tmp = directory / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    except OSError as error:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise StorageError("io", io_kind(error)) from error
    os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        raise StorageError("io", io_kind(error)) from error
    os.chmod(path, 0o600)
    _sync_directory(directory)


def _decode_session(data: bytes) -> MemberSession:
    try:
        value = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StorageError("invalid") from error
    if not isinstance(value, dict) or "version" not in value:
        raise StorageError("invalid")
    version = value["version"]
    if version != 1:
        if isinstance(version, int):
            raise StorageError("version")
        raise StorageError("invalid")
    if set(value) != {"version", "session"} or not isinstance(value["session"], dict):
        raise StorageError("invalid")
    session = value["session"]
    if set(session) != {"token"} or not isinstance(session["token"], str):
        raise StorageError("invalid")
    try:
        return MemberSession(session["token"])
    except ValueError as error:
        raise StorageError("invalid") from error


def _decode_library(data: bytes) -> Library:
    try:
        value = json.loads(data.decode())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SearchError("storage", StorageError("invalid")) from error
    if not isinstance(value, dict) or "version" not in value:
        raise SearchError("storage", StorageError("invalid"))
    version = value["version"]
    if version != 1:
        if isinstance(version, int):
            raise SearchError("storage", StorageError("version"))
        raise SearchError("storage", StorageError("invalid"))
    if set(value) != {"version", "last", "named"}:
        raise SearchError("storage", StorageError("invalid"))
    named_raw = value["named"]
    if not isinstance(named_raw, dict) or len(named_raw) > MAX_NAMES:
        raise SearchError("storage", StorageError("invalid"))
    named: dict[str, SearchCriteria] = {}
    for name, criteria in named_raw.items():
        try:
            _check_name(name)
            named[name] = criteria_from_file(criteria)
        except (SearchError, ValueError, KeyError, TypeError) as error:
            raise SearchError("storage", StorageError("invalid")) from error
    last = None
    if value["last"] is not None:
        try:
            last = criteria_from_file(value["last"])
        except (ValueError, KeyError, TypeError) as error:
            raise SearchError("storage", StorageError("invalid")) from error
    return Library(last, named)


def _check_name(name: str) -> None:
    if (
        not name
        or name != name.strip()
        or len(name) > 64
        or len(name.encode()) > 256
        or has_control(name)
    ):
        raise SearchError("name")
