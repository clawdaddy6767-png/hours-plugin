from __future__ import annotations
import atexit
import json
import os
import shutil
import sqlite3
import tempfile
from urllib.parse import quote
from typing import Any, Dict, Iterator, List, Optional, Tuple
COPY_LIMIT = 2 * 1024 * 1024 * 1024
_SIDES = ("-wal", "-shm")
_CACHE: Dict[str, Tuple[Tuple, sqlite3.Connection, Optional[str]]] = {}
class SnapshotError(Exception):
    pass
def _stamp(path: str) -> Tuple:
    out = []
    for p in (path,) + tuple(path + s for s in _SIDES):
        try:
            st = os.stat(p)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)
def _text(b: bytes) -> str:
    return b.decode("utf-8", "replace")
def snapshot(path: str) -> sqlite3.Connection:
    real = os.path.realpath(path)
    stamp = _stamp(real)
    hit = _CACHE.get(real)
    if hit and hit[0] == stamp:
        return hit[1]
    if hit:
        _drop(real)
    if stamp[0] is None:
        raise SnapshotError("database not found")
    tmp: Optional[str] = None
    try:
        if stamp[0][0] <= COPY_LIMIT:
            tmp = tempfile.mkdtemp(prefix="hours-db-")
            os.chmod(tmp, 0o700)
            dest = os.path.join(tmp, "copy.db")
            shutil.copyfile(real, dest)
            for side, st in zip(_SIDES, stamp[1:]):
                if st is not None:
                    try:
                        shutil.copyfile(real + side, dest + side)
                    except OSError:
                        pass
            conn = sqlite3.connect("file:%s?mode=ro" % quote(dest), uri=True, timeout=5)
        else:
            conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % quote(real), uri=True, timeout=5)
        conn.text_factory = _text
        conn.execute("select count(*) from sqlite_master").fetchone()
    except (OSError, sqlite3.Error) as exc:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SnapshotError("database could not be read") from exc
    _CACHE[real] = (stamp, conn, tmp)
    return conn
def _drop(real: str) -> None:
    hit = _CACHE.pop(real, None)
    if not hit:
        return
    try:
        hit[1].close()
    except sqlite3.Error:
        pass
    if hit[2]:
        shutil.rmtree(hit[2], ignore_errors=True)
def close_all() -> None:
    for real in list(_CACHE):
        _drop(real)
atexit.register(close_all)
def tables(conn: sqlite3.Connection) -> List[str]:
    try:
        return [str(r[0]) for r in conn.execute("select name from sqlite_master where type = 'table'")]
    except sqlite3.Error:
        return []
def columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if not table.replace("_", "").isalnum():
        return []
    try:
        return [str(r[1]) for r in conn.execute("pragma table_info(%s)" % table)]
    except sqlite3.Error:
        return []
def rows(conn: sqlite3.Connection, sql: str, args: Tuple = ()) -> Iterator[Tuple]:
    try:
        cur = conn.execute(sql, args)
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                return
            for r in batch:
                yield r
    except sqlite3.Error:
        return
def loads(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
def to_epoch(value: Any) -> Optional[float]:
    from ..model import parse_ts
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().replace(".", "", 1).isdigit():
        value = float(value)
    dt = parse_ts(value)
    return dt.timestamp() if dt is not None else None
def prefix_range(prefix: str) -> Tuple[str, str]:
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)
