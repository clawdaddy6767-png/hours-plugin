from __future__ import annotations
import io
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from . import _common, _sqlite
from ._common import DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, encode_project_dir, json_size, make_tool, set_text
SOURCE = "zed"
NEVER_READ = ("settings.json", "keymap.json", "conversations", "prompts", "embeddings")
def zed_data_dir(home: Optional[str] = None) -> str:
    home = home or os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Zed")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"), "Zed")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "zed")
def db_path(home: Optional[str] = None) -> Optional[str]:
    p = os.path.join(zed_data_dir(home), "threads", "threads.db")
    return p if os.path.isfile(p) else None
def _connect(path: str) -> sqlite3.Connection:
    con = _sqlite.snapshot(path)
    con.row_factory = sqlite3.Row
    return con
def _release(con: sqlite3.Connection) -> None:
    con.row_factory = None
def thread_rows(path: Optional[str] = None) -> List[Tuple[str, str, int]]:
    path = path or db_path()
    if not path:
        return []
    try:
        con = _connect(path)
    except (sqlite3.Error, _sqlite.SnapshotError):
        return []
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(threads)")]
        where = " WHERE (parent_id IS NULL OR parent_id = '')" if "parent_id" in cols else ""
        return [(str(r[0]), str(r[1] or ""), int(r[2] or 0)) for r in con.execute(
            "SELECT id, updated_at, LENGTH(data) FROM threads" + where)]
    except sqlite3.Error:
        return []
    finally:
        _release(con)
def _unzstd(data: bytes) -> bytes:
    dec = _common.zstd_decoder()
    if dec == "zstandard":
        return _common._ZSTD["mod"].ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
    if dec == "compression.zstd":
        return _common._ZSTD["mod"].decompress(data)
    from . import _zstd
    return _zstd.decompress(data)
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for k in ("Text", "text"):
            if isinstance(content.get(k), str):
                return content[k]
        return ""
    if isinstance(content, list):
        return "\n".join(x for x in (_text_of(c) for c in content) if x)
    return ""
