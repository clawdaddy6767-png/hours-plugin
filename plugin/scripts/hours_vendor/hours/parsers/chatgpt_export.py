from __future__ import annotations
import codecs
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union
from ..model import Attachment, Session, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, as_uuid, bump, json_size, make_tool, set_text, strip_data_uris)
from .claude_export import iter_json_items
SOURCE = "chatgpt"
CONV_RE = re.compile(r"^conversations(?:-\d{1,5})?\.json$", re.I)
CHAT_HTML = "chat.html"
NEVER_OPEN = {"user.json", "message_feedback.json", "shared_conversations.json", "model_comparisons.json",
              "shopping.json", "sora.json", "group_chats.json"}
TEXT_TYPES = {"text", "multimodal_text"}
THINK_TYPES = {"thoughts", "reasoning_recap"}
HIDDEN_TOOL_TYPES = {"tether_browsing_display", "sonic_webpage"}
@dataclass
class ChatGPTExport:
    sessions: List[Session] = field(default_factory=list)
    files_seen: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    used_chat_html: bool = False
def is_conversations_name(name: str) -> bool:
    return bool(CONV_RE.match(os.path.basename(name.replace("\\", "/"))))
def looks_like_chatgpt(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("mapping"), dict)
def _content(msg: Dict[str, Any]) -> Tuple[str, str, List[Attachment]]:
    c = msg.get("content") if isinstance(msg.get("content"), dict) else {}
    ct = str(c.get("content_type") or "")
    atts: List[Attachment] = []
    texts: List[str] = []
    parts = c.get("parts")
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, str):
                texts.append(p)
            elif isinstance(p, dict):
                pt = str(p.get("content_type") or "")
                if isinstance(p.get("text"), str):
                    texts.append(p["text"])
                elif "image" in pt:
                    size = p.get("size_bytes")
                    atts.append(Attachment(kind="image", size_bytes=size if isinstance(size, int) else None))
                elif "audio" in pt:
                    atts.append(Attachment(kind="voice"))
                elif pt:
                    atts.append(Attachment(kind="file"))
    for k in ("text", "result"):
        if isinstance(c.get(k), str) and not texts:
            texts.append(c[k])
    if ct == "tether_quote":
        texts = [x for x in (c.get("title"), c.get("text")) if isinstance(x, str)]
    text, n_media = strip_data_uris("\n".join(texts))
    atts.extend(Attachment(kind="image") for _ in range(n_media))
    return ct, text, atts
def _file_attachments(meta: Dict[str, Any]) -> List[Attachment]:
    out = []
    for a in meta.get("attachments") or []:
        if isinstance(a, dict):
            size = a.get("size")
            mime = a.get("mime_type") or a.get("mimeType")
            out.append(Attachment(name=a.get("name"), mime=mime, size_bytes=size if isinstance(size, int) else None,
                                  kind="image" if str(mime or "").startswith("image/") else "file"))
    return out
def _tree(mapping: Dict[str, Any], current: Optional[str]) -> Tuple[List[str], set, Dict[str, int]]:
    nodes = {k: v for k, v in mapping.items() if isinstance(v, dict)}
    counts: Dict[str, int] = {}
    parent = {}
    for k, v in nodes.items():
        p = v.get("parent")
        parent[k] = p if isinstance(p, str) and p in nodes else None
        if isinstance(v.get("id"), str) and v["id"] != k:
            bump(counts, "id_key_mismatch")
        if isinstance(p, str) and p not in nodes:
            bump(counts, "missing_parent")
    roots = [k for k, p in parent.items() if p is None]
    counts["roots"] = len(roots)
    children: Dict[Optional[str], List[str]] = {}
    for k, p in parent.items():
        children.setdefault(p, []).append(k)
    def created(k: str) -> float:
        m = nodes[k].get("message") if isinstance(nodes[k].get("message"), dict) else {}
        v = m.get("create_time")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    for k in children:
        children[k].sort(key=lambda x: (created(x), x))
    leaf = current if isinstance(current, str) and current in nodes else None
    if leaf is None:
        leaves = [k for k in nodes if not children.get(k)]
        leaf = max(leaves, key=lambda k: (created(k), k)) if leaves else None
        if nodes:
            bump(counts, "current_node_missing")
    main = set()
    cur, guard = leaf, 0
    while cur is not None and guard <= len(nodes):
        if cur in main:
            bump(counts, "cycle")
            break
        main.add(cur)
        cur = parent.get(cur)
        guard += 1
    order: List[str] = []
    seen = set()
    stack = list(reversed(sorted(roots, key=lambda x: (created(x), x))))
    while stack:
        k = stack.pop()
        if k in seen:
            bump(counts, "cycle")
            continue
        seen.add(k)
        order.append(k)
        stack.extend(reversed(children.get(k, [])))
    for k in nodes:
        if k not in seen:
            order.append(k)
            bump(counts, "unreachable")
    return order, main, counts
