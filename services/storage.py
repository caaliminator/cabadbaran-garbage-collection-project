"""
JSON file storage.

Every read and write of a collection goes through this module -- no route or
template may call `open()` on a data file. That single choke point is what
makes the three guarantees below possible:

  atomic writes   data is written to a temp file in the same directory and
                  then os.replace()d over the target, so a crash mid-write can
                  never leave a half-written (corrupt) collection behind
  per-file locks  one reentrant lock per collection serialises writers inside
                  the process, and an OS file lock serialises them across
                  processes -- so neither two requests nor two workers nor a
                  `seed.py` run against a live server can clobber each other
  auto-create     a missing collection is created with its default shape on
                  first touch, so a fresh checkout just runs

Read-modify-write must go through `transaction()`, which holds the lock across
the whole cycle. Doing `rows = read(); rows.append(x); write(rows)` outside a
transaction is a lost-update bug waiting to happen -- the helpers below
(`insert`, `update`, `delete`) all use it.

Swapping this file for SQLite later means reimplementing these functions only;
nothing above it knows the data lives in files.
"""

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config import Config
from services import timeutil

# ---------------------------------------------------------------------------
# Collection registry
#
# name -> id prefix. Every collection is a JSON array of objects; records carry
# a string id of the form "<prefix>-0001". Add a collection here and it exists.
# ---------------------------------------------------------------------------

COLLECTIONS: dict[str, str] = {
    "users": "usr",
    "barangays": "brgy",
    "vehicles": "veh",
    "assignments_tricycle": "atri",
    "assignments_truck": "atrk",
    "properties": "prop",
    "collections": "col",
    "mrf_pickups": "mrf",
    "deliveries": "dlv",
    "carry_overs": "cyo",
    "waste_schedule": "sched",
    "unavailable_requests": "unav",
    "notifications": "ntf",
    "public_reports": "rep",
    "history": "hist",
}

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

# Cross-process locking primitives, whichever this platform provides.
try:                                    # POSIX
    import fcntl as _fcntl

    _flock = _fcntl.flock
    _flock_exclusive = _fcntl.LOCK_EX
    _flock_release = _fcntl.LOCK_UN
    _msvcrt = None
except ImportError:                     # Windows
    _flock = _flock_exclusive = _flock_release = None
    try:
        import msvcrt as _msvcrt
    except ImportError:                 # pragma: no cover - neither available
        _msvcrt = None


class StorageError(RuntimeError):
    """Raised for an unknown collection or an unreadable data file."""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    return Path(Config.DATA_DIR)


def path_for(name: str) -> Path:
    if name not in COLLECTIONS:
        raise StorageError(
            f"Unknown collection {name!r}. Register it in storage.COLLECTIONS."
        )
    return _data_dir() / f"{name}.json"


def _lock_for(name: str) -> threading.RLock:
    with _locks_guard:
        if name not in _locks:
            _locks[name] = threading.RLock()
        return _locks[name]


@contextmanager
def _file_lock(name: str):
    """
    An exclusive lock held across *processes*, not just threads.

    The threading lock above only serialises writers inside one interpreter.
    Two web workers, or a `seed.py` run against a live server, are separate
    processes and would happily write over each other -- the second one's
    read-modify-write would silently discard the first one's record.

    Uses the OS advisory lock: `fcntl.flock` on POSIX, `msvcrt.locking` on
    Windows. If neither is available the lock degrades to a no-op and the
    threading lock still protects the single-process case, which is the
    deployment this ships for.
    """
    lock_dir = _data_dir() / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / f"{name}.lock", "a+b")

    try:
        if _flock:
            _flock(handle.fileno(), _flock_exclusive)
        elif _msvcrt:
            handle.seek(0)
            # Blocks and retries for ~10s, then raises -- long enough for any
            # honest write, short enough to surface a genuine deadlock.
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if _flock:
                _flock(handle.fileno(), _flock_release)
            elif _msvcrt:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass            # the lock goes with the handle either way
        handle.close()


@contextmanager
def _guard(name: str):
    """Both locks: threads within this process, then processes on this host."""
    with _lock_for(name):
        with _file_lock(name):
            yield


def _read_raw(name: str) -> list[dict]:
    """Read without locking. Callers must already hold the lock."""
    file = path_for(name)
    if not file.exists():
        return []
    try:
        text = file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StorageError(f"Could not read {file}: {exc}") from exc
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StorageError(
            f"{file} is not valid JSON ({exc}). Fix or delete the file and "
            f"re-run seed.py."
        ) from exc
    if not isinstance(data, list):
        raise StorageError(f"{file} must contain a JSON array, got {type(data).__name__}.")
    return data


