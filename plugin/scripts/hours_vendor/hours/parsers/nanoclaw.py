from __future__ import annotations
import os
from typing import Any, Dict, Iterator, List, Optional
from ..model import Session, Turn, parse_ts
from . import _sqlite
from ._common import DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, set_command, set_text
SOURCE = "nanoclaw"
HUMAN_KINDS = ("chat", "chat-sdk")
SEARCH_PARENTS = ("", "dev", "code", "projects", "src")
def roots() -> List[str]:
    env = os.environ.get("NANOCLAW_ROOTS")
    cands: List[str] = []
    if env:
        cands += [os.path.expanduser(p) for p in env.split(os.pathsep) if p]
    home = os.path.expanduser("~")
    cands += [os.path.join(home, x) for x in ("nanoclaw", "nanoclaw-v2", os.path.join("dev", "nanoclaw"),
                                                 os.path.join("dev", "nanoclaw-v2"))]
    for parent in SEARCH_PARENTS:
        base = os.path.join(home, parent) if parent else home
        try:
            for n in sorted(os.listdir(base)):
                if not n.startswith("."):
                    cands.append(os.path.join(base, n))
        except OSError:
            continue
    out: List[str] = []
    seen = set()
    for c in cands:
        real = os.path.realpath(c)
        if real not in seen and os.path.isfile(os.path.join(real, "data", "v2.db")):
            seen.add(real)
            out.append(real)
    return out
def _session_dir(root: str, group: Any, sid: Any) -> Optional[str]:
    g, s = str(group or ""), str(sid or "")
    if not g or not s or any(x in g + s for x in ("/", "\\", "..")):
        return None
    d = os.path.join(root, "data", "v2-sessions", g, s)
    return d if os.path.isfile(os.path.join(d, "inbound.db")) and os.path.isfile(os.path.join(d, "outbound.db")) else None
def session_refs(root_list: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for root in root_list if root_list is not None else roots():
        try:
            conn = _sqlite.snapshot(os.path.join(root, "data", "v2.db"))
        except _sqlite.SnapshotError:
            continue
        if "sessions" not in _sqlite.tables(conn):
            continue
        has_mg = bool(_sqlite.columns(conn, "messaging_groups"))
        sql = ("select s.id, s.agent_group_id, s.thread_id, s.created_at, s.last_active, %s from sessions s %s" %
               (("coalesce(g.is_group, 0)", "left join messaging_groups g on g.id = s.messaging_group_id")
                if has_mg else ("0", "")))
        for sid, group, thread, created, last, is_group in _sqlite.rows(conn, sql):
            if str(thread or "").startswith("system:") or is_group:
                continue
            d = _session_dir(root, group, sid)
            c = _sqlite.to_epoch(created)
            if d is None or c is None:
                continue
            try:
                n = _sqlite.snapshot(os.path.join(d, "inbound.db")).execute(
                    "select count(*) from messages_in where kind in ('chat', 'chat-sdk') and source_session_id is null"
                ).fetchone()[0]
            except Exception:
                n = 0
            if n:
                out.append({"id": str(sid), "path": d, "created": c, "updated": _sqlite.to_epoch(last) or c,
                            "messages": int(n)})
    return out
def _body(content: Any) -> str:
    doc = _sqlite.loads(content)
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        for k in ("text", "content", "body", "message"):
            if isinstance(doc.get(k), str):
                return doc[k]
        return ""
    return content if isinstance(content, str) else ""
def parse_nanoclaw_session(folder: str, *, keep_text: bool = True,
                           tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    sid = os.path.basename(folder.rstrip(os.sep))
    s = Session(id=as_uuid(sid, SOURCE, sid), source=SOURCE)
    items = []
    types: Dict[str, int] = {}
    inc = _sqlite.snapshot(os.path.join(folder, "inbound.db"))
    for kind, ts, content, src in _sqlite.rows(
            inc, "select kind, timestamp, content, source_session_id from messages_in order by seq"):
        bump(types, "in:" + str(kind))
        if kind in HUMAN_KINDS and not src:
            items.append(("user", ts, _body(content)))
    out = _sqlite.snapshot(os.path.join(folder, "outbound.db"))
    ocols = _sqlite.columns(out, "messages_out")
    tcol = "timestamp" if "timestamp" in ocols else "created_at"
    for kind, ts, content in _sqlite.rows(out, "select kind, %s, content from messages_out order by seq" % tcol):
        bump(types, "out:" + str(kind))
        text = _body(content)
        if text.strip():
            items.append(("assistant", ts, text))
    items.sort(key=lambda x: (_sqlite.to_epoch(x[1]) or 0.0))
    for i, (role, ts, text) in enumerate(items):
        if role == "user":
            stripped = text.lstrip()
            kind = "command" if stripped.startswith("/") else "prompt"
            t = Turn(id="%s#%d" % (s.id, i + 1), role="user", kind=kind, timestamp=parse_ts(_sqlite.to_epoch(ts)))
            if kind == "command":
                set_command(t, stripped, keep_text=keep_text)
            else:
                set_text(t, text, keep_text=keep_text)
        else:
            t = Turn(id="%s#%d" % (s.id, i + 1), role="assistant", kind="response",
                     timestamp=parse_ts(_sqlite.to_epoch(ts)))
            set_text(t, text, keep_text=keep_text)
        s.turns.append(t)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = min(stamps) if stamps else None
    s.ended_at = max(stamps) if stamps else None
    s.metadata.update({"nanoclaw_session_id": sid, "event_types": types})
    return s
def iter_sessions(root_list: Optional[List[str]] = None, **kwargs: Any) -> Iterator[Session]:
    for ref in session_refs(root_list):
        yield parse_nanoclaw_session(ref["path"], **kwargs)
__all__ = ["parse_nanoclaw_session", "session_refs", "roots", "iter_sessions", "SOURCE"]
