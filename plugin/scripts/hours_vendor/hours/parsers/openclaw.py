from __future__ import annotations
import glob
import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional
from ..model import Session
from . import _sqlite, pi
from ._common import DEFAULT_TOOL_RESULT_CHARS, iter_jsonl
SOURCE = "openclaw"
MAIN_KEY_RE = re.compile(r"^agent:[^:]+:main$", re.I)
SESSIONS_JSON_CAP = 8 * 1024 * 1024
def state_dir() -> str:
    return os.path.expanduser(os.environ.get("OPENCLAW_STATE_DIR") or "~/.openclaw")
def db_paths(root: Optional[str] = None) -> List[str]:
    return sorted(glob.glob(os.path.join(root or state_dir(), "agents", "*", "agent", "openclaw-agent.sqlite")))
def _main_ids_from_index(sessions_dir: str) -> set:
    p = os.path.join(sessions_dir, "sessions.json")
    try:
        if os.path.getsize(p) > SESSIONS_JSON_CAP:
            return set()
        with open(p, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return set()
    out = set()
    if isinstance(doc, dict):
        for key, ent in doc.items():
            if MAIN_KEY_RE.match(str(key)) and isinstance(ent, dict) and isinstance(ent.get("sessionId"), str):
                out.add(ent["sessionId"])
    return out
def session_refs(root: Optional[str] = None) -> List[Dict[str, Any]]:
    root = root or state_dir()
    out: List[Dict[str, Any]] = []
    for path in db_paths(root):
        try:
            conn = _sqlite.snapshot(path)
        except _sqlite.SnapshotError:
            continue
        tabs = set(_sqlite.tables(conn))
        if {"session_windows", "session_nodes", "transcript_events"} <= tabs:
            sql = ("select w.session_id, w.started_at, w.ended_at, w.created_at, w.updated_at, w.session_key, "
                   "(select count(*) from transcript_events e where e.session_id = w.session_id) "
                   "from session_windows w")
        elif {"sessions", "transcript_events"} <= tabs:
            sql = ("select s.session_id, null, null, s.created_at, s.updated_at, s.session_key, "
                   "(select count(*) from transcript_events e where e.session_id = s.session_id) from sessions s")
        else:
            continue
        for sid, started, ended, created, updated, key, n in _sqlite.rows(conn, sql):
            if not MAIN_KEY_RE.match(str(key or "")) or not n:
                continue
            c = _sqlite.to_epoch(started) or _sqlite.to_epoch(created)
            if c is None:
                continue
            out.append({"id": str(sid), "path": path, "created": c,
                        "updated": max([x for x in (_sqlite.to_epoch(ended), _sqlite.to_epoch(updated), c) if x]),
                        "messages": int(n)})
    for sdir in sorted(glob.glob(os.path.join(root, "agents", "*", "sessions"))):
        main_ids = _main_ids_from_index(sdir)
        if not main_ids:
            continue
        try:
            names = sorted(os.listdir(sdir))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl") or n[:-6] not in main_ids:
                continue
            p = os.path.join(sdir, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size:
                out.append({"id": n[:-6], "path": p, "created": getattr(st, "st_birthtime", st.st_mtime),
                            "updated": st.st_mtime, "messages": 1})
    return out
def parse_openclaw_session(path: str, session_id: str, *, keep_text: bool = True,
                           tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    if path.endswith(".jsonl"):
        warnings: List[str] = []
        recs = list(iter_jsonl(path, warnings))
        s = pi.parse_pi_entries(recs, source=SOURCE, label=path, keep_text=keep_text,
                                tool_result_chars=tool_result_chars, id_hint=session_id, warnings=warnings)
        s.metadata["store"] = "jsonl"
        return s
    conn = _sqlite.snapshot(path)
    if "session_transcript_active_events" in _sqlite.tables(conn):
        sql = ("select e.event_json, e.created_at from transcript_events e join session_transcript_active_events a "
               "on a.session_id = e.session_id and a.event_seq = e.seq where e.session_id = ? "
               "order by a.active_position")
    else:
        sql = "select event_json, created_at from transcript_events where session_id = ? order by seq"
    recs: List[Dict[str, Any]] = []
    for raw, created in _sqlite.rows(conn, sql, (session_id,)):
        ev = _sqlite.loads(raw)
        if isinstance(ev, dict):
            if ev.get("timestamp") is None and created is not None:
                ev["timestamp"] = _sqlite.to_epoch(created)
            recs.append(ev)
    s = pi.parse_pi_entries(recs, source=SOURCE, label=path, keep_text=keep_text,
                            tool_result_chars=tool_result_chars, id_hint=session_id)
    s.warnings = [w for w in s.warnings if w != "no session header"]
    s.metadata["store"] = "sqlite"
    return s
def iter_sessions(root: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for ref in session_refs(root):
        yield parse_openclaw_session(ref["path"], ref["id"], **kwargs)
__all__ = ["parse_openclaw_session", "session_refs", "db_paths", "state_dir", "iter_sessions", "SOURCE"]