def conversation_to_session(conv: Dict[str, Any], *, keep_text: bool = True,
                            tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    mapping = conv.get("mapping") if isinstance(conv.get("mapping"), dict) else {}
    raw_id = conv.get("conversation_id") or conv.get("id")
    sid = as_uuid(raw_id, "chatgpt", raw_id or json.dumps(sorted(mapping.keys()))[:2000],
                  conv.get("create_time"))
    s = Session(id=sid, source=SOURCE, title=conv.get("title") or None, started_at=parse_ts(conv.get("create_time")))
    order, main, counts = _tree(mapping, conv.get("current_node"))
    default_model = conv.get("default_model_slug") if isinstance(conv.get("default_model_slug"), str) else None
    unknown: Dict[str, int] = {}
    roles: Dict[str, int] = {}
    edits = regens = 0
    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for k in order:
        node = mapping.get(k) or {}
        msg = node.get("message") if isinstance(node.get("message"), dict) else None
        if msg is not None:
            by_parent.setdefault(node.get("parent"), []).append(msg)
    for kids in by_parent.values():
        rs = [((m.get("author") or {}).get("role")) for m in kids]
        if len(kids) > 1:
            if all(r == "assistant" for r in rs):
                regens += 1
            elif any(r == "user" for r in rs):
                edits += 1
    pending: Dict[str, List[Turn]] = {}
    n = 0
    last_ts = s.started_at
    def add(role: str, kind: str, k: str, msg: Dict[str, Any]) -> Turn:
        nonlocal n, last_ts
        n += 1
        t = Turn(id="%s#%s" % (k, n), role=role, kind=kind, timestamp=parse_ts(msg.get("create_time")),
                 message_id=str(msg.get("id") or k), on_main_branch=k in main,
                 parent_id=(mapping.get(k) or {}).get("parent"))
        if t.timestamp is not None and (last_ts is None or t.timestamp > last_ts):
            last_ts = t.timestamp
        s.turns.append(t)
        return t
    for k in order:
        node = mapping.get(k) or {}
        msg = node.get("message") if isinstance(node.get("message"), dict) else None
        if msg is None:
            continue
        author = msg.get("author") if isinstance(msg.get("author"), dict) else {}
        role = str(author.get("role") or "")
        bump(roles, role or "none")
        meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        ct, text, atts = _content(msg)
        recipient = str(msg.get("recipient") or "all")
        model = meta.get("model_slug") if isinstance(meta.get("model_slug"), str) else None
        if role == "user":
            if meta.get("is_user_system_message"):
                t = add("system", "meta", k, msg)
                t.char_count = len(text)
                t.extra["custom_instructions"] = True
            elif meta.get("is_visually_hidden_from_conversation"):
                t = add("system", "meta", k, msg)
                t.char_count = len(text)
                t.extra["hidden"] = True
            elif ct in TEXT_TYPES or text:
                t = add("user", "prompt", k, msg)
                set_text(t, text, keep_text=keep_text)
                t.attachments = atts + _file_attachments(meta)
            else:
                bump(unknown, "user:" + ct)
        elif role == "assistant":
            if ct in THINK_TYPES:
                t = add("assistant", "thinking", k, msg)
                t.char_count = len(text) or json_size((msg.get("content") or {}).get("thoughts"))
                t.model = model or default_model
            elif recipient == "all":
                if not text.strip() and not atts:
                    continue
                t = add("assistant", "response", k, msg)
                set_text(t, text, keep_text=keep_text)
                t.model = model or default_model
                if meta.get("citations"):
                    t.extra["citations"] = len(meta["citations"]) if isinstance(meta["citations"], list) else 1
            else:
                t = add("assistant", "tool_call", k, msg)
                t.tool = make_tool(recipient, call_id=str(msg.get("id") or k),
                                   args=text if ct == "code" else {"content_type": ct})
                t.char_count = len(text)
                t.model = model or default_model
                pending.setdefault(recipient, []).append(t)
        elif role == "tool":
            name = str(author.get("name") or "tool")
            if ct in HIDDEN_TOOL_TYPES and not text:
                continue
            calls = pending.get(name) or next((v for v in pending.values() if v), None) or []
            call = calls.pop(0) if calls else None
            r = add("tool_result", "tool_result", k, msg)
            r.tool = make_tool(call.tool.name if call and call.tool else name,
                               call_id=call.tool.id if call and call.tool else str(msg.get("id") or k))
            r.tool.is_error = ct == "system_error" or str(msg.get("status") or "") == "error"
            set_text(r, text, keep_text=keep_text, limit=tool_result_chars)
            if call is None:
                c = Turn(id=r.id + ".c", role="assistant", kind="tool_call", timestamp=r.timestamp,
                         on_main_branch=r.on_main_branch, tool=make_tool(name, call_id=r.tool.id))
                s.turns.insert(len(s.turns) - 1, c)
        elif role == "system":
            if text.strip():
                t = add("system", "meta", k, msg)
                t.char_count = len(text)
        else:
            bump(unknown, "role:" + role)
    upd = parse_ts(conv.get("update_time"))
    s.ended_at = max([x for x in (last_ts, upd) if x is not None], default=None)
    s.metadata.update({
        "message_count": sum(roles.values()), "roles": roles, "tree": counts,
        "edit_branch_points": edits, "regenerate_branch_points": regens,
        "default_model_slug": default_model, "gizmo": bool(conv.get("gizmo_id")),
        "is_archived": conv.get("is_archived"), "id_was_uuid": as_uuid(raw_id, "x") == str(raw_id or "").lower(),
    })
    if unknown:
        s.metadata["unknown_content"] = unknown
    return s
Opener = Callable[[], IO[bytes]]
def _members(path: str, warnings: List[str]) -> Iterator[Tuple[str, Opener]]:
    if os.path.isdir(path):
        for dirpath, _dirs, files in os.walk(path):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, path), (lambda p=full: open(p, "rb"))
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    yield name, (lambda z=zf, n=name: z.open(n))
        return
    if os.path.isfile(path):
        yield os.path.basename(path), (lambda p=path: open(p, "rb"))
        return
    warnings.append("input not found")
