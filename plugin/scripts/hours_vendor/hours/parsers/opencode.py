from __future__ import annotations
import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Session, Tokens, Turn, parse_ts
from . import _sqlite
from ._common import DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, json_size, make_tool, set_command, set_text
SOURCE = "opencode"
def data_dir() -> str:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = os.path.expanduser(xdg) if xdg and os.path.isabs(os.path.expanduser(xdg)) else os.path.expanduser("~/.local/share")
    return os.path.join(base, "opencode")
def db_path() -> str:
    env = os.environ.get("OPENCODE_DB")
    if env and env != ":memory:":
        p = os.path.expanduser(env)
        return p if os.path.isabs(p) else os.path.join(data_dir(), p)
    return os.path.join(data_dir(), "opencode.db")
def _ms(v: Any) -> Optional[float]:
    return _sqlite.to_epoch(v)
def db_paths() -> List[str]:
    out = [db_path()]
    try:
        for n in sorted(os.listdir(data_dir())):
            p = os.path.join(data_dir(), n)
            if re.match(r"^opencode-[A-Za-z0-9_.-]+\.db$", n) and os.path.isfile(p) and p not in out:
                out.append(p)
    except OSError:
        pass
    return out
def session_refs(path: Optional[str] = None) -> List[Dict[str, Any]]:
    if path is None:
        out: List[Dict[str, Any]] = []
        for p in db_paths():
            if os.path.isfile(p):
                out.extend(_refs_one(p))
        return out or _refs_one(db_path())
    return _refs_one(path)
def _refs_one(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if os.path.isfile(path):
        try:
            conn = _sqlite.snapshot(path)
        except _sqlite.SnapshotError:
            return out
        cols = _sqlite.columns(conn, "session")
        if not cols:
            return out
        pcol = "parent_id" if "parent_id" in cols else "null"
        counts = dict(_sqlite.rows(conn, "select session_id, count(*) from message group by session_id")) \
            if _sqlite.columns(conn, "message") else {}
        for sid, par, created, updated in _sqlite.rows(
                conn, "select id, %s, time_created, time_updated from session order by time_created" % pcol):
            if par:
                continue
            out.append({"id": str(sid), "path": path, "created": _ms(created), "updated": _ms(updated) or _ms(created),
                        "messages": int(counts.get(sid) or 0)})
        return out
    root = os.path.join(data_dir(), "storage", "session")
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return out
    for proj in projects:
        pdir = os.path.join(root, proj)
        try:
            names = sorted(os.listdir(pdir))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json"):
                continue
            fp = os.path.join(pdir, n)
            try:
                st = os.stat(fp)
                with open(fp, "rb") as fh:
                    info = json.loads(fh.read(65536).decode("utf-8", "replace"))
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict) or info.get("parentID"):
                continue
            tm = info.get("time") if isinstance(info.get("time"), dict) else {}
            mdir = os.path.join(data_dir(), "storage", "message", n[:-5])
            try:
                cnt = len(os.listdir(mdir))
            except OSError:
                cnt = 0
            out.append({"id": str(info.get("id") or n[:-5]), "path": fp, "created": _ms(tm.get("created")) or st.st_mtime,
                        "updated": max(_ms(tm.get("updated")) or 0, st.st_mtime), "messages": cnt})
    return out
