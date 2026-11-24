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
        """トークンを検証して保持する。不正なら ValueError("セッション形式が不正です")。"""
        if not _valid_token(token):
            raise ValueError("セッション形式が不正です")
        self._token = token

    @property
    def token(self) -> str:
        """保存・認証に使うトークン。`repr` には出さない。"""
        return self._token

    def __repr__(self) -> str:
        """トークンを伏せた表現を返す。"""
        return "MemberSession([REDACTED])"

    def to_dict(self) -> dict:
        """保存用の辞書にする。キーは `token` だけ。"""
        return {"token": self._token}


def _valid_token(token: str) -> bool:
    """Cookie として送れる ASCII トークンか。1〜4096バイト、制御文字と区切り文字を拒否。"""
    if not token or len(token.encode()) > 4096:
        return False
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(byte in _COOKIE_OCTETS for byte in raw)


class Membership:
    """会員の聴取可否。`area_free` はエリア外聴取、`time_free_30` は30日間の
    タイムフリー。不明は None。
    """

    def __init__(self, area_free: bool | None, time_free_30: bool | None) -> None:
        """2つの可否を保持する。値は True / False / None（未確認）。"""
        self.area_free = area_free
        self.time_free_30 = time_free_30

    def __eq__(self, other: object) -> bool:
        """2つの可否が同じなら等しい。"""
        return (
            isinstance(other, Membership)
            and self.area_free == other.area_free
            and self.time_free_30 == other.time_free_30
        )

    def status_line(self) -> str:
        """契約を「ログイン有効 / エリアフリー: あり|なし|未確認 / タイムフリー30: …」にする。"""

        def label(value: bool | None) -> str:
            """True / False / None を あり / なし / 未確認 にする。"""
            if value is True:
                return "あり"
            if value is False:
                return "なし"
            return "未確認"

        return (
            f"ログイン有効 / エリアフリー: {label(self.area_free)} / "
            f"タイムフリー30: {label(self.time_free_30)}"
        )


