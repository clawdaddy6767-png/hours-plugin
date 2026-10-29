from __future__ import annotations
import os
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, iter_jsonl, json_size, make_tool,
                      set_text)
SOURCE = "pi"
CHILD_RUN_RE = re.compile(r"^run-\d+$")
NAME_RE = re.compile(r"^(?:.*_)?([0-9A-Za-z-]{8,})\.jsonl$")
def sessions_dir(home: Optional[str] = None) -> str:
    explicit = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    agent = os.environ.get("PI_CODING_AGENT_DIR")
    if agent:
        return os.path.join(os.path.expanduser(agent), "sessions")
    return os.path.join(home or os.path.expanduser("~"), ".pi", "agent", "sessions")
def session_files(root: Optional[str] = None) -> List[Tuple[str, str]]:
    root = root or sessions_dir()
    out: List[Tuple[str, str]] = []
    seen = set()
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        dirnames.sort()
        in_child_run = any(CHILD_RUN_RE.match(p) for p in dirpath.split(os.sep))
        for n in sorted(names):
            if not n.endswith(".jsonl") or n == "run-history.jsonl":
                continue
            if in_child_run and n == "session.jsonl":
                continue
            p = os.path.join(dirpath, n)
            real = os.path.realpath(p)
            if real in seen or not os.path.isfile(real):
                continue
            seen.add(real)
            m = NAME_RE.match(n)
            sid = (m.group(1) if m else n[:-6])
            out.append((sid.lower() if len(sid) == 36 else sid, p))
    return out
def _blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
def _text_of(content: Any) -> str:
    return "\n".join(str(b.get("text") or "") for b in _blocks(content) if b.get("type") == "text")
def _usage(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    def n(*keys: str) -> int:
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return int(v)
        return 0
    t = Tokens(input=n("input", "input_tokens"), output=n("output", "output_tokens"),
               cache_read=n("cacheRead", "cache_read_input_tokens"),
               cache_creation=n("cacheWrite", "cache_creation_input_tokens"))
    return t if (t.input or t.output or t.cache_read or t.cache_creation) else None
def _mcp_name(name: str) -> Optional[str]:
    m = re.match(r"^mcp_([^_]+)_", name)
    return m.group(1) if m else None
def parse_pi_entries(entries: Iterable[Dict[str, Any]], *, source: str = SOURCE, label: str = "pi",
                     keep_text: bool = True, tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS,
                     id_hint: Optional[str] = None, warnings: Optional[List[str]] = None) -> Session:
    recs = [r for r in entries if isinstance(r, dict)]
    s = Session(id="", source=source)
    if warnings:
        s.warnings.extend(warnings)
    header = next((r for r in recs if r.get("type") == "session"), None)
    body = [r for r in recs if r is not header]
    meta_id: Optional[str] = None
    if header:
        meta_id = str(header.get("id") or "") or None
        cwd = header.get("cwd")
        s.cwd = cwd if isinstance(cwd, str) and cwd else None
        s.started_at = parse_ts(header.get("timestamp"))
        s.metadata.update({"format_version": header.get("version"),
                           "forked_from": bool(header.get("parentSession"))})
    parent_of = {str(r["id"]): r.get("parentId") for r in body if r.get("id")}
    main: Optional[set] = None
    if parent_of and any(v for v in parent_of.values()):
        leaf = next((str(r["id"]) for r in reversed(body) if r.get("id")), None)
        main = set()
        cur = leaf
        while cur and cur not in main:
            main.add(cur)
            cur = parent_of.get(cur)
    types: Dict[str, int] = {}
    calls: Dict[str, Turn] = {}
    model: Optional[str] = None
    effort: Optional[str] = None
    n = 0
    def add(role: str, kind: str, ts: Any, rec: Dict[str, Any]) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind, timestamp=parse_ts(ts), message_id=str(rec.get("id") or "") or None)
        if main is not None and rec.get("id") and str(rec["id"]) not in main:
            t.on_main_branch = False
        if role == "assistant":
            t.model, t.effort = model, effort
        s.turns.append(t)
        return t
    for rec in body:
        typ = str(rec.get("type") or "")
        bump(types, typ or "untyped")
        ets = rec.get("timestamp")
        if typ == "model_change":
            m = rec.get("modelId") or rec.get("model")
            model = str(m) if isinstance(m, str) and m else model
        elif typ == "thinking_level_change":
            e = rec.get("thinkingLevel") or rec.get("level")
            effort = str(e) if isinstance(e, str) and e else effort
        elif typ == "session_info":
            nm = rec.get("name")
            s.title = nm if isinstance(nm, str) and nm else s.title
        elif typ == "compaction":
            t = add("system", "compaction", ets, rec)
            t.extra.update({"boundary": True, "trigger": "pi", "tokens_before": rec.get("tokensBefore")})
            set_text(t, rec.get("summary") if isinstance(rec.get("summary"), str) else "", keep_text=False)
        elif typ == "branch_summary":
            t = add("system", "meta", ets, rec)
            t.extra["branch_summary"] = True
            set_text(t, rec.get("summary") if isinstance(rec.get("summary"), str) else "", keep_text=False)
        elif typ == "custom_message":
            t = add("system", "meta", ets, rec)
            t.extra["custom_type"] = str(rec.get("customType") or "")[:64]
            set_text(t, _text_of(rec.get("content")), keep_text=False)
        elif typ == "message" and isinstance(rec.get("message"), dict):
            _message(rec, rec["message"], add, calls, keep_text, tool_result_chars, lambda: model, lambda: effort)
    sid = as_uuid(meta_id or id_hint, source, label, id_hint or "")
    s.id = sid
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = s.started_at or (min(stamps) if stamps else None)
    s.ended_at = max(stamps) if stamps else s.started_at
    s.metadata.update({"event_types": types, "encoded_project_dir": encode_project_dir(s.cwd),
                       "branched": main is not None and len(main) < len(parent_of)})
    if header is None:
        s.warnings.append("no session header")
    return s