class _Build:
    def __init__(self, keep_text: bool, limit: Optional[int]) -> None:
        self.keep_text, self.limit = keep_text, limit
        self.turns: List[Turn] = []
        self.calls: Dict[str, Turn] = {}
        self.n = 0
        self.types: Dict[str, int] = {}
        self.cwd: Optional[str] = None
        self.models: Dict[str, int] = {}
    def add(self, role: str, kind: str, ts: Any, mid: Optional[str], model: Optional[str] = None) -> Turn:
        self.n += 1
        t = Turn(id="#%d" % self.n, role=role, kind=kind, timestamp=parse_ts(ts), message_id=mid)
        if role == "assistant":
            t.model = model
        self.turns.append(t)
        return t
    def message(self, mid: str, created: Any, data: Dict[str, Any], parts: List[Dict[str, Any]]) -> None:
        role = data.get("role")
        tm = data.get("time") if isinstance(data.get("time"), dict) else {}
        ts = tm.get("created") if tm.get("created") is not None else created
        mdl = data.get("modelID") if isinstance(data.get("modelID"), str) else None
        if not mdl and isinstance(data.get("model"), dict):
            mdl = data["model"].get("modelID") if isinstance(data["model"].get("modelID"), str) else None
        path = data.get("path") if isinstance(data.get("path"), dict) else {}
        if isinstance(path.get("cwd"), str) and path["cwd"] and not self.cwd:
            self.cwd = path["cwd"]
        if mdl and role == "assistant":
            bump(self.models, mdl)
        first: Optional[Turn] = None
        if role == "user":
            text = "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text" and not p.get("synthetic")
                             and not p.get("ignored"))
            synth = [p for p in parts if p.get("type") == "text" and p.get("synthetic")]
            if text.strip() or not synth:
                stripped = text.lstrip()
                t = self.add("user", "command" if stripped.startswith("/") else "prompt", ts, mid)
                if t.kind == "command":
                    set_command(t, stripped, keep_text=self.keep_text)
                else:
                    set_text(t, text, keep_text=self.keep_text)
            if synth:
                m = self.add("system", "meta", ts, mid)
                set_text(m, "\n".join(str(p.get("text") or "") for p in synth), keep_text=False)
            for p in parts:
                if p.get("type") == "subtask":
                    s = self.add("user", "meta", ts, mid)
                    s.extra["subtask_agent"] = str(p.get("agent") or "")[:64]
            return
        if role != "assistant":
            return
        for p in parts:
            pt = p.get("type")
            pts = ((p.get("time") or {}).get("start") if isinstance(p.get("time"), dict) else None) or ts
            if pt == "text":
                if not str(p.get("text") or "").strip():
                    continue
                t = self.add("assistant", "response", pts, mid, mdl)
                set_text(t, str(p["text"]), keep_text=self.keep_text)
            elif pt == "reasoning":
                t = self.add("assistant", "thinking", pts, mid, mdl)
                t.char_count = len(str(p.get("text") or ""))
            elif pt == "tool":
                st = p.get("state") if isinstance(p.get("state"), dict) else {}
                tt = st.get("time") if isinstance(st.get("time"), dict) else {}
                nm = str(p.get("tool") or "unknown")
                cid = str(p.get("callID") or p.get("id") or "") or None
                t = self.add("assistant", "tool_call", tt.get("start") or pts, mid, mdl)
                m = re.match(r"^mcp_([^_]+)_", nm)
                t.tool = make_tool(nm, call_id=cid, args=st.get("input"), mcp_server=m.group(1) if m else None)
                t.char_count = json_size(st.get("input"))
                status = st.get("status")
                if status in ("completed", "error") or st.get("output") is not None or st.get("error"):
                    r = self.add("tool_result", "tool_result", tt.get("end") or tt.get("start") or pts, mid)
                    r.tool = make_tool(nm, call_id=cid, mcp_server=t.tool.mcp_server)
                    r.tool.is_error = status == "error"
                    out = st.get("output") if st.get("output") is not None else st.get("error")
                    set_text(r, out if isinstance(out, str) else json.dumps(out) if out is not None else "",
                             keep_text=self.keep_text, limit=self.limit)
                elif status in ("pending", "running"):
                    t.tool.is_interrupted = True
                if cid:
                    self.calls[cid] = t
            elif pt == "subtask" or pt == "agent":
                t = self.add("system", "meta", pts, mid)
                t.extra["agent"] = str(p.get("agent") or p.get("name") or "")[:64]
            elif pt == "compaction":
                t = self.add("system", "compaction", pts, mid)
                t.extra.update({"boundary": True, "trigger": "opencode"})
            elif pt == "retry":
                t = self.add("system", "error", pts, mid)
            else:
                continue
            first = first or (t if pt in ("text", "reasoning", "tool") else None)
        tok = data.get("tokens") if isinstance(data.get("tokens"), dict) else None
        if tok is None:
            steps = [p.get("tokens") for p in parts if p.get("type") == "step-finish" and isinstance(p.get("tokens"), dict)]
            if steps:
                tok = {"input": sum(_i(s.get("input")) for s in steps), "output": sum(_i(s.get("output")) for s in steps),
                       "reasoning": sum(_i(s.get("reasoning")) for s in steps),
                       "cache": {"read": sum(_i((s.get("cache") or {}).get("read")) for s in steps),
                                 "write": sum(_i((s.get("cache") or {}).get("write")) for s in steps)}}
        if tok is not None:
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            tk = Tokens(input=_i(tok.get("input")), output=_i(tok.get("output")) + _i(tok.get("reasoning")),
                        cache_read=_i(cache.get("read")), cache_creation=_i(cache.get("write")))
            if tk.input or tk.output or tk.cache_read or tk.cache_creation:
                if first is None:
                    first = self.add("assistant", "response", ts, mid, mdl)
                first.tokens = tk
        err = data.get("error")
        if isinstance(err, dict):
            kind = "interrupt" if str(err.get("name") or "") == "MessageAbortedError" else "error"
            e = self.add("system", kind, tm.get("completed") or ts, mid)
            e.char_count = len(json.dumps(err.get("data") or {}))
