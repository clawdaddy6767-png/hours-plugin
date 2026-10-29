from __future__ import annotations
import codecs
import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union
from ..model import Attachment, Session, ToolInfo, Turn, categorize_tool, parse_ts
ROOT_PARENT = "00000000-0000-4000-8000-000000000000"
INJECTED_BLOCK_TYPES = {"injected_prompt_block"}
@dataclass
class ExportBundle:
    sessions: List[Session] = field(default_factory=list)
    users: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    projects: List[Dict[str, Any]] = field(default_factory=list)
    memories: List[Dict[str, Any]] = field(default_factory=list)
    login_event_count: int = 0
    manifest: Optional[Dict[str, Any]] = None
    files_seen: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
def iter_json_items(fh: IO[bytes], chunk_size: int = 1 << 20) -> Iterator[Any]:
    dec = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
    jd = json.JSONDecoder()
    buf = ""
    eof = False
    def fill() -> bool:
        nonlocal buf, eof
        if eof:
            return False
        chunk = fh.read(chunk_size)
        if not chunk:
            buf += dec.decode(b"", final=True)
            eof = True
            return False
        buf += dec.decode(chunk)
        return True
    while True:
        s = buf.lstrip()
        if s:
            buf = s
            break
        if not fill():
            return
    if buf[0] == "{":
        while fill():
            pass
        obj = json.loads(buf)
        if isinstance(obj, dict) and isinstance(obj.get("conversations"), list) and "chat_messages" not in obj:
            for x in obj["conversations"]:
                yield x
        else:
            yield obj
        return
    if buf[0] != "[":
        raise ValueError("not a JSON array or object")
    pos = 1
    while True:
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf):
                break
            buf, pos = "", 0
            if not fill():
                return
        if buf[pos] == "]":
            return
        try:
            obj, end = jd.raw_decode(buf, pos)
        except ValueError:
            if not fill():
                raise
            continue
        yield obj
        buf, pos = buf[end:], 0
        if len(buf) < 64 and not eof:
            fill()
Opener = Callable[[], IO[bytes]]
def _iter_members(path: str, warnings: List[str]) -> Iterator[Tuple[str, Opener]]:
    if os.path.isdir(path):
        for dirpath, _dirs, files in os.walk(path):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, path)
                if fn.lower().endswith(".zip"):
                    for item in _iter_members(full, warnings):
                        yield item
                else:
                    yield rel, (lambda p=full: open(p, "rb"))
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                if name.lower().endswith(".zip"):
                    data = zf.read(name)
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        for iname in inner.namelist():
                            if not iname.endswith("/"):
                                yield iname, (lambda z=inner, n=iname: z.open(n))
                    continue
                yield name, (lambda z=zf, n=name: z.open(n))
        return
    if os.path.isfile(path):
        yield os.path.basename(path), (lambda p=path: open(p, "rb"))
        return
    warnings.append("input not found: %s" % path)
def _classify(name: str) -> str:
    parts = name.replace("\\", "/").split("/")
    base = parts[-1].lower()
    parent = parts[-2].lower() if len(parts) > 1 else ""
    if not base.endswith(".json"):
        return "other"
    if base.startswith("manifest"):
        return "manifest"
    if base.startswith("conversations"):
        return "conversations"
    if base.startswith("users"):
        return "users"
    if base.startswith("login_history"):
        return "login_history"
    if base.startswith("projects") or parent == "projects":
        return "projects"
    if base.startswith("memories") or parent == "memories":
        return "memories"
    return "other"
def _tool_input_summary(name: str, inp: Any, limit: int = 300) -> Optional[str]:
    if not isinstance(inp, dict):
        return None
    for k in ("query", "url", "command", "path", "file_path", "title", "skill", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:limit]
        if isinstance(v, list) and v:
            return ",".join(str(x) for x in v)[:limit]
    return ("keys=" + ",".join(sorted(inp.keys())))[:limit]
def _block_text(b: Dict[str, Any]) -> str:
    t = b.get("type")
    if t in ("text", "voice_note"):
        return b.get("text") or ""
    if t == "thinking":
        return b.get("thinking") or ""
    if t in INJECTED_BLOCK_TYPES:
        return b.get("prompt") or b.get("text") or ""
    return ""