def _usage(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    tk = Tokens(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                cache_creation=int(u.get("cache_creation_input_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0))
    return tk if tk.total else None
def parse_zed_thread(thread_id: str, path: Optional[str] = None, *, keep_text: bool = True,
                     tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    path = path or db_path() or ""
    sid = as_uuid(thread_id, "zed", thread_id)
    s = Session(id=sid, source=SOURCE)
    warnings: List[str] = []
    row: Optional[Dict[str, Any]] = None
    try:
        con = _connect(path)
        try:
            r = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            row = dict(r) if r is not None else None
        finally:
            _release(con)
    except (sqlite3.Error, _sqlite.SnapshotError):
        warnings.append("threads.db could not be read")
    doc: Dict[str, Any] = {}
    if row is not None:
        data = row.get("data")
        try:
            raw = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else str(data or "").encode()
            if str(row.get("data_type") or "") == "zstd":
                raw = _unzstd(raw)
            v = json.loads(raw.decode("utf-8", "replace"))
            doc = v if isinstance(v, dict) else {}
        except Exception as exc:
            warnings.append("thread data unreadable: %s" % type(exc).__name__)
        s.title = row.get("summary") if isinstance(row.get("summary"), str) else None
        s.started_at = parse_ts(row.get("created_at")) or parse_ts(row.get("updated_at"))
        s.ended_at = parse_ts(row.get("updated_at"))
        fp = row.get("folder_paths")
        if isinstance(fp, str) and fp.strip():
            s.cwd = fp.replace("\r", "").split("\n")[0].split(",")[0].strip() or None
    mdl = doc.get("model") if isinstance(doc.get("model"), dict) else {}
    model = mdl.get("model") if isinstance(mdl.get("model"), str) else None
    per_request = doc.get("request_token_usage") if isinstance(doc.get("request_token_usage"), dict) else {}
    types: Dict[str, int] = {}
    n = 0
    calls: Dict[str, Turn] = {}
    pending: Optional[Tokens] = None
    def add(role: str, kind: str) -> Turn:
        nonlocal n, pending
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind)
        if role == "assistant":
            t.model = model
            if pending is not None and kind in ("response", "tool_call", "thinking"):
                t.tokens, pending = pending, None
        s.turns.append(t)
        return t
    for m in doc.get("messages") or []:
        if m == "Resume":
            bump(types, "Resume")
            continue
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("User"), dict):
            u = m["User"]
            bump(types, "User")
            content = u.get("content") if isinstance(u.get("content"), list) else []
            text = "\n".join(c["Text"] for c in content if isinstance(c, dict) and isinstance(c.get("Text"), str))
            stripped = text.lstrip()
            t = add("user", "command" if stripped.startswith("/") else "prompt")
            set_text(t, text, keep_text=keep_text)
            for c in content:
                if isinstance(c, dict) and "Mention" in c:
                    mc = c["Mention"].get("content") if isinstance(c["Mention"], dict) else None
                    t.attachments.append(Attachment(kind="file", extracted_chars=len(mc) if isinstance(mc, str) else None))
                elif isinstance(c, dict) and "Image" in c:
                    t.attachments.append(Attachment(kind="image"))
            pending = _usage(per_request.get(str(u.get("id")))) if per_request else None
        elif isinstance(m.get("Agent"), dict):
            a = m["Agent"]
            bump(types, "Agent")
            for c in a.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if isinstance(c.get("Text"), str):
                    if c["Text"].strip():
                        set_text(add("assistant", "response"), c["Text"], keep_text=keep_text)
                elif "Thinking" in c or "RedactedThinking" in c:
                    th = add("assistant", "thinking")
                    tv = c.get("Thinking")
                    th.char_count = len(str(tv.get("text") or "")) if isinstance(tv, dict) else 0
                elif isinstance(c.get("ToolUse"), dict):
                    tu = c["ToolUse"]
                    cid = str(tu.get("id") or "") or None
                    args = tu.get("input") if tu.get("input") is not None else tu.get("raw_input")
                    call = add("assistant", "tool_call")
                    call.tool = make_tool(tu.get("name"), call_id=cid, args=args)
                    call.char_count = json_size(args)
                    if cid:
                        calls[cid] = call
            results = a.get("tool_results") if isinstance(a.get("tool_results"), dict) else {}
            for cid, tr in results.items():
                if not isinstance(tr, dict):
                    continue
                call = calls.get(str(cid))
                r = add("tool_result", "tool_result")
                r.tool = make_tool(call.tool.name if call and call.tool else (tr.get("tool_name") or "unknown"),
                                   call_id=str(cid))
                if call and call.tool:
                    r.tool.category = call.tool.category
                r.tool.is_error = bool(tr.get("is_error"))
                set_text(r, _text_of(tr.get("content")), keep_text=keep_text, limit=tool_result_chars)
        elif isinstance(m.get("Compaction"), (dict, str)):
            bump(types, "Compaction")
            c = add("system", "compaction")
            c.extra.update({"boundary": True, "trigger": "zed"})
    cum = _usage(doc.get("cumulative_token_usage"))
    if cum is not None and not any(t.tokens for t in s.turns):
        first = next((t for t in s.turns if t.role == "assistant"), None)
        if first is not None:
            first.tokens = cum
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    s.project = os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else None
    s.metadata.update({"event_types": types, "model": model, "provider": mdl.get("provider"),
                       "thread_version": doc.get("version"), "thread_row_id": thread_id,
                       "encoded_project_dir": encode_project_dir(s.cwd), "timestamps": "thread dates only"})
    s.warnings.extend(warnings)
    return s
def iter_sessions(path: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for tid, _u, _b in thread_rows(path):
        yield parse_zed_thread(tid, path, **kwargs)
__all__ = ["parse_zed_thread", "thread_rows", "db_path", "zed_data_dir", "iter_sessions", "SOURCE", "NEVER_READ"]