def _i(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
def _finish(b: _Build, sid: str, title: Any, directory: Any, created: Any, updated: Any, label: str) -> Session:
    s = Session(id=as_uuid(sid, SOURCE, label, sid), source=SOURCE)
    s.title = title if isinstance(title, str) and title else None
    s.cwd = directory if isinstance(directory, str) and directory else b.cwd
    s.turns = b.turns
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (s.id, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = parse_ts(created) or (min(stamps) if stamps else None)
    s.ended_at = max([x for x in (parse_ts(updated),) if x] + stamps) if (stamps or updated) else s.started_at
    s.metadata.update({"opencode_session_id": sid, "encoded_project_dir": encode_project_dir(s.cwd),
                       "models": b.models})
    return s
def _parse_db(conn: Any, sid: str, keep_text: bool, limit: Optional[int], label: str) -> Session:
    row = next(iter(_sqlite.rows(conn, "select title, directory, time_created, time_updated from session where id = ?",
                                 (sid,))), (None, None, None, None))
    b = _Build(keep_text, limit)
    pcols = _sqlite.columns(conn, "part")
    parts_by_msg: Dict[str, List[Dict[str, Any]]] = {}
    if pcols:
        for mid, data in _sqlite.rows(conn, "select message_id, data from part where session_id = ? order by id", (sid,)):
            d = _sqlite.loads(data)
            if isinstance(d, dict):
                parts_by_msg.setdefault(str(mid), []).append(d)
    for mid, created, data in _sqlite.rows(
            conn, "select id, time_created, data from message where session_id = ? order by time_created, id", (sid,)):
        d = _sqlite.loads(data)
        if isinstance(d, dict):
            inline = d.get("parts") if isinstance(d.get("parts"), list) else []
            b.message(str(mid), created, d, parts_by_msg.get(str(mid)) or [p for p in inline if isinstance(p, dict)])
    s = _finish(b, sid, row[0], row[1], row[2], row[3], label)
    kids = [str(k) for (k,) in _sqlite.rows(conn, "select id from session where parent_id = ? order by time_created",
                                            (sid,))] if "parent_id" in _sqlite.columns(conn, "session") else []
    for kid in kids[:200]:
        sub = _parse_db(conn, kid, keep_text, limit, label)
        sub.parent_session_id, sub.agent_id = s.id, kid
        for t in sub.turns:
            t.is_sidechain, t.agent_id = True, kid
        s.subagents.append(sub)
    return s
def _parse_files(info_path: str, keep_text: bool, limit: Optional[int]) -> Session:
    def load(p: str) -> Any:
        try:
            with open(p, "rb") as fh:
                return json.loads(fh.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None
    info = load(info_path) or {}
    sid = str(info.get("id") or os.path.basename(info_path)[:-5])
    storage = os.path.dirname(os.path.dirname(os.path.dirname(info_path)))
    b = _Build(keep_text, limit)
    msgs: List[Tuple[float, str, Dict[str, Any]]] = []
    mdir = os.path.join(storage, "message", sid)
    try:
        names = sorted(os.listdir(mdir))
    except OSError:
        names = []
    for n in names:
        d = load(os.path.join(mdir, n))
        if isinstance(d, dict):
            tm = d.get("time") if isinstance(d.get("time"), dict) else {}
            msgs.append((float(tm.get("created") or 0), str(d.get("id") or n[:-5]), d))
    for created, mid, d in sorted(msgs, key=lambda x: (x[0], x[1])):
        pdir = os.path.join(storage, "part", mid)
        parts: List[Dict[str, Any]] = []
        try:
            for pn in sorted(os.listdir(pdir)):
                pd = load(os.path.join(pdir, pn))
                if isinstance(pd, dict):
                    parts.append(pd)
        except OSError:
            pass
        b.message(mid, created, d, parts)
    tm = info.get("time") if isinstance(info.get("time"), dict) else {}
    return _finish(b, sid, info.get("title"), info.get("directory"), tm.get("created"), tm.get("updated"), info_path)
def parse_opencode_session(path: str, session_id: Optional[str] = None, *, keep_text: bool = True,
                           tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    if path.endswith(".json"):
        return _parse_files(path, keep_text, tool_result_chars)
    conn = _sqlite.snapshot(path)
    return _parse_db(conn, str(session_id), keep_text, tool_result_chars, path)
def iter_sessions(path: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for ref in session_refs(path):
        yield parse_opencode_session(ref["path"], ref["id"], **kwargs)
__all__ = ["parse_opencode_session", "session_refs", "db_paths", "db_path", "data_dir", "iter_sessions", "SOURCE"]
