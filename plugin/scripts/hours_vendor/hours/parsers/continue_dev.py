from __future__ import annotations
import datetime as _dt
import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, json_size, make_tool, set_command,
                      set_text)
SOURCE = "continue_dev"
NEVER_READ = ("config.yaml", "config.json", ".env", "index", "dev_data")
def continue_home() -> str:
    return os.path.expanduser(os.environ.get("CONTINUE_GLOBAL_DIR") or "~/.continue")
def session_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    root = os.path.join(home or continue_home(), "sessions")
    out: List[Tuple[str, str]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and n != "sessions.json":
            p = os.path.join(root, n)
            if os.path.isfile(p):
                out.append((n[:-5], p))
    return out
def _read_json(path: str) -> Any:
    try:
        with open(path, "rb") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""
def _images(content: Any) -> int:
    if isinstance(content, list):
        return sum(1 for p in content if isinstance(p, dict) and p.get("type") in ("imageUrl", "image", "image_url"))
    return 0
def _usage(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    det = u.get("promptTokensDetails") if isinstance(u.get("promptTokensDetails"), dict) else {}
    cached = int(det.get("cachedTokens") or 0)
    prompt = int(u.get("promptTokens") or 0)
    tk = Tokens(input=max(0, prompt - cached), output=int(u.get("completionTokens") or 0), cache_read=cached,
                cache_creation=int(det.get("cacheWriteTokens") or 0))
    return tk if tk.total else None
def _created(session_id: str, home: str) -> Optional[_dt.datetime]:
    lst = _read_json(os.path.join(home, "sessions", "sessions.json"))
    if isinstance(lst, list):
        for it in lst:
            if isinstance(it, dict) and it.get("sessionId") == session_id:
                v = it.get("dateCreated")
                if isinstance(v, str) and v.isdigit():
                    v = int(v)
                return parse_ts(v)
    return None
def parse_continue_session(path: str, *, keep_text: bool = True,
                           tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    warnings: List[str] = []
    doc = _read_json(path)
    if not isinstance(doc, dict):
        doc = {}
        warnings.append("session file unreadable or not an object")
    file_id = os.path.basename(path)[:-5]
    sid = as_uuid(doc.get("sessionId") or file_id, "continue_dev", file_id)
    s = Session(id=sid, source=SOURCE)
    s.title = doc.get("title") if isinstance(doc.get("title"), str) else None
    cwd = doc.get("workspaceDirectory")
    if isinstance(cwd, str) and cwd:
        s.cwd = cwd[len("file://"):] if cwd.startswith("file://") else cwd
    model = doc.get("chatModelTitle") if isinstance(doc.get("chatModelTitle"), str) else None
    history = doc.get("history") if isinstance(doc.get("history"), list) else []
    types: Dict[str, int] = {}
    calls: Dict[str, Turn] = {}
    n = 0
    tool_msg_ids = {str((h.get("message") or {}).get("toolCallId")) for h in history
                    if isinstance(h, dict) and isinstance(h.get("message"), dict)
                    and h["message"].get("role") == "tool"}
    def add(role: str, kind: str) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind)
        if role == "assistant":
            t.model = model
        s.turns.append(t)
        return t
    def call(cid: Optional[str], name: Any, args: Any) -> Turn:
        c = add("assistant", "tool_call")
        c.tool = make_tool(name or "unknown", call_id=cid, args=args)
        c.char_count = json_size(args)
        if cid:
            calls[cid] = c
        return c
    for h in history:
        if not isinstance(h, dict) or not isinstance(h.get("message"), dict):
            continue
        m = h["message"]
        role = str(m.get("role") or "")
        bump(types, role or "norole")
        if role == "user":
            text = _text(m.get("content"))
            stripped = text.lstrip()
            kind = "command" if stripped.startswith("/") else "prompt"
            t = add("user", kind)
            if kind == "command":
                set_command(t, stripped, keep_text=keep_text)
            else:
                set_text(t, text, keep_text=keep_text)
            for _ in range(_images(m.get("content"))):
                t.attachments.append(Attachment(kind="image"))
            for ci in h.get("contextItems") or []:
                if isinstance(ci, dict):
                    c = ci.get("content")
                    t.attachments.append(Attachment(kind="file", extracted_chars=len(c) if isinstance(c, str) else None))
        elif role == "thinking":
            th = add("assistant", "thinking")
            th.char_count = len(_text(m.get("content")))
        elif role == "assistant":
            first: Optional[Turn] = None
            text = _text(m.get("content"))
            if text.strip():
                r = add("assistant", "response")
                set_text(r, text, keep_text=keep_text)
                first = r
            seen = set()
            for tc in m.get("toolCalls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                cid = str(tc.get("id") or "") or None
                c = call(cid, fn.get("name"), fn.get("arguments"))
                first = first or c
                if cid:
                    seen.add(cid)
            for st in h.get("toolCallStates") or []:
                if not isinstance(st, dict):
                    continue
                cid = str(st.get("toolCallId") or "") or None
                tcall = st.get("toolCall") if isinstance(st.get("toolCall"), dict) else {}
                fn = tcall.get("function") if isinstance(tcall.get("function"), dict) else {}
                if cid not in seen:
                    c = call(cid, fn.get("name"), st.get("parsedArgs") if st.get("parsedArgs") is not None
                             else fn.get("arguments"))
                    first = first or c
                if cid and cid not in tool_msg_ids and isinstance(st.get("output"), list):
                    r = add("tool_result", "tool_result")
                    r.tool = make_tool(fn.get("name") or "unknown", call_id=cid)
                    r.tool.category = calls[cid].tool.category if cid in calls else r.tool.category
                    r.tool.is_error = st.get("status") in ("errored", "error")
                    r.tool.is_rejected = st.get("status") == "canceled"
                    set_text(r, "\n".join(str(o.get("content") or "") for o in st["output"] if isinstance(o, dict)),
                             keep_text=keep_text, limit=tool_result_chars)
            tk = _usage(m.get("usage"))
            if tk is not None:
                first = first or add("assistant", "response")
                first.tokens = tk
        elif role == "tool":
            cid = str(m.get("toolCallId") or "") or None
            c = calls.get(cid or "")
            r = add("tool_result", "tool_result")
            r.tool = make_tool(c.tool.name if c and c.tool else "unknown", call_id=cid)
            if c and c.tool:
                r.tool.category = c.tool.category
            set_text(r, _text(m.get("content")), keep_text=keep_text, limit=tool_result_chars)
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    home = os.path.dirname(os.path.dirname(path))
    s.started_at = _created(file_id, home) or _created(str(doc.get("sessionId") or ""), home)
    try:
        s.ended_at = _dt.datetime.fromtimestamp(os.path.getmtime(path), tz=_dt.timezone.utc)
    except OSError:
        s.ended_at = None
    s.started_at = s.started_at or s.ended_at
    s.project = os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else None
    s.metadata.update({"event_types": types, "model": model, "mode": doc.get("mode"), "file_id": file_id,
                       "encoded_project_dir": encode_project_dir(s.cwd), "timestamps": "session dates only"})
    s.warnings.extend(warnings)
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _sid, p in session_files(home):
        yield parse_continue_session(p, **kwargs)
__all__ = ["parse_continue_session", "session_files", "continue_home", "iter_sessions", "SOURCE", "NEVER_READ"]