def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    out.append(c["text"])
                elif isinstance(c.get("title"), str):
                    out.append(c["title"])
            elif isinstance(c, str):
                out.append(c)
        return "\n".join(out)
    return ""
def _attachments(msg: Dict[str, Any]) -> List[Attachment]:
    out: List[Attachment] = []
    for a in msg.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        ec = a.get("extracted_content")
        size = a.get("file_size")
        out.append(Attachment(
            name=a.get("file_name"), mime=a.get("file_type"),
            size_bytes=size if isinstance(size, int) else None,
            extracted_chars=len(ec) if isinstance(ec, str) else None,
            kind="pasted_text" if (not a.get("file_name") and ec) else "file",
        ))
    for key in ("files", "files_v2"):
        for f in msg.get(key) or []:
            if not isinstance(f, dict):
                continue
            fk = (f.get("file_kind") or "").lower()
            out.append(Attachment(name=f.get("file_name"), kind="image" if fk == "image" else "file"))
        if msg.get("files") and key == "files":
            break
    return out
def _main_path(messages: List[Dict[str, Any]], leaf_hint: Optional[str]) -> Tuple[set, int, int]:
    by_id = {m.get("uuid"): m for m in messages if m.get("uuid")}
    has_tree = any(m.get("parent_message_uuid") for m in messages)
    if not has_tree:
        return set(by_id.keys()), 0, 0
    children: Dict[str, List[Dict[str, Any]]] = {}
    for m in messages:
        p = m.get("parent_message_uuid") or ROOT_PARENT
        children.setdefault(p, []).append(m)
    edits = regens = 0
    for p, kids in children.items():
        if len(kids) > 1:
            if all(k.get("sender") == "assistant" for k in kids):
                regens += 1
            else:
                edits += 1
    leaf = by_id.get(leaf_hint) if leaf_hint else None
    if leaf is None:
        leaves = [m for m in messages if m.get("uuid") not in children]
        leaves.sort(key=lambda m: (m.get("created_at") or "", m.get("index") or 0))
        leaf = leaves[-1] if leaves else (messages[-1] if messages else None)
    main = set()
    cur = leaf
    guard = 0
    while cur is not None and guard < len(messages) + 1:
        main.add(cur.get("uuid"))
        cur = by_id.get(cur.get("parent_message_uuid"))
        guard += 1
    return main, edits, regens
