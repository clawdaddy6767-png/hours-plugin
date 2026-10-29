from __future__ import annotations
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, background_requested, UUID_RE, as_uuid, bump, encode_project_dir, iter_jsonl, json_size,
                      make_tool, set_command, set_text)
SOURCE = "copilot_cli"
NEVER_READ = ("config.json", "mcp-oauth-config", "mcp-secrets", "session-store.db")
def copilot_home() -> str:
    return os.path.expanduser(os.environ.get("COPILOT_HOME") or "~/.copilot")
def session_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    root = os.path.join(home or copilot_home(), "session-state")
    out: List[Tuple[str, str]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        p = os.path.join(root, n)
        if os.path.isdir(p):
            ev = os.path.join(p, "events.jsonl")
            if os.path.isfile(ev):
                out.append((n.lower() if UUID_RE.match(n) else n, ev))
        elif n.endswith(".jsonl") and os.path.isfile(p):
            stem = n[:-6]
            out.append((stem.lower() if UUID_RE.match(stem) else stem, p))
    return out
def events_path_for(session_id: str, home: Optional[str] = None) -> Optional[str]:
    if not UUID_RE.match(session_id or ""):
        return None
    root = os.path.join(home or copilot_home(), "session-state")
    for p in (os.path.join(root, session_id, "events.jsonl"), os.path.join(root, session_id + ".jsonl")):
        if os.path.isfile(p):
            return p
    return None
def _data(rec: Dict[str, Any]) -> Dict[str, Any]:
    d = rec.get("data")
    return d if isinstance(d, dict) else rec
def _str(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(x if isinstance(x, str) else str((x or {}).get("text") or "") for x in v
                         if isinstance(x, (str, dict)))
    if isinstance(v, dict):
        for k in ("content", "text", "detailedContent", "message"):
            if isinstance(v.get(k), str):
                return v[k]
    return ""
def parse_copilot_session(path: str, *, keep_text: bool = True,
                          tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    warnings: List[str] = []
    base = os.path.basename(path)
    folder_id = os.path.basename(os.path.dirname(path)) if base == "events.jsonl" else base[:-6]
    s = Session(id="", source=SOURCE)
    types: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    calls: Dict[str, Turn] = {}
    model: Optional[str] = None
    meta_id: Optional[str] = None
    n = 0
    def add(role: str, kind: str, ts: Any, mid: Any = None) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind, timestamp=parse_ts(ts),
                 message_id=str(mid) if mid else None)
        if role == "assistant":
            t.model = model
        s.turns.append(t)
        return t
    for rec in iter_jsonl(path, warnings):
        typ = str(rec.get("type") or "")
        ts = rec.get("timestamp")
        d = _data(rec)
        bump(types, typ or "untyped")
        if typ.startswith("model.") or typ.startswith("hook."):
            bump(dropped, typ.split(".")[0])
            continue
        if typ == "session.start":
            meta_id = str(d.get("sessionId") or "") or meta_id
            ctx = d.get("context") if isinstance(d.get("context"), dict) else {}
            s.cwd = ctx.get("cwd") if isinstance(ctx.get("cwd"), str) else s.cwd
            s.started_at = parse_ts(d.get("startTime")) or parse_ts(ts)
            ver = d.get("copilotVersion") or d.get("version")
            s.metadata.update({"copilot_version": ver, "producer": d.get("producer"),
                               "git_branch": ctx.get("branch")})
            if ver:
                s.metadata["versions"] = [str(ver)]
        elif typ == "session.resume":
            bump(s.metadata.setdefault("resumes", {}), "n")
        elif typ == "user.message":
            text = _str(d.get("content"))
            stripped = text.lstrip()
            kind = "command" if stripped.startswith("/") else ("shell" if stripped.startswith("!") else "prompt")
            t = add("user", kind, ts, rec.get("id"))
            if kind == "command":
                set_command(t, stripped, keep_text=keep_text)
            else:
                set_text(t, stripped[1:].strip() if kind == "shell" else text, keep_text=keep_text)
            for a in d.get("attachments") or []:
                if isinstance(a, dict):
                    at = str(a.get("type") or "file").lower()
                    t.attachments.append(Attachment(kind="image" if "image" in at else "file",
                                                    mime=a.get("mimeType")))
        elif typ == "assistant.message":
            if isinstance(d.get("model"), str):
                model = d["model"]
            elif isinstance(rec.get("model"), str):
                model = rec["model"]
            first: Optional[Turn] = None
            rt = d.get("reasoningText")
            if isinstance(rt, str) and rt:
                th = add("assistant", "thinking", ts, rec.get("id"))
                th.char_count = len(rt)
                first = th
            text = _str(d.get("content"))
            if text.strip():
                r = add("assistant", "response", ts, rec.get("id"))
                set_text(r, text, keep_text=keep_text)
                first = first or r
            for tr in d.get("toolRequests") or []:
                if not isinstance(tr, dict):
                    continue
                cid = str(tr.get("toolCallId") or tr.get("id") or "") or None
                c = add("assistant", "tool_call", ts, rec.get("id"))
                args = tr.get("arguments") if tr.get("arguments") is not None else tr.get("args")
                c.tool = make_tool(tr.get("name") or tr.get("toolName"), call_id=cid, args=args)
                if background_requested(args):
                    c.extra["background"] = True
                c.char_count = json_size(args)
                if cid:
                    calls[cid] = c
                first = first or c
            out_tok = d.get("outputTokens")
            if isinstance(out_tok, int) and not isinstance(out_tok, bool) and out_tok > 0:
                if first is None:
                    first = add("assistant", "response", ts, rec.get("id"))
                first.tokens = Tokens(output=out_tok)
        elif typ == "assistant.reasoning":
            th = add("assistant", "thinking", ts, rec.get("id"))
            th.char_count = len(_str(d.get("content")))
        elif typ == "tool.execution_start":
            cid = str(d.get("toolCallId") or "") or None
            if cid and cid in calls:
                continue
            c = add("assistant", "tool_call", ts, rec.get("id"))
            c.tool = make_tool(d.get("toolName") or d.get("name"), call_id=cid, args=d.get("arguments"))
            c.char_count = json_size(d.get("arguments"))
            if cid:
                calls[cid] = c
        elif typ == "tool.execution_complete":
            cid = str(d.get("toolCallId") or "") or None
            call = calls.get(cid or "")
            r = add("tool_result", "tool_result", ts, rec.get("id"))
            r.tool = make_tool(call.tool.name if call and call.tool else (d.get("toolName") or "unknown"),
                               call_id=cid)
            r.tool.is_error = d.get("success") is False or bool(d.get("error"))
            res = d.get("result")
            set_text(r, _str(res) if res is not None else _str(d.get("error")), keep_text=keep_text,
                     limit=tool_result_chars)
        elif typ == "abort":
            add("system", "interrupt", ts, rec.get("id"))
        elif typ in ("session.compaction_complete", "session.truncation"):
            t = add("system", "compaction", ts, rec.get("id"))
            t.extra.update({"boundary": True, "trigger": "copilot"})
        elif typ == "session.model_change":
            nm = d.get("newModel") or d.get("model")
            if isinstance(nm, str):
                model = nm
        elif typ == "session.mode_changed":
            t = add("system", "mode_change", ts, rec.get("id"))
            t.extra.update({"what": "mode", "to": d.get("newMode") or d.get("mode")})
        elif typ == "skill.invoked":
            t = add("system", "meta", ts, rec.get("id"))
            nm = d.get("name") or d.get("skillName")
            t.skill = str(nm) if nm else None
        elif typ in ("session.error", "error"):
            t = add("system", "error", ts, rec.get("id"))
            t.char_count = len(_str(d.get("message") or d.get("error")))
        elif typ:
            pass
    sid = as_uuid(meta_id or folder_id, "copilot_cli", path)
    s.id = sid
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = s.started_at or (min(stamps) if stamps else None)
    s.ended_at = max(stamps) if stamps else s.started_at
    s.metadata.update({
        "event_types": types, "dropped": dropped, "folder_id": folder_id,
        "folder_matches_meta": (folder_id.lower() == meta_id.lower()) if (meta_id and folder_id) else None,
        "legacy_flat_file": base != "events.jsonl",
        "encoded_project_dir": encode_project_dir(s.cwd),
    })
    s.warnings.extend(warnings)
    if meta_id is None:
        s.warnings.append("no session.start event")
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _sid, path in session_files(home):
        yield parse_copilot_session(path, **kwargs)
__all__ = ["parse_copilot_session", "session_files", "events_path_for", "iter_sessions", "copilot_home", "SOURCE",
           "NEVER_READ"]
