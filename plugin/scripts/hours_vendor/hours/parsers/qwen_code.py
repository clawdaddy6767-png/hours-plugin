from __future__ import annotations
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, iter_jsonl, json_size, make_tool,
                      set_command, set_text)
SOURCE = "qwen_code"
NEVER_READ = ("settings.json", "oauth_creds.json", "installation_id", "QWEN.md")
REAL = ("user", "assistant", "tool_result")
def qwen_home() -> str:
    return os.path.expanduser(os.environ.get("QWEN_RUNTIME_DIR") or "~/.qwen")
def session_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    home = home or qwen_home()
    out: List[Tuple[str, str]] = []
    seen = set()
    for top in ("projects", "tmp"):
        root = os.path.join(home, top)
        try:
            projects = sorted(os.listdir(root))
        except OSError:
            continue
        for p in projects:
            chats = os.path.join(root, p, "chats")
            try:
                names = sorted(os.listdir(chats))
            except OSError:
                continue
            for n in names:
                if n.endswith(".jsonl"):
                    fp = os.path.join(chats, n)
                    if os.path.isfile(fp) and fp not in seen:
                        seen.add(fp)
                        out.append((n[:-6], fp))
    return out
def _parts(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    m = rec.get("message")
    if isinstance(m, dict) and isinstance(m.get("parts"), list):
        return [p for p in m["parts"] if isinstance(p, dict)]
    return []
def _tokens(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    prompt = int(u.get("promptTokenCount") or 0)
    cached = int(u.get("cachedContentTokenCount") or 0)
    tk = Tokens(input=max(0, prompt - cached), output=int(u.get("candidatesTokenCount") or 0)
                + int(u.get("thoughtsTokenCount") or 0), cache_read=cached)
    return tk if tk.total else None
def _active_branch(recs: List[Dict[str, Any]]) -> Optional[set]:
    by_id = {r.get("uuid"): r for r in recs if r.get("uuid")}
    tail = next((r for r in reversed(recs) if r.get("type") in REAL and r.get("uuid")), None)
    keep = set()
    cur = tail
    guard = 0
    while cur is not None and guard < 1000000:
        keep.add(cur.get("uuid"))
        cur = by_id.get(cur.get("parentUuid"))
        guard += 1
    return keep
def parse_qwen_session(path: str, *, keep_text: bool = True,
                       tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    warnings: List[str] = []
    recs = list(iter_jsonl(path, warnings))
    file_id = os.path.basename(path)[:-6]
    sess_id = next((str(r["sessionId"]) for r in recs if r.get("sessionId")), file_id)
    sid = as_uuid(sess_id, "qwen_code", file_id)
    s = Session(id=sid, source=SOURCE)
    types: Dict[str, int] = {}
    if any(r.get("subtype") == "rewind" for r in recs):
        keep = _active_branch(recs)
        recs = [r for r in recs if r.get("type") not in REAL or r.get("uuid") in keep]
        s.metadata["rewound"] = True
    calls: Dict[str, Turn] = {}
    n = 0
    versions: List[str] = []
    def add(role: str, kind: str, rec: Dict[str, Any]) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind, timestamp=parse_ts(rec.get("timestamp")),
                 message_id=str(rec.get("uuid") or "") or None)
        if role == "assistant":
            t.model = rec.get("model") if isinstance(rec.get("model"), str) else None
        s.turns.append(t)
        return t
    for rec in recs:
        typ = str(rec.get("type") or "")
        sub = str(rec.get("subtype") or "")
        bump(types, "%s:%s" % (typ, sub) if sub else typ)
        if isinstance(rec.get("cwd"), str) and not s.cwd:
            s.cwd = rec["cwd"]
        v = rec.get("version")
        if isinstance(v, str) and v not in versions:
            versions.append(v)
        if isinstance(rec.get("gitBranch"), str):
            s.metadata.setdefault("git_branch", rec["gitBranch"])
        parts = _parts(rec)
        if typ == "user" and sub in ("", "mid_turn_user_message"):
            text = "\n".join(str(p.get("text") or "") for p in parts if "text" in p and not p.get("thought"))
            if not text.strip() and not parts:
                continue
            t = add("user", "prompt", rec)
            set_text(t, text, keep_text=keep_text)
            for p in parts:
                if isinstance(p.get("inlineData"), dict) or isinstance(p.get("fileData"), dict):
                    t.attachments.append(Attachment(kind="image" if "image" in str(
                        (p.get("inlineData") or p.get("fileData") or {}).get("mimeType")) else "file"))
        elif typ == "system" and sub == "slash_command":
            payload = rec.get("systemPayload") if isinstance(rec.get("systemPayload"), dict) else {}
            raw = payload.get("rawCommand") or payload.get("command") or "/?"
            t = add("user", "command", rec)
            set_command(t, str(raw) if str(raw).startswith("/") else "/" + str(raw), keep_text=keep_text)
        elif typ == "system" and sub == "chat_compression":
            c = add("system", "compaction", rec)
            c.extra.update({"boundary": True, "trigger": "qwen_code"})
        elif typ == "assistant":
            first: Optional[Turn] = None
            for p in parts:
                if p.get("thought") and "text" in p:
                    th = add("assistant", "thinking", rec)
                    th.char_count = len(str(p.get("text") or ""))
                    first = first or th
                elif "text" in p and str(p.get("text") or "").strip():
                    r = add("assistant", "response", rec)
                    set_text(r, str(p["text"]), keep_text=keep_text)
                    first = first or r
                elif isinstance(p.get("functionCall"), dict):
                    fc = p["functionCall"]
                    cid = str(fc.get("id") or "") or None
                    c = add("assistant", "tool_call", rec)
                    c.tool = make_tool(fc.get("name"), call_id=cid, args=fc.get("args"))
                    c.char_count = json_size(fc.get("args"))
                    if cid:
                        calls[cid] = c
                    first = first or c
            tk = _tokens(rec.get("usageMetadata"))
            if tk is not None:
                first = first or add("assistant", "response", rec)
                first.tokens = tk
        elif typ == "tool_result":
            res = rec.get("toolCallResult") if isinstance(rec.get("toolCallResult"), dict) else {}
            for p in parts:
                fr = p.get("functionResponse")
                if not isinstance(fr, dict):
                    continue
                cid = str(fr.get("id") or "") or None
                call = calls.get(cid or "")
                r = add("tool_result", "tool_result", rec)
                r.tool = make_tool(call.tool.name if call and call.tool else (fr.get("name") or "unknown"), call_id=cid)
                if call and call.tool:
                    r.tool.category = call.tool.category
                body = fr.get("response") if isinstance(fr.get("response"), dict) else {}
                err = body.get("error") or res.get("error")
                status = str(res.get("status") or "")
                r.tool.is_error = bool(err) or status == "error"
                r.tool.is_rejected = status == "cancelled"
                set_text(r, str(body.get("output") or body.get("error") or ""), keep_text=keep_text,
                         limit=tool_result_chars)
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = min(stamps) if stamps else None
    s.ended_at = max(stamps) if stamps else None
    s.project = os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else None
    s.metadata.update({"event_types": types, "versions": versions, "file_id": file_id,
                       "encoded_project_dir": encode_project_dir(s.cwd)})
    s.warnings.extend(warnings)
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _sid, p in session_files(home):
        yield parse_qwen_session(p, **kwargs)
__all__ = ["parse_qwen_session", "session_files", "qwen_home", "iter_sessions", "SOURCE", "NEVER_READ"]