def conversation_to_session(conv: Dict[str, Any], *, keep_text: bool = True,
                            tool_result_chars: Optional[int] = 4000) -> Session:
    acct = conv.get("account") if isinstance(conv.get("account"), dict) else {}
    s = Session(
        id=str(conv.get("uuid") or conv.get("conversation_id") or conv.get("id") or ""),
        source="claude_ai",
        title=conv.get("name") or None,
        account_id=acct.get("uuid"),
        started_at=parse_ts(conv.get("created_at")),
    )
    model = conv.get("model")
    proj = conv.get("project") if isinstance(conv.get("project"), dict) else {}
    s.project = proj.get("name") or conv.get("project_uuid")
    if conv.get("summary"):
        s.metadata["summary"] = conv["summary"]
    for k in ("is_starred", "model", "project_uuid", "workspace_id"):
        if conv.get(k) is not None:
            s.metadata[k] = conv[k]
    msgs = [m for m in (conv.get("chat_messages") or []) if isinstance(m, dict)]
    msgs.sort(key=lambda m: (m.get("created_at") or "", m.get("index") if isinstance(m.get("index"), int) else 0))
    main, edits, regens = _main_path(msgs, conv.get("current_leaf_message_uuid"))
    s.metadata.update({"message_count": len(msgs), "edit_branch_points": edits, "regenerate_branch_points": regens})
    unknown_blocks: Dict[str, int] = {}
    last_ts = s.started_at
    def mk(mid: str, i: int, role: str, kind: str, msg: Dict[str, Any], ts: Any) -> Turn:
        t = Turn(id="%s#%d" % (mid, i), role=role, kind=kind,
                 parent_id=(msg.get("parent_message_uuid") if msg.get("parent_message_uuid") != ROOT_PARENT else None),
                 timestamp=parse_ts(ts) or parse_ts(msg.get("created_at")), message_id=mid,
                 on_main_branch=mid in main)
        return t
    def set_text(t: Turn, text: str, limit: Optional[int] = None) -> None:
        t.char_count = len(text)
        t.text = (text if limit is None or len(text) <= limit else text[:limit]) if keep_text else None
    for m in msgs:
        mid = str(m.get("uuid") or "")
        sender = (m.get("sender") or m.get("role") or "").lower()
        is_human = sender in ("human", "user")
        content = m.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if not blocks and isinstance(m.get("text"), str):
            blocks = [{"type": "text", "text": m["text"]}]
        ts_msg = m.get("created_at")
        mts = parse_ts(ts_msg)
        if mts and (last_ts is None or mts > last_ts):
            last_ts = mts
        i = 0
        if is_human:
            typed = [b for b in blocks if b.get("type") in ("text", "voice_note")]
            prompt = mk(mid, i, "user", "prompt", m, ts_msg)
            set_text(prompt, "\n".join(_block_text(b) for b in typed))
            prompt.attachments = _attachments(m)
            if any(b.get("type") == "voice_note" for b in typed):
                prompt.extra["voice"] = True
            s.turns.append(prompt)
            i += 1
            for b in blocks:
                bt = b.get("type")
                if bt in ("text", "voice_note"):
                    continue
                if bt in INJECTED_BLOCK_TYPES:
                    t = mk(mid, i, "system", "meta", m, b.get("start_timestamp") or ts_msg)
                    set_text(t, _block_text(b), 2000)
                    t.extra.update({"injected": True, "injection_source": b.get("injection_source")})
                elif bt == "tool_result":
                    t = _tool_result_turn(mk(mid, i, "tool_result", "tool_result", m, b.get("start_timestamp") or ts_msg), b)
                    set_text(t, _result_text(b.get("content")), tool_result_chars)
                else:
                    t = mk(mid, i, "system", "meta", m, ts_msg)
                    t.extra["block_type"] = bt
                    unknown_blocks[str(bt)] = unknown_blocks.get(str(bt), 0) + 1
                s.turns.append(t)
                i += 1
            continue
        for b in blocks:
            bt = b.get("type")
            ts = b.get("start_timestamp") or ts_msg
            if bt == "text":
                t = mk(mid, i, "assistant", "response", m, ts)
                set_text(t, b.get("text") or "")
                if b.get("citations"):
                    t.extra["citations"] = len(b["citations"])
            elif bt == "thinking":
                t = mk(mid, i, "assistant", "thinking", m, ts)
                set_text(t, b.get("thinking") or "")
                t.extra.update({"cut_off": b.get("cut_off"), "summaries": len(b.get("summaries") or [])})
            elif bt == "tool_use":
                name = str(b.get("name") or "")
                t = mk(mid, i, "assistant", "tool_call", m, ts)
                integ = b.get("integration_name")
                is_mcp = bool(b.get("mcp_server_url")) or ":" in name
                t.tool = ToolInfo(name=name, id=b.get("id"), input_summary=_tool_input_summary(name, b.get("input")),
                                  mcp_server=(integ or name.split(":")[0]) if is_mcp else None,
                                  category="mcp" if is_mcp else categorize_tool(name))
                if integ:
                    t.extra["integration_name"] = integ
                t.char_count = len(json.dumps(b.get("input"))) if b.get("input") is not None else 0
            elif bt == "tool_result":
                t = _tool_result_turn(mk(mid, i, "tool_result", "tool_result", m, ts), b)
                set_text(t, _result_text(b.get("content")), tool_result_chars)
            else:
                t = mk(mid, i, "system", "meta", m, ts)
                t.extra["block_type"] = bt
                unknown_blocks[str(bt)] = unknown_blocks.get(str(bt), 0) + 1
            t.model = model
            if b.get("stop_timestamp") and b.get("start_timestamp"):
                a, z = parse_ts(b["start_timestamp"]), parse_ts(b["stop_timestamp"])
                if a and z:
                    t.extra["duration_s"] = (z - a).total_seconds()
            s.turns.append(t)
            i += 1
    upd = parse_ts(conv.get("updated_at"))
    s.ended_at = max([x for x in (last_ts, upd) if x is not None], default=None)
    if unknown_blocks:
        s.metadata["unknown_block_types"] = unknown_blocks
    return s
