from __future__ import annotations
import json
import os
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Session, Tokens, Turn, parse_ts
from . import _sqlite
from ._common import (DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, iter_jsonl, json_size, make_tool,
                      parse_args, set_command, set_text)
SOURCE = "cursor"
NEVER_READ = ("ItemTable", "mcp.json", "store.db")
QUERY_RE = re.compile(r"</?user_query>")
def db_path() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Cursor", "User", "globalStorage", "state.vscdb")
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "Cursor", "User", "globalStorage", "state.vscdb")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(os.path.expanduser(base), "Cursor", "User", "globalStorage", "state.vscdb")
def cursor_home() -> str:
    return os.path.expanduser("~/.cursor")
def _ms(v: Any) -> Optional[float]:
    return _sqlite.to_epoch(v)
def composer_refs(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or db_path()
    out: List[Dict[str, Any]] = []
    if not os.path.isfile(path):
        return out
    try:
        conn = _sqlite.snapshot(path)
    except _sqlite.SnapshotError:
        return out
    if "cursorDiskKV" not in _sqlite.tables(conn):
        return out
    lo, hi = _sqlite.prefix_range("composerData:")
    for key, value in _sqlite.rows(conn, "select key, value from cursorDiskKV where key >= ? and key < ?", (lo, hi)):
        doc = _sqlite.loads(value)
        if not isinstance(doc, dict):
            continue
        heads = doc.get("fullConversationHeadersOnly")
        inline = doc.get("conversation")
        n = len(heads) if isinstance(heads, list) else (len(inline) if isinstance(inline, list) else 0)
        created = _ms(doc.get("createdAt"))
        if not n or created is None:
            continue
        out.append({"id": str(key)[len("composerData:"):], "path": path, "created": created,
                    "updated": _ms(doc.get("lastUpdatedAt")) or created, "messages": n})
    return out
def transcript_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    root = os.path.join(home or cursor_home(), "projects")
    out: List[Tuple[str, str]] = []
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return out
    for proj in projects:
        tdir = os.path.join(root, proj, "agent-transcripts")
        try:
            names = sorted(os.listdir(tdir))
        except OSError:
            continue
        for n in names:
            p = os.path.join(tdir, n)
            if n.endswith(".jsonl") and os.path.isfile(p):
                out.append((n[:-6], p))
            elif os.path.isdir(p):
                inner = os.path.join(p, n + ".jsonl")
                if os.path.isfile(inner):
                    out.append((n, inner))
    return out
def _lexical_text(node: Any) -> str:
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        return "".join(_lexical_text(c) for c in node.get("children") or [])
    return ""
def _bubble_text(b: Dict[str, Any]) -> str:
    t = b.get("text")
    if isinstance(t, str) and t.strip():
        return t
    rt = b.get("richText")
    if isinstance(rt, str) and rt.strip():
        doc = _sqlite.loads(rt)
        if isinstance(doc, dict):
            return _lexical_text(doc.get("root") or doc)
    return ""
def _tokens(b: Dict[str, Any]) -> Optional[Tokens]:
    tc = b.get("tokenCount")
    if not isinstance(tc, dict):
        return None
    def n(*keys: str) -> int:
        for k in keys:
            v = tc.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return int(v)
        return 0
    t = Tokens(input=n("inputTokens", "input"), output=n("outputTokens", "output"),
               cache_read=n("cacheRead", "cache_read"), cache_creation=n("cacheCreation", "cache_creation"))
    return t if (t.input or t.output or t.cache_read or t.cache_creation) else None
def parse_cursor_composer(path: str, composer_id: str, *, keep_text: bool = True,
                          tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    conn = _sqlite.snapshot(path)
    doc = next((_sqlite.loads(v) for (v,) in _sqlite.rows(
        conn, "select value from cursorDiskKV where key = ?", ("composerData:" + composer_id,))), None)
    doc = doc if isinstance(doc, dict) else {}
    s = Session(id=as_uuid(composer_id, SOURCE, composer_id), source=SOURCE)
    s.started_at = parse_ts(doc.get("createdAt"))
    s.ended_at = parse_ts(doc.get("lastUpdatedAt")) or s.started_at
    mc = doc.get("modelConfig") if isinstance(doc.get("modelConfig"), dict) else {}
    model = mc.get("modelName") if isinstance(mc.get("modelName"), str) else None
    heads = doc.get("fullConversationHeadersOnly")
    bubbles: List[Tuple[str, Optional[int], Dict[str, Any]]] = []
    if isinstance(heads, list) and heads:
        for h in heads:
            if not isinstance(h, dict) or not h.get("bubbleId"):
                continue
            bid = str(h["bubbleId"])
            row = next(iter(_sqlite.rows(conn, "select value from cursorDiskKV where key = ?",
                                         ("bubbleId:%s:%s" % (composer_id, bid),))), None)
            b = _sqlite.loads(row[0]) if row else None
            if isinstance(b, dict):
                bubbles.append((bid, h.get("type") if isinstance(h.get("type"), int) else b.get("type"), b))
    elif isinstance(doc.get("conversation"), list):
        for i, b in enumerate(doc["conversation"]):
            if isinstance(b, dict):
                bubbles.append((str(b.get("bubbleId") or i), b.get("type"), b))
    types: Dict[str, int] = {}
    n = 0
    def add(role: str, kind: str, ts: Any, mid: str) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind, timestamp=parse_ts(ts), message_id=mid)
        if role == "assistant":
            t.model = model
        s.turns.append(t)
        return t
    for bid, btype, b in bubbles:
        bump(types, str(btype))
        ts = b.get("createdAt")
        if btype == 1:
            text = _bubble_text(b)
            stripped = text.lstrip()
            kind = "command" if stripped.startswith("/") else "prompt"
            t = add("user", kind, ts, bid)
            if kind == "command":
                set_command(t, stripped, keep_text=keep_text)
            else:
                set_text(t, text, keep_text=keep_text)
            continue
        if btype != 2:
            continue
        first: Optional[Turn] = None
        mi = b.get("modelInfo") if isinstance(b.get("modelInfo"), dict) else {}
        bmodel = mi.get("modelName") if isinstance(mi.get("modelName"), str) else None
        th = b.get("thinking")
        thinking = th.get("text") if isinstance(th, dict) else th
        if isinstance(thinking, str) and thinking.strip():
            t = add("assistant", "thinking", ts, bid)
            t.char_count = len(thinking)
            first = t
        text = _bubble_text(b)
        if text.strip():
            t = add("assistant", "response", ts, bid)
            set_text(t, text, keep_text=keep_text)
            first = first or t
        tfd = b.get("toolFormerData")
        if isinstance(tfd, dict) and tfd.get("name"):
            args = parse_args(tfd.get("params") if tfd.get("params") is not None else tfd.get("rawArgs"))
            cid = str(tfd.get("toolCallId") or bid)
            c = add("assistant", "tool_call", ts, bid)
            c.tool = make_tool(str(tfd["name"]), call_id=cid, args=args)
            c.char_count = json_size(args)
            first = first or c
            status = str(tfd.get("status") or "").lower()
            decision = str(tfd.get("userDecision") or "").lower()
            if tfd.get("result") is not None or status in ("completed", "error", "cancelled"):
                r = add("tool_result", "tool_result", ts, bid)
                r.tool = make_tool(str(tfd["name"]), call_id=cid)
                r.tool.is_error = status == "error" or bool(b.get("errorDetails"))
                r.tool.is_rejected = decision == "rejected"
                if r.tool.is_rejected:
                    r.tool.denial_kind = "user-rejected"
                res = tfd.get("result")
                set_text(r, res if isinstance(res, str) else json.dumps(res) if res is not None else "",
                         keep_text=keep_text, limit=tool_result_chars)
        tok = _tokens(b)
        if tok is not None:
            if first is None:
                first = add("assistant", "response", ts, bid)
            first.tokens = tok
        if bmodel:
            for t in s.turns[-3:]:
                if t.message_id == bid and t.role == "assistant":
                    t.model = bmodel
        if b.get("errorDetails") and first is None:
            e = add("system", "error", ts, bid)
            e.char_count = json_size(b.get("errorDetails"))
    s.metadata.update({"composer_id": composer_id, "agentic": bool(doc.get("isAgentic")),
                       "mode": doc.get("unifiedMode") if isinstance(doc.get("unifiedMode"), str) else None,
                       "bubble_types": types, "bubbles_found": len(bubbles),
                       "bubbles_listed": len(heads) if isinstance(heads, list) else None})
    if isinstance(heads, list) and len(bubbles) < len(heads):
        s.warnings.append("%d of %d messages were not in the database" % (len(heads) - len(bubbles), len(heads)))
    sid = s.id
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    if stamps:
        s.started_at = min([x for x in [s.started_at] if x] + stamps)
        s.ended_at = max([x for x in [s.ended_at] if x] + stamps)
    return s
def parse_cursor_transcript(path: str, *, keep_text: bool = True,
                            tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    warnings: List[str] = []
    base = os.path.basename(path)
    stem = base[:-6] if base.endswith(".jsonl") else base
    s = Session(id=as_uuid(stem, SOURCE, stem), source=SOURCE)
    calls: Dict[str, Turn] = {}
    n = 0
    types: Dict[str, int] = {}
    for rec in iter_jsonl(path, warnings):
        role = rec.get("role") or rec.get("type")
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
            [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        bump(types, str(role))
        ts = rec.get("timestamp") or msg.get("timestamp")
        def add(r: str, kind: str) -> Turn:
            nonlocal n
            n += 1
            t = Turn(id="#%d" % n, role=r, kind=kind, timestamp=parse_ts(ts))
            s.turns.append(t)
            return t
        if role == "user":
            res = [b for b in blocks if b.get("type") == "tool_result"]
            for b in res:
                r = add("tool_result", "tool_result")
                call = calls.get(str(b.get("tool_use_id") or ""))
                r.tool = make_tool(call.tool.name if call and call.tool else "unknown",
                                   call_id=str(b.get("tool_use_id") or "") or None)
                r.tool.is_error = bool(b.get("is_error"))
                body = b.get("content")
                set_text(r, body if isinstance(body, str) else "\n".join(
                    str(x.get("text") or "") for x in body or [] if isinstance(x, dict)),
                    keep_text=keep_text, limit=tool_result_chars)
            text = QUERY_RE.sub("", "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")).strip()
            if text:
                kind = "command" if text.startswith("/") else "prompt"
                t = add("user", kind)
                if kind == "command":
                    set_command(t, text, keep_text=keep_text)
                else:
                    set_text(t, text, keep_text=keep_text)
        elif role == "assistant":
            for b in blocks:
                bt = b.get("type")
                if bt == "text" and str(b.get("text") or "").strip():
                    set_text(add("assistant", "response"), str(b["text"]), keep_text=keep_text)
                elif bt == "thinking":
                    add("assistant", "thinking").char_count = len(str(b.get("thinking") or b.get("text") or ""))
                elif bt == "tool_use":
                    c = add("assistant", "tool_call")
                    c.tool = make_tool(str(b.get("name") or "unknown"), call_id=str(b.get("id") or "") or None,
                                       args=b.get("input"))
                    c.char_count = json_size(b.get("input"))
                    if c.tool.id:
                        calls[c.tool.id] = c
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (s.id, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = min(stamps) if stamps else None
    s.ended_at = max(stamps) if stamps else None
    s.metadata.update({"transcript_file": base, "role_counts": types, "no_timestamps": not stamps,
                       "encoded_project_dir": os.path.basename(path.split("agent-transcripts")[0].rstrip(os.sep)) or None})
    s.warnings.extend(warnings)
    return s
def iter_sessions(path: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for ref in composer_refs(path):
        yield parse_cursor_composer(ref["path"], ref["id"], **kwargs)
__all__ = ["parse_cursor_composer", "parse_cursor_transcript", "composer_refs", "transcript_files", "db_path",
           "cursor_home", "iter_sessions", "SOURCE", "NEVER_READ"]