class StorageError(Exception):
    """session.json の読み書き失敗。`code` と `detail` から表示文を作る。

    code は io / invalid / version / permissions / path / nodir。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        """code と detail を保持し、表示文を例外メッセージにする。"""
        self.code = code
        self.detail = detail
        super().__init__(self.text)

    @property
    def text(self) -> str:
        """code に対応する利用者向けの文言。未知の code は汎用の失敗文。"""
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
    """searches.json の読み書き失敗。名前・件数・ロックの失敗も表す。

    code は name / missing / many / storage。storage のときは元の StorageError を持つ。
    """

    def __init__(self, code: str, storage: StorageError | None = None) -> None:
        """code と、storage 失敗なら元の StorageError を保持する。"""
        self.code = code
        self.storage = storage
        super().__init__(str(self))

    def __str__(self) -> str:
        """code（と元の StorageError）に対応する利用者向けの文言。"""
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
    """OSError を保存層の短い分類に変える。

    NotFound / PermissionDenied / WouldBlock / StorageFull、該当しなければ例外クラス名。
    """
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
    """データ保存先のディレクトリ。session と searches のストアを作る。"""

    def __init__(self, directory: Path) -> None:
        """保存先ディレクトリを受け取る。作成はしない。"""
        self.directory = directory

    @classmethod
    def default(cls) -> DataDir:
        """既定の保存先を返す。解決できない場合は StorageError("nodir")。"""
        try:
            return cls(default_data_dir())
        except OSError as error:
            raise StorageError("nodir") from error

    def sessions(self) -> SessionStore:
        """このディレクトリを使う SessionStore を返す。"""
        return SessionStore(self.directory)

    def searches(self) -> SearchStore:
        """このディレクトリを使う SearchStore を返す。"""
        return SearchStore(self.directory)


class SessionStore:
    """session.json の読み書き。ディレクトリ0700・ファイル0600・原子的置換を守る。
    シンボリックリンクと通常ファイル以外は拒否する。
    """

    def __init__(self, directory: Path) -> None:
        """保存先ディレクトリを受け取る。作成は save 時。"""
        self.directory = Path(directory)

    @classmethod
    def default(cls) -> SessionStore:
        """既定のデータ保存先を使うストアを返す。"""
        return DataDir.default().sessions()

    @classmethod
    def for_dir(cls, data_dir: Path | None = None) -> SessionStore:
        """data_dir があれば cls(data_dir)、無ければ cls.default() を返す。"""
        return cls(data_dir) if data_dir is not None else cls.default()

    @property
    def path(self) -> Path:
        """session.json のパス。"""
        return self.directory / "session.json"

    def load(self) -> MemberSession | None:
        """session.json を読む。無ければ None。

        ディレクトリ・ファイルは 0700/0600 と通常ファイル（リンク不可）を要求する。
        16KiB 超・JSON 不正・未知 version は StorageError。破損ファイルは削除せず残す。
        """
        data = self._read_file(self.path, MAX_SESSION_BYTES)
        if data is None:
            return None
        return _decode_session(data)

    def _read_file(self, path: Path, max_bytes: int) -> bytes | None:
        """0700 ディレクトリ配下の 0600 通常ファイルを上限サイズ以内で読む。無ければ None。"""
        if not self._directory_exists():
            return None
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        _regular(metadata)
        # TOCTOU によるシンボリックリンク差し替えと、FIFO 等での無限ブロックを防ぐため、
        # O_NOFOLLOW | O_NONBLOCK で開いた fd を再度 fstat して検証する。
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        try:
            opened = os.fstat(fd)
            _regular(opened)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise StorageError("permissions")
            data = os.read(fd, max_bytes + 1)
        except OSError as error:
            raise StorageError("io", io_kind(error)) from error
        finally:
            os.close(fd)
        if len(data) > max_bytes:
            raise StorageError("invalid")
        return data

    def save(self, session: MemberSession) -> None:
        """session.json を書く。

        ディレクトリを 0700 で作り、0600 の一時ファイルから os.replace で原子的に置換する。
        既存のリンクは拒否する。
        """
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
        """session.json を削除してディレクトリを fsync する。無ければ何もしない。"""
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
        """保存先ディレクトリの存在と 0700・非リンクを確認する。無ければ False。"""
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
        """保存先が無ければ 0700 で作り、既存なら権限とリンクだけを確認する。"""
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
    """searches.json の中身。直近条件と、名前付き条件の辞書。"""

    def __init__(
        self, last: SearchCriteria | None = None, named: dict[str, SearchCriteria] | None = None
    ):
        """直近条件と名前付き条件を保持する。named の既定は空。"""
        self.last = last
        self.named = named or {}


class SearchStore:
    """searches.json の読み書き。ロックと原子的置換、0700/0600 を守る。"""

    def __init__(self, directory: Path) -> None:
        """保存先ディレクトリを受け取る。session と同じディレクトリを使う。"""
        self.base = SessionStore(directory)

    @classmethod
    def for_dir(cls, data_dir: Path | None = None) -> SearchStore:
        """data_dir があれば cls(data_dir)、無ければ DataDir.default().searches() を返す。"""
        return cls(data_dir) if data_dir is not None else DataDir.default().searches()

    @property
    def path(self) -> Path:
        """searches.json のパス。"""
        return self.base.directory / "searches.json"

    def load(self) -> Library:
        """searches.json を読む。無ければ空の Library。

        形式は {"version": 1, "last": 条件|null, "named": {名前: 条件}}。
        壊れたファイルは SearchError("storage") にするが削除しない。
        """
        try:
            data = self.base._read_file(self.path, MAX_SEARCH_BYTES)
        except StorageError as error:
            raise SearchError("storage", error) from error
        if data is None:
            return Library()
        return _decode_library(data)

    def save_last(self, criteria: SearchCriteria) -> None:
        """ロックを取って直近条件だけを更新する。名前付き条件は保つ。"""
        with self._lock():
            library = self.load()
            library.last = criteria
            self._write(library)

    def save_named(self, name: str, criteria: SearchCriteria) -> None:
        """ロックを取って名前付き条件を追加・更新する。

        名前は `_check_name` の制約。64件を超える場合は SearchError("many")。
        """
        _check_name(name)
        with self._lock():
            library = self.load()
            library.named[name] = criteria
            if len(library.named) > MAX_NAMES:
                raise SearchError("many")
            self._write(library)

    def delete_named(self, name: str) -> None:
        """ロックを取って名前付き条件を削除する。無ければ SearchError("missing")。"""
        _check_name(name)
        with self._lock():
            library = self.load()
            if name not in library.named:
                raise SearchError("missing")
            del library.named[name]
            self._write(library)

    def _lock(self):
        """searches.lock を 0600 で開き、非ブロッキングの排他ロックを取る。

        取得できなければ SearchError("storage", WouldBlock)。戻り値は with 用の _Lock。
        """
        try:
            self.base._ensure_directory()
        except StorageError as error:
            raise SearchError("storage", error) from error
        # _atomic_write が os.replace で searches.json の inode を差し替えるため、
        # 本体を flock すると置換後に排他が別 inode に分かれてしまう。
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
        """Library を searches.json に書く。

        version 1、last、名前順の named。128KiB 超は invalid。0600 の原子的置換。
        """
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
    """searches.lock の fd を with 終了時に閉じる。"""

    def __init__(self, fd: int) -> None:
        """ロック済みの fd を保持する。"""
        self.fd = fd

    def __enter__(self) -> _Lock:
        """with の本体へ自分を返す。"""
        return self

    def __exit__(self, *_) -> None:
        """fd を閉じてロックを解放する。"""
        os.close(self.fd)


def _regular(metadata: os.stat_result) -> None:
    """通常ファイルでリンクでないことを要求する。違反は StorageError("path")。"""
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise StorageError("path")


def _sync_directory(directory: Path) -> None:
    """ディレクトリを fsync し、rename を永続化する。"""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(directory: Path, path: Path, payload: bytes) -> None:
    """0600 の一時ファイルに書いて fsync し、os.replace で原子的に置き換える。

    失敗時は一時ファイルを消し、成功後は os.chmod で 0600 にしてから
    ディレクトリを fsync する。
    """
    tmp = directory / f".{path.name}.{os.getpid()}.tmp"
    # umask や既存ファイルの権限に影響されず確実に 0600 とし、
    # rename 後のディレクトリエントリまで永続化するため fchmod・chmod・fsync を行う。
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
    """session.json の bytes を MemberSession にする。

    version 1 と {"version","session":{"token"}} だけを許す。未知 version は
    "version"、形・値の不正は "invalid"。
    """
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
    """searches.json の bytes を Library にする。

    version 1・last・named だけを許し、名前は `_check_name`、条件は `criteria_from_file` で検証する。
    形式・値の不正は `SearchError("storage")`（サイズ検査は `load` / `_write` 側）。
    """
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
    """条件名を検証する。空・前後空白・制御文字・64文字超・256バイト超は
    SearchError("name")。
    """
    if (
        not name
        or name != name.strip()
        or len(name) > 64
        or len(name.encode()) > 256
        or has_control(name)
    ):
        raise SearchError("name")