def _tool_result_turn(t: Turn, b: Dict[str, Any]) -> Turn:
    name = str(b.get("name") or "unknown")
    is_mcp = bool(b.get("mcp_server_url")) or ":" in name
    t.tool = ToolInfo(name=name, id=b.get("tool_use_id"), is_error=bool(b.get("is_error")),
                      mcp_server=(b.get("integration_name") or name.split(":")[0]) if is_mcp else None,
                      category="mcp" if is_mcp else categorize_tool(name))
    return t
def parse_export(paths: Union[str, Sequence[str]], *, keep_text: bool = True,
                 tool_result_chars: Optional[int] = 4000) -> ExportBundle:
    if isinstance(paths, (str, os.PathLike)):
        paths = [str(paths)]
    bundle = ExportBundle()
    seen_conv: set = set()
    for p in paths:
        for name, opener in _iter_members(str(p), bundle.warnings):
            kind = _classify(name)
            bundle.files_seen.append(name)
            if kind == "other":
                continue
            try:
                with opener() as fh:
                    if kind == "conversations":
                        for conv in iter_json_items(fh):
                            if not isinstance(conv, dict):
                                continue
                            cid = conv.get("uuid")
                            if cid and cid in seen_conv:
                                continue
                            if cid:
                                seen_conv.add(cid)
                            try:
                                bundle.sessions.append(conversation_to_session(
                                    conv, keep_text=keep_text, tool_result_chars=tool_result_chars))
                            except Exception as exc:
                                bundle.warnings.append("conversation %s skipped: %s" % (cid, type(exc).__name__))
                    else:
                        _handle_side_file(kind, json.load(fh), bundle)
            except ValueError as exc:
                bundle.warnings.append("%s: invalid JSON (%s)" % (name, exc.__class__.__name__))
    for s in bundle.sessions:
        u = bundle.users.get(s.account_id or "")
        if u:
            s.metadata["user_email"] = u.get("email")
    return bundle
def _as_list(data: Any) -> List[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []
def _handle_side_file(kind: str, data: Any, bundle: ExportBundle) -> None:
    if kind == "users":
        for u in _as_list(data):
            if isinstance(u, dict) and u.get("uuid"):
                bundle.users[u["uuid"]] = {"full_name": u.get("full_name"),
                                           "email": u.get("email_address") or u.get("email")}
    elif kind == "projects":
        for p in _as_list(data):
            if not isinstance(p, dict):
                continue
            creator = p.get("creator") if isinstance(p.get("creator"), dict) else {}
            bundle.projects.append({
                "uuid": p.get("uuid"), "name": p.get("name"), "created_at": p.get("created_at"),
                "creator_uuid": creator.get("uuid"), "is_private": p.get("is_private"),
                "is_starter_project": p.get("is_starter_project"),
                "doc_count": len(p.get("docs") or []),
                "prompt_template_chars": len(p.get("prompt_template") or ""),
                "description_chars": len(p.get("description") or ""),
            })
    elif kind == "memories":
        for m in _as_list(data):
            if not isinstance(m, dict):
                continue
            bundle.memories.append({
                "account_uuid": m.get("account_uuid"),
                "conversations_memory_chars": len(m.get("conversations_memory") or ""),
                "memory_file_count": len(m.get("memory_files") or []),
            })
    elif kind == "login_history":
        ev = data.get("login_events") if isinstance(data, dict) else data
        bundle.login_event_count += len(ev or [])
    elif kind == "manifest" and isinstance(data, dict):
        files = data.get("data_files") or []
        bundle.manifest = {
            "version": data.get("version"), "created_at": data.get("created_at"),
            "total_files": data.get("total_files"),
            "files": [{"category": f.get("category"), "part": f.get("part"), "filename": f.get("filename")}
                      for f in files if isinstance(f, dict)],
        }
