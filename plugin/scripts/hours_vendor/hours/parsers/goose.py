from __future__ import annotations
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from . import _sqlite
from ._common import (DEFAULT_TOOL_RESULT_CHARS, bump, encode_project_dir, json_size, make_tool, set_text, stable_uuid,
                      tool_category)
SOURCE = "goose"
SKIP_TYPES = ("sub_agent", "hidden")
NEVER_READ = ("config.yaml", "secrets.yaml", "permission.yaml")
def goose_data_dir(home: Optional[str] = None) -> str:
    root = os.environ.get("GOOSE_PATH_ROOT")
    if root and os.path.isabs(root):
        return os.path.join(root, "data")
    home = home or os.path.expanduser("~")
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(appdata, "Block", "goose", "data")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "goose")
def db_path(home: Optional[str] = None) -> Optional[str]:
    p = os.path.join(goose_data_dir(home), "sessions", "sessions.db")
    return p if os.path.isfile(p) else None
def _connect(path: str) -> sqlite3.Connection:
    con = _sqlite.snapshot(path)
    con.row_factory = sqlite3.Row
    return con
def _release(con: sqlite3.Connection) -> None:
    con.row_factory = None
def _cols(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
def session_rows(path: Optional[str] = None) -> List[Tuple[str, str, int, int]]:
    path = path or db_path()
    if not path:
        return []
    try:
        con = _connect(path)
    except (sqlite3.Error, _sqlite.SnapshotError):
        return []
    try:
        cols = _cols(con, "sessions")
        where = []
        if "session_type" in cols:
            where.append("COALESCE(s.session_type,'user') NOT IN (%s)" % ",".join("'%s'" % t for t in SKIP_TYPES))
        if "parent_session_id" in cols:
            where.append("(s.parent_session_id IS NULL OR s.parent_session_id = '')")
        sql = ("SELECT s.id, COALESCE(s.updated_at,''), COUNT(m.id), COALESCE(SUM(LENGTH(m.content_json)),0) "
               "FROM sessions s JOIN messages m ON m.session_id = s.id %s GROUP BY s.id"
               % (("WHERE " + " AND ".join(where)) if where else ""))
        return [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in con.execute(sql)]
    except sqlite3.Error:
        return []
    finally:
        _release(con)
_BUILTIN = ("developer", "computercontroller", "memory", "to" "do", "chatrecall", "summon", "analyze", "orchestrator")
_TOOL_WORDS = (("shell", "shell"), ("command", "shell"), ("text_editor", "file_edit"), ("write", "file_edit"),
               ("edit", "file_edit"), ("tree", "file_read"), ("analyze", "file_read"), ("read", "file_read"),
               ("view", "file_read"), ("list", "file_read"), ("to" "do", "plan"), ("search", "search"), ("grep", "search"),
               ("fetch", "web"), ("scrape", "web"), ("browser", "web"), ("delegate", "agent"), ("subagent", "agent"))
def _categorize(name: str) -> Tuple[str, Optional[str]]:
    ext, sep, tool = name.partition("__")
    if not sep:
        return tool_category(name), None
    if ext in _BUILTIN:
        for word, cat in _TOOL_WORDS:
            if word in tool.lower() or word in ext.lower():
                return cat, None
        return "other", None
    return "mcp", ext
def _blocks(content_json: str) -> List[Dict[str, Any]]:
    try:
        v = json.loads(content_json)
    except ValueError:
        return []
    if isinstance(v, dict):
        v = [v]
    return [b for b in v if isinstance(b, dict)] if isinstance(v, list) else []
def _result_text(res: Any) -> Tuple[str, bool]:
    if not isinstance(res, dict):
        return "", False
    if res.get("status") == "error":
        return str(res.get("error") or ""), True
    val = res.get("value")
    err = False
    items: Any = val
    if isinstance(val, dict):
        err = bool(val.get("isError"))
        items = val.get("content")
    if isinstance(items, list):
        return "\n".join(str(i.get("text") or "") for i in items if isinstance(i, dict) and i.get("type") == "text"), err
    return "", err
def _model_of(cfg_json: Any) -> Optional[str]:
    if isinstance(cfg_json, str) and cfg_json[:1] == "{":
        try:
            v = json.loads(cfg_json)
        except ValueError:
            return None
        if isinstance(v, dict) and isinstance(v.get("model_name"), str):
            return v["model_name"]
    return None
def parse_goose_session(session_id: str, path: Optional[str] = None, *, keep_text: bool = True,
                        tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    path = path or db_path() or ""
    sid = stable_uuid("goose", session_id)
    s = Session(id=sid, source=SOURCE)
    warnings: List[str] = []
    try:
        con = _connect(path)
    except (sqlite3.Error, _sqlite.SnapshotError):
        s.warnings.append("sessions.db could not be opened")
        return s
    types: Dict[str, int] = {}
    n = 0
    calls: Dict[str, Turn] = {}
    assistants: List[Turn] = []
    cur: Dict[str, Any] = {"ts": None, "used": None}
    def add(r_: str, kind: str) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=r_, kind=kind, timestamp=parse_ts(cur["ts"]))
        if r_ == "assistant":
            used = cur["used"]
            t.model = used if isinstance(used, str) else None
            assistants.append(t)
        s.turns.append(t)
        return t
    try:
        row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        info = dict(row) if row is not None else {}
        model = _model_of(info.get("model_config_json"))
        s.title = info.get("name") or None
        wd = info.get("working_dir")
        s.cwd = wd if isinstance(wd, str) and wd else None
        s.project = os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else None
        s.started_at = parse_ts(info.get("created_at")) if info.get("created_at") else None
        for r in con.execute("SELECT role, content_json, created_timestamp, metadata_json FROM messages "
                             "WHERE session_id = ? ORDER BY created_timestamp, id", (session_id,)):
            role = str(r["role"] or "")
            ts = r["created_timestamp"]
            meta: Dict[str, Any] = {}
            try:
                mj = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                meta = mj if isinstance(mj, dict) else {}
            except ValueError:
                pass
            inf = meta.get("inference") if isinstance(meta.get("inference"), dict) else {}
            used = inf.get("resolved_model") or inf.get("resolvedModel") or inf.get("requested_model") \
                or inf.get("requestedModel") or model
            for b in _blocks(r["content_json"]):
                bt = str(b.get("type") or "")
                bump(types, "%s:%s" % (role, bt))
                cur["ts"], cur["used"] = ts, used
                if bt == "text":
                    text = str(b.get("text") or "")
                    if role == "user":
                        if meta.get("userVisible") is False or meta.get("user_visible") is False:
                            continue
                        stripped = text.lstrip()
                        t = add("user", "command" if stripped.startswith("/") else "prompt")
                        set_text(t, text, keep_text=keep_text)
                    elif text.strip():
                        set_text(add("assistant", "response"), text, keep_text=keep_text)
                elif bt in ("thinking", "redactedThinking"):
                    add("assistant", "thinking").char_count = len(str(b.get("thinking") or ""))
                elif bt == "toolRequest":
                    tc = b.get("toolCall") if isinstance(b.get("toolCall"), dict) else {}
                    val = tc.get("value") if isinstance(tc.get("value"), dict) else {}
                    cid = str(b.get("id") or "") or None
                    c = add("assistant", "tool_call")
                    name = str(val.get("name") or "unknown")
                    c.tool = make_tool(name, call_id=cid, args=val.get("arguments"))
                    c.tool.category, c.tool.mcp_server = _categorize(name)
                    c.char_count = json_size(val.get("arguments"))
                    if tc.get("status") == "error":
                        c.tool.is_error = True
                    if cid:
                        calls[cid] = c
                elif bt == "toolResponse":
                    cid = str(b.get("id") or "") or None
                    call = calls.get(cid or "")
                    text, err = _result_text(b.get("toolResult"))
                    t = add("tool_result", "tool_result")
                    t.tool = make_tool(call.tool.name if call and call.tool else "unknown", call_id=cid)
                    if call and call.tool:
                        t.tool.category = call.tool.category
                        t.tool.mcp_server = call.tool.mcp_server
                    t.tool.is_error = err
                    set_text(t, text, keep_text=keep_text, limit=tool_result_chars)
                elif bt == "error":
                    add("system", "error").char_count = len(str(b.get("error") or ""))
                elif bt == "image" and role == "user" and s.turns and s.turns[-1].role == "user":
                    s.turns[-1].attachments.append(Attachment(kind="image", mime=b.get("mimeType")))
        ledger: List[sqlite3.Row] = []
        try:
            ledger = list(con.execute("SELECT created_timestamp, model, input_tokens, output_tokens, cache_read_tokens, "
                                      "cache_write_tokens, COALESCE(is_compaction,0) AS comp FROM usage_ledger "
                                      "WHERE session_id = ? ORDER BY created_timestamp, id", (session_id,)))
        except sqlite3.Error:
            ledger = []
        if ledger and assistants:
            for lr in ledger:
                lt = parse_ts(lr["created_timestamp"])
                target = next((a for a in assistants if a.tokens is None and (lt is None or a.timestamp is None
                                                                               or a.timestamp >= lt)), None)
                target = target or assistants[-1]
                tk = Tokens(input=int(lr["input_tokens"] or 0), output=int(lr["output_tokens"] or 0),
                            cache_read=int(lr["cache_read_tokens"] or 0), cache_creation=int(lr["cache_write_tokens"] or 0))
                target.tokens = tk if target.tokens is None else target.tokens + tk
        elif assistants and info:
            tk = Tokens(input=int(info.get("accumulated_input_tokens") or 0),
                        output=int(info.get("accumulated_output_tokens") or 0),
                        cache_read=int(info.get("accumulated_cache_read_tokens") or 0),
                        cache_creation=int(info.get("accumulated_cache_write_tokens") or 0))
            if tk.total:
                assistants[0].tokens = tk
        s.metadata.update({"provider": info.get("provider_name"), "session_type": info.get("session_type"),
                           "goose_mode": info.get("goose_mode"), "model": model, "cost_usd": info.get("accumulated_cost")})
    except sqlite3.Error as exc:
        warnings.append("database error: %s" % type(exc).__name__)
    finally:
        _release(con)
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    if stamps:
        s.started_at = min(stamps)
        s.ended_at = max(stamps)
    s.metadata.update({"event_types": types, "session_row_id": session_id,
                       "encoded_project_dir": encode_project_dir(s.cwd)})
    s.warnings.extend(warnings)
    return s
def iter_sessions(path: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for sid, _u, _n, _b in session_rows(path):
        yield parse_goose_session(sid, path, **kwargs)
__all__ = ["parse_goose_session", "session_rows", "db_path", "goose_data_dir", "iter_sessions", "SOURCE", "NEVER_READ"]