def _message(rec: Dict[str, Any], msg: Dict[str, Any], add: Any, calls: Dict[str, Turn], keep_text: bool,
             limit: Optional[int], get_model: Any, get_effort: Any) -> None:
    role = str(msg.get("role") or "")
    ts = msg.get("timestamp") if msg.get("timestamp") is not None else rec.get("timestamp")
    if role == "user":
        content = msg.get("content")
        t = add("user", "prompt", ts, rec)
        set_text(t, _text_of(content), keep_text=keep_text)
        for b in _blocks(content):
            if b.get("type") == "image":
                t.attachments.append(Attachment(kind="image", mime=b.get("mimeType")))
    elif role == "assistant":
        first: Optional[Turn] = None
        mdl = msg.get("model") if isinstance(msg.get("model"), str) else None
        for b in _blocks(msg.get("content")):
            bt = b.get("type")
            if bt == "thinking":
                t = add("assistant", "thinking", ts, rec)
                t.char_count = len(str(b.get("thinking") or b.get("text") or ""))
            elif bt == "text":
                if not str(b.get("text") or "").strip():
                    continue
                t = add("assistant", "response", ts, rec)
                set_text(t, str(b.get("text")), keep_text=keep_text)
            elif bt == "toolCall":
                t = add("assistant", "tool_call", ts, rec)
                nm = str(b.get("name") or "unknown")
                args = b.get("arguments")
                t.tool = make_tool(nm, call_id=str(b.get("id") or "") or None, args=args,
                                   mcp_server=_mcp_name(nm))
                t.char_count = json_size(args)
                if t.tool.id:
                    calls[t.tool.id] = t
                path = args.get("path") if isinstance(args, dict) else None
                if nm.lower() == "read" and isinstance(path, str) and path.endswith("SKILL.md"):
                    t.skill = os.path.basename(os.path.dirname(path)) or None
            else:
                continue
            if mdl:
                t.model = mdl
            if isinstance(msg.get("thinkingLevel"), str):
                t.effort = msg["thinkingLevel"]
            first = first or t
        tok = _usage(msg.get("usage"))
        sr = msg.get("stopReason")
        if first is None and (tok or sr in ("aborted", "error")):
            first = add("assistant", "response", ts, rec)
            if mdl:
                first.model = mdl
        if tok is not None and first is not None:
            first.tokens = tok
        if sr == "aborted":
            add("system", "interrupt", ts, rec)
        elif sr == "error":
            e = add("system", "error", ts, rec)
            e.char_count = len(str(msg.get("errorMessage") or ""))
    elif role == "toolResult":
        cid = str(msg.get("toolCallId") or "") or None
        call = calls.get(cid or "")
        r = add("tool_result", "tool_result", ts, rec)
        r.tool = make_tool(msg.get("toolName") or (call.tool.name if call and call.tool else "unknown"), call_id=cid,
                           mcp_server=_mcp_name(str(msg.get("toolName") or "")))
        r.tool.is_error = bool(msg.get("isError"))
        set_text(r, _text_of(msg.get("content")), keep_text=keep_text, limit=limit)
    elif role == "bashExecution":
        t = add("user", "shell", ts, rec)
        set_text(t, str(msg.get("command") or ""), keep_text=keep_text)
        t.extra["exit_code"] = msg.get("exitCode")
        if msg.get("excludeFromContext"):
            t.extra["excluded_from_context"] = True
        out = add("tool_result", "tool_result", ts, rec)
        out.tool = make_tool("bash")
        out.tool.is_error = isinstance(msg.get("exitCode"), int) and msg["exitCode"] != 0
        set_text(out, str(msg.get("output") or ""), keep_text=keep_text, limit=limit)
    elif role == "compactionSummary":
        t = add("system", "compaction", ts, rec)
        t.extra.update({"boundary": True, "trigger": "pi"})
        set_text(t, str(msg.get("summary") or ""), keep_text=False)
    elif role in ("branchSummary", "custom", "hookMessage", "system"):
        t = add("system", "meta", ts, rec)
        t.extra["role"] = role
        set_text(t, _text_of(msg.get("content")) or str(msg.get("summary") or ""), keep_text=False)
def parse_pi_session(path: str, *, keep_text: bool = True,
                     tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    warnings: List[str] = []
    recs = list(iter_jsonl(path, warnings))
    m = NAME_RE.match(os.path.basename(path))
    s = parse_pi_entries(recs, source=SOURCE, label=path, keep_text=keep_text, tool_result_chars=tool_result_chars,
                         id_hint=m.group(1) if m else os.path.basename(path), warnings=warnings)
    s.metadata["file_name"] = os.path.basename(path)
    return s
def iter_sessions(root: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _sid, path in session_files(root):
        yield parse_pi_session(path, **kwargs)
__all__ = ["parse_pi_session", "parse_pi_entries", "session_files", "sessions_dir", "iter_sessions", "SOURCE"]