def iter_chat_html(fh: IO[bytes]) -> Iterator[Any]:
    text = codecs.getreader("utf-8")(fh, errors="replace").read()
    i = text.find("jsonData")
    if i < 0:
        return
    j = text.find("[", i)
    if j < 0:
        return
    try:
        arr, _end = json.JSONDecoder().raw_decode(text, j)
    except ValueError:
        return
    if isinstance(arr, list):
        for x in arr:
            yield x
def iter_conversations(fh: IO[bytes]) -> Iterator[Any]:
    for x in iter_json_items(fh):
        yield x
def parse_chatgpt_export(paths: Union[str, Sequence[str]], *, keep_text: bool = True,
                         tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> ChatGPTExport:
    if isinstance(paths, (str, os.PathLike)):
        paths = [str(paths)]
    out = ChatGPTExport()
    seen: set = set()
    found_json = False
    for p in paths:
        for name, _opener in _members(str(p), []):
            out.files_seen.append(name)
            if is_conversations_name(name):
                found_json = True
    for p in paths:
        for name, opener in _members(str(p), out.warnings):
            base = os.path.basename(name.replace("\\", "/")).lower()
            if base in NEVER_OPEN:
                continue
            if base == CHAT_HTML and not found_json:
                with opener() as fh:
                    _take(iter_chat_html(fh), out, seen, keep_text, tool_result_chars)
                out.used_chat_html = True
                continue
            if not is_conversations_name(base):
                continue
            try:
                with opener() as fh:
                    _take(iter_conversations(fh), out, seen, keep_text, tool_result_chars)
            except ValueError as exc:
                out.warnings.append("%s: invalid JSON (%s)" % (base, type(exc).__name__))
    return out
def _take(items: Iterator[Any], out: ChatGPTExport, seen: set, keep_text: bool, limit: Optional[int]) -> None:
    for conv in items:
        if not looks_like_chatgpt(conv):
            continue
        cid = str(conv.get("conversation_id") or conv.get("id") or "")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        try:
            out.sessions.append(conversation_to_session(conv, keep_text=keep_text, tool_result_chars=limit))
        except Exception as exc:
            out.warnings.append("conversation skipped: %s" % type(exc).__name__)
__all__ = ["parse_chatgpt_export", "conversation_to_session", "iter_conversations", "iter_chat_html",
           "looks_like_chatgpt", "is_conversations_name", "ChatGPTExport", "SOURCE", "NEVER_OPEN"]
