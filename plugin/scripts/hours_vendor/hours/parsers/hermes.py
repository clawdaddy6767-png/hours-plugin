from __future__ import annotations
import json
import os
from typing import Any, Dict, Iterator, List, Optional
from ..model import Session, Tokens, Turn, parse_ts
from . import _sqlite
from ._common import DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, json_size, make_tool, parse_args, set_command, set_text
SOURCE = "hermes"
OWN_SOURCES = ("cli", "tui", "desktop", "terminal", "local", "acp")
NEVER_SELECTED = ("user_id", "system_prompt", "title")
def hermes_home() -> str:
    return os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")
def db_path() -> str:
    return os.path.join(hermes_home(), "state.db")
def session_refs(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or db_path()
    out: List[Dict[str, Any]] = []
    if not os.path.isfile(path):
        return out
    try:
        conn = _sqlite.snapshot(path)
    except _sqlite.SnapshotError:
        return out
    cols = _sqlite.columns(conn, "sessions")
    if not {"id", "source", "started_at"} <= set(cols) or not _sqlite.columns(conn, "messages"):
        return out
    ph = ",".join("?" for _ in OWN_SOURCES)
    last = "(select max(m.timestamp) from messages m where m.session_id = sessions.id)"
    cnt = "(select count(*) from messages m where m.session_id = sessions.id)"
    ended = "ended_at" if "ended_at" in cols else "null"
    for sid, started, end, lastts, n in _sqlite.rows(
            conn, "select id, started_at, %s, %s, %s from sessions where lower(source) in (%s)" % (ended, last, cnt, ph),
            tuple(OWN_SOURCES)):
        s0 = _sqlite.to_epoch(started)
        if s0 is None or not n:
            continue
        out.append({"id": str(sid), "path": path, "created": s0,
                    "updated": max([x for x in (_sqlite.to_epoch(end), _sqlite.to_epoch(lastts), s0) if x]),
                    "messages": int(n)})
    return out
def _calls(raw: Any) -> List[Dict[str, Any]]:
    v = _sqlite.loads(raw) if isinstance(raw, (str, bytes)) else raw
    out: List[Dict[str, Any]] = []
    for c in v if isinstance(v, list) else []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else {}
        nm = c.get("name") or fn.get("name")
        if isinstance(nm, str) and nm:
            out.append({"id": c.get("id") if isinstance(c.get("id"), str) else None, "name": nm,
                        "args": parse_args(c.get("arguments") if c.get("arguments") is not None else fn.get("arguments"))})
    return out
def _parse_one(conn: Any, sid: str, keep_text: bool, limit: Optional[int], label: str) -> Session:
    scols = _sqlite.columns(conn, "sessions")
    mcols = _sqlite.columns(conn, "messages")
    want = [c for c in ("source", "parent_session_id", "started_at", "ended_at", "model", "model_config",
                        "end_reason", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                        "reasoning_tokens", "cwd") if c in scols]
    row = next(iter(_sqlite.rows(conn, "select %s from sessions where id = ?" % ", ".join(want), (sid,))), None)
    info = dict(zip(want, row)) if row else {}
    s = Session(id=as_uuid(sid, SOURCE, label, sid), source=SOURCE)
    s.cwd = info.get("cwd") if isinstance(info.get("cwd"), str) and info.get("cwd") else None
    started = _sqlite.to_epoch(info.get("started_at"))
    cfg = _sqlite.loads(info.get("model_config")) if info.get("model_config") else None
    branched = (isinstance(cfg, dict) and bool(cfg.get("_branched_from"))) or info.get("end_reason") == "branched"
    model = info.get("model") if isinstance(info.get("model"), str) and info.get("model") else None
    reasoning_cols = [c for c in ("reasoning", "reasoning_content", "reasoning_details") if c in mcols]
    rexpr = " or ".join("nullif(%s, '') is not null" % c for c in reasoning_cols) or "0"
    active = " and coalesce(active, 1) != 0" if "active" in mcols else ""
    scheduled = False
    types: Dict[str, int] = {}
    pending: Dict[str, Turn] = {}
    n = 0
    first_assistant: Optional[Turn] = None
    for role, content, calls, cid, tname, ts, has_r in _sqlite.rows(
            conn, "select role, content, tool_calls, tool_call_id, tool_name, timestamp, (%s) from messages "
                  "where session_id = ?%s order by timestamp, id" % (rexpr, active), (sid,)):
        t0 = _sqlite.to_epoch(ts)
        if branched and started is not None and t0 is not None and t0 < started:
            continue
        bump(types, str(role))
        text = content if isinstance(content, str) else ""
        def add(r: str, kind: str) -> Turn:
            nonlocal n
            n += 1
            t = Turn(id="#%d" % n, role=r, kind=kind, timestamp=parse_ts(t0))
            s.turns.append(t)
            return t
        if role == "user":
            stripped = text.lstrip()
            kind = "command" if stripped.startswith("/") else "prompt"
            t = add("user", kind)
            if kind == "command":
                set_command(t, stripped, keep_text=keep_text)
            else:
                set_text(t, text, keep_text=keep_text)
        elif role == "assistant":
            if has_r:
                th = add("assistant", "thinking")
                th.model = model
                first_assistant = first_assistant or th
            if text.strip():
                r = add("assistant", "response")
                r.model = model
                set_text(r, text, keep_text=keep_text)
                first_assistant = first_assistant or r
            for c in _calls(calls):
                tc = add("assistant", "tool_call")
                tc.model = model
                tc.tool = make_tool(c["name"], call_id=c["id"], args=c["args"])
                tc.char_count = json_size(c["args"])
                if c["id"]:
                    pending[c["id"]] = tc
                first_assistant = first_assistant or tc
        elif role == "tool":
            call = pending.get(str(cid or ""))
            r = add("tool_result", "tool_result")
            r.tool = make_tool(str(tname or (call.tool.name if call and call.tool else "unknown")),
                               call_id=str(cid) if cid else None)
            res = _sqlite.loads(text)
            r.tool.is_error = isinstance(res, dict) and (res.get("success") is False or res.get("is_error") is True)
            set_text(r, text, keep_text=keep_text, limit=limit)
    tk = Tokens(input=_i(info.get("input_tokens")), output=_i(info.get("output_tokens")) + _i(info.get("reasoning_tokens")),
                cache_read=_i(info.get("cache_read_tokens")), cache_creation=_i(info.get("cache_write_tokens")))
    if (tk.input or tk.output or tk.cache_read or tk.cache_creation) and first_assistant is not None:
        first_assistant.tokens = tk
        first_assistant.extra["tokens_scope"] = "session_total"
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (s.id, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = parse_ts(started) or (min(stamps) if stamps else None)
    end = parse_ts(info.get("ended_at"))
    s.ended_at = max([x for x in [end] if x] + stamps) if (stamps or end) else s.started_at
    s.metadata.update({"hermes_session_id": sid, "event_types": types, "origin": info.get("source"),
                       "end_reason": info.get("end_reason"), "branched": branched, "scheduled": scheduled,
                       "encoded_project_dir": encode_project_dir(s.cwd)})
    return s
def _i(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
def parse_hermes_session(path: str, session_id: str, *, keep_text: bool = True,
                         tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    conn = _sqlite.snapshot(path)
    s = _parse_one(conn, session_id, keep_text, tool_result_chars, path)
    if "parent_session_id" in _sqlite.columns(conn, "sessions"):
        for (kid,) in list(_sqlite.rows(
                conn, "select id from sessions where parent_session_id = ? and lower(source) = 'subagent' "
                      "order by started_at", (session_id,)))[:200]:
            sub = _parse_one(conn, str(kid), keep_text, tool_result_chars, path)
            sub.parent_session_id, sub.agent_id = s.id, str(kid)
            for t in sub.turns:
                t.is_sidechain, t.agent_id = True, str(kid)
            s.subagents.append(sub)
    return s
def iter_sessions(path: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for ref in session_refs(path):
        yield parse_hermes_session(ref["path"], ref["id"], **kwargs)
__all__ = ["parse_hermes_session", "session_refs", "db_path", "hermes_home", "iter_sessions", "SOURCE", "OWN_SOURCES",
           "NEVER_SELECTED"]