def _replace_with_retry(tmp: str, file: Path,
                        attempts: int = 8, delay: float = .05) -> None:
    """
    os.replace, retried briefly on a Windows sharing violation.

    On Windows a rename fails outright while any other process holds the target
    open -- and on a normal machine something does, intermittently: Defender
    scanning the file we just wrote, the search indexer, or a OneDrive client
    syncing the folder. The hold lasts milliseconds; the write is otherwise
    perfectly valid. Without this, a save fails for a reason that has nothing
    to do with the app and everything to do with where the folder lives.

    POSIX has no such restriction, so this is a no-op there.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, file)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))   # back off: 50ms, 100ms, 150ms...


def _write_raw(name: str, rows: list[dict]) -> None:
    """Atomically replace the collection. Callers must already hold the lock."""
    file = path_for(name)
    file.parent.mkdir(parents=True, exist_ok=True)

    # Temp file in the SAME directory: os.replace is only atomic within a
    # filesystem, and /tmp may well be a different one.
    fd, tmp = tempfile.mkstemp(dir=str(file.parent), prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, file)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def bootstrap() -> None:
    """Create the data directory and any missing collection, empty."""
    _data_dir().mkdir(parents=True, exist_ok=True)
    Path(Config.GEO_DIR).mkdir(parents=True, exist_ok=True)
    for name in COLLECTIONS:
        file = path_for(name)
        if not file.exists():
            with _guard(name):
                if not file.exists():
                    _write_raw(name, [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read(name: str) -> list[dict]:
    """Every record in a collection, as a deep copy safe to mutate freely."""
    with _guard(name):
        return copy.deepcopy(_read_raw(name))


def write(name: str, rows: list[dict]) -> None:
    """Replace a whole collection. Prefer insert/update/delete where possible."""
    with _guard(name):
        _write_raw(name, list(rows))


@contextmanager
def transaction(name: str):
    """
    Hold the collection's lock across a read-modify-write.

        with storage.transaction("users") as rows:
            rows.append(record)

    The list is written back on a clean exit; an exception rolls the whole
    change back by simply not writing.
    """
    with _guard(name):
        rows = _read_raw(name)
        yield rows
        _write_raw(name, rows)


def next_id(name: str, rows: list[dict] | None = None) -> str:
    """Next sequential id, e.g. 'usr-0007'. Call inside a transaction."""
    prefix = COLLECTIONS[name]
    source = _read_raw(name) if rows is None else rows
    highest = 0
    for row in source:
        rid = str(row.get("id", ""))
        if rid.startswith(f"{prefix}-"):
            tail = rid.split("-", 1)[1]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return f"{prefix}-{highest + 1:04d}"


def insert(name: str, record: dict, actor: str | None = None) -> dict:
    """Add a record, stamping id and audit fields. Returns the stored copy."""
    with transaction(name) as rows:
        stored = dict(record)
        stored.setdefault("id", next_id(name, rows))
        stored.setdefault("created_at", timeutil.stamp())
        stored.setdefault("created_by", actor)
        stored.setdefault("updated_at", None)
        stored.setdefault("updated_by", None)
        rows.append(stored)
        return copy.deepcopy(stored)


def update(name: str, record_id: str, changes: dict,
           actor: str | None = None) -> dict | None:
    """Merge `changes` into one record. Returns the updated copy, or None."""
    with transaction(name) as rows:
        for row in rows:
            if row.get("id") == record_id:
                row.update(changes)
                row["updated_at"] = timeutil.stamp()
                row["updated_by"] = actor
                return copy.deepcopy(row)
    return None


def delete(name: str, record_id: str) -> dict | None:
    """Remove a record. Returns the removed copy, or None if it was absent."""
    with transaction(name) as rows:
        for index, row in enumerate(rows):
            if row.get("id") == record_id:
                return copy.deepcopy(rows.pop(index))
    return None


def get(name: str, record_id: str) -> dict | None:
    for row in read(name):
        if row.get("id") == record_id:
            return row
    return None


def find(name: str, predicate=None, **equals) -> list[dict]:
    """
    Filter a collection.

        find("users", role="tricycle_collector", status="Active")
        find("collections", lambda r: r["date"] == today)

    Keyword filters are exact matches; `predicate` is an extra callable. A
    keyword whose value is None is ignored, so optional request filters can be
    passed straight through.
    """
    rows = read(name)
    active = {k: v for k, v in equals.items() if v is not None}
    if active:
        rows = [r for r in rows if all(r.get(k) == v for k, v in active.items())]
    if predicate:
        rows = [r for r in rows if predicate(r)]
    return rows


def find_one(name: str, predicate=None, **equals) -> dict | None:
    matches = find(name, predicate, **equals)
    return matches[0] if matches else None


def exists(name: str, predicate=None, **equals) -> bool:
    return find_one(name, predicate, **equals) is not None


def count(name: str, predicate=None, **equals) -> int:
    return len(find(name, predicate, **equals))


def upsert_by(name: str, key: str, record: dict,
              actor: str | None = None) -> tuple[dict, bool]:
    """
    Insert a record, or leave the existing one alone if `key` already matches.

    This is what makes seeding idempotent: re-running the seed script must not
    duplicate rows, and must not overwrite edits an admin has since made.
    Returns (record, created).
    """
    with transaction(name) as rows:
        wanted = record.get(key)
        for row in rows:
            if row.get(key) == wanted:
                return copy.deepcopy(row), False
        stored = dict(record)
        stored.setdefault("id", next_id(name, rows))
        stored.setdefault("created_at", timeutil.stamp())
        stored.setdefault("created_by", actor)
        stored.setdefault("updated_at", None)
        stored.setdefault("updated_by", None)
        rows.append(stored)
        return copy.deepcopy(stored), True
