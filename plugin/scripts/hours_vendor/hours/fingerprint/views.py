from __future__ import annotations
import base64
import json
import os
import re
import zlib
from typing import Any, Dict, Iterable, List, Optional
from .core import short_hash, text_hash
from .skeleton import record_hash, record_key
VIEW_VERSION = 1
DEMO_TYPE = "x-hours-demo"
CC_CONVERSATION = ("user", "assistant", "attachment", "system")
KEY_NAME = re.compile(r"^[A-Za-z0-9_$.:-]{1,64}$")
BRACE = re.compile(r"\{[^{}\s]{1,40}\}")
STR_CAP = 120
BLOB_KEEP = 256
BLOB_END = 64
MAX_SHAPES = 50
def _sc(v: Any, cap: int = STR_CAP) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v[:cap]
    return {"#": short_hash(v, 16)}
def _num(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return "x"
def _nums(d: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    return {k: _num(d[k]) for k in keys if k in d}
def _shape(tok: str) -> str:
    out = []
    for ch in tok:
        if "a" <= ch <= "z":
            out.append("a")
        elif "A" <= ch <= "Z":
            out.append("A")
        elif "0" <= ch <= "9":
            out.append("9")
        elif ch in "{}_":
            out.append(ch)
        else:
            out.append(".")
    return "".join(out)
def text_view(text: str) -> Dict[str, Any]:
    s = text.lstrip()
    words = s.split()
    shapes: List[str] = []
    for m in BRACE.finditer(text):
        sh = _shape(m.group(0))
        if sh not in shapes:
            shapes.append(sh)
            if len(shapes) >= MAX_SHAPES:
                break
    return {"h": text_hash(text), "n": len(text), "f": s[:1], "w": short_hash(" ".join(words[:2]), 12) if words else "",
            "b": shapes}
def _join(content: Any, sep: str) -> str:
    if isinstance(content, str):
        return content
    try:
        return sep.join(str(b.get("text") or "") for b in (content or []) if isinstance(b, dict))
    except TypeError:
        return ""
def blob_view(sig: Any, full: bool) -> Any:
    if not sig:
        return sig if isinstance(sig, (str, type(None))) else None
    if not isinstance(sig, str):
        return 1
    if full or len(sig) <= BLOB_KEEP:
        return sig
    odd = sorted(set(ch for ch in sig if not (ch.isascii() and ch.isalnum())))
    return {"n": len(sig), "h": sig[:BLOB_END], "t": sig[-BLOB_END:], "c": "".join(odd[:16]), "q": sig.count("=")}
def folder_key(cwd: str) -> str:
    return short_hash("".join(ch if ch.isalnum() and ch.isascii() else "-" for ch in cwd), 16)
def _ids(v: Dict[str, Any], src: Dict[str, Any], keys: Iterable[str]) -> None:
    for k in keys:
        if k in src:
            v[k] = _sc(src[k])
def _head(v: Dict[str, Any], source: str, rec: Dict[str, Any]) -> None:
    h = record_hash(source, rec)
    if h:
        v["#h"] = h
        v["#k"] = record_key(source, rec)
def _demo(v: Dict[str, Any], rec: Dict[str, Any]) -> Dict[str, Any]:
    sig = rec.get("hours_demo_sig")
    if isinstance(sig, str):
        v["hours_demo_sig"] = sig[:200]
    return v
def _cc_block(b: Any, full: bool) -> Any:
    if not isinstance(b, dict):
        return 0
    bt = b.get("type")
    o: Dict[str, Any] = {"type": _sc(bt)}
    if bt == "thinking":
        o["signature"] = blob_view(b.get("signature"), full)
        th = b.get("thinking")
        o["thinking"] = ("1" if th.strip() else "") if isinstance(th, str) else ("1" if th else "")
    elif bt in ("tool_use", "server_tool_use"):
        if "id" in b:
            o["id"] = _sc(b["id"])
    elif bt == "text":
        o["text"] = text_hash(b.get("text"))
    return o
def cc_view(rec: Any, full: bool = False) -> Any:
    if not isinstance(rec, dict):
        return 1
    typ = rec.get("type")
    v: Dict[str, Any] = {"type": _sc(typ)}
    if typ == DEMO_TYPE:
        return _demo(v, rec)
    _head(v, "claude_code", rec)
    if typ not in CC_CONVERSATION:
        return v
    for k in rec:
        if isinstance(k, str) and KEY_NAME.match(k) and k not in v:
            v[k] = None
    _ids(v, rec, ("uuid", "parentUuid", "timestamp", "sessionId", "version", "agentId", "entrypoint", "requestId"))
    if "cwd" in rec:
        c = rec["cwd"]
        v["cwd"] = folder_key(c) if isinstance(c, str) and c else _sc(c)
    for k in ("isMeta", "isCompactSummary", "isSidechain", "isApiErrorMessage"):
        if k in rec:
            v[k] = bool(rec[k])
    origin = rec.get("origin")
    if isinstance(origin, dict):
        v["origin"] = {"kind": _sc(origin["kind"])} if "kind" in origin else {}
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return v
    m: Dict[str, Any] = {}
    if typ == "assistant":
        _ids(m, msg, ("id", "model"))
        u = msg.get("usage")
        if isinstance(u, dict):
            uv = _nums(u, ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            split = u.get("cache_creation")
            if isinstance(split, dict):
                uv["cache_creation"] = _nums(split, ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
            m["usage"] = uv
        elif "usage" in msg:
            m["usage"] = None
        content = msg.get("content")
        if isinstance(content, list):
            m["content"] = [_cc_block(b, full) for b in content]
    elif typ == "user":
        content = msg.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            m["content"] = [{"type": "tool_result", "tool_use_id": _sc(b.get("tool_use_id")),
                             "is_error": bool(b.get("is_error"))}
                            for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        else:
            if isinstance(content, list):
                content = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            m["content"] = {"P": text_view(_join(content, " "))}
    v["message"] = m
    return v
def codex_view(rec: Any, full: bool = False) -> Any:
    if not isinstance(rec, dict):
        return 1
    typ = rec.get("type")
    v: Dict[str, Any] = {"type": _sc(typ)}
    if typ == DEMO_TYPE:
        return _demo(v, rec)
    _head(v, "codex", rec)
    _ids(v, rec, ("timestamp",))
    p = rec.get("payload")
    if isinstance(p, dict):
        pv: Dict[str, Any] = {}
        _ids(pv, p, ("type", "id", "timestamp", "cli_version", "model", "call_id", "role"))
        if p.get("type") == "message":
            texts = [c.get("text") for c in p.get("content") or [] if isinstance(c, dict) and isinstance(c.get("text"), str)] \
                if isinstance(p.get("content"), list) else []
            pv["content"] = {"P": text_view("\n".join(texts))}
        info = p.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            pv["info"] = {"total_token_usage": _nums(info["total_token_usage"], (
                "total_tokens", "input_tokens", "cached_input_tokens", "output_tokens"))}
        v["payload"] = pv
    return v
def _gemini_one(rec: Dict[str, Any]) -> Dict[str, Any]:
    if any(isinstance(k, str) and k.startswith("$") for k in rec):
        v: Dict[str, Any] = {"$": None}
        _head(v, "gemini_cli", rec)
        patch = rec.get("$patch")
        if isinstance(patch, dict):
            v["$patch"] = {"id": _sc(patch["id"])} if "id" in patch else {}
        rw = rec.get("$rewindTo")
        if isinstance(rw, str):
            v["$rewindTo"] = rw[:STR_CAP]
        return v
    v = {}
    _ids(v, rec, ("type", "sessionId", "startTime", "id", "timestamp", "model"))
    if (rec.get("type") or ("meta" if "sessionId" in rec else None)) == DEMO_TYPE:
        return _demo(v, rec)
    _head(v, "gemini_cli", rec)
    if "content" in rec:
        v["content"] = {"P": text_view(_join(rec.get("content"), " "))}
    tok = rec.get("tokens")
    if isinstance(tok, dict):
        v["tokens"] = _nums(tok, ("input", "output", "cached", "thoughts", "tool", "total"))
    return v
def gemini_view(rec: Any, full: bool = False) -> Any:
    if not isinstance(rec, dict):
        return 1
    v = _gemini_one(rec)
    msgs = rec.get("messages")
    if isinstance(msgs, list):
        head = {k: x for k, x in rec.items() if k != "messages"}
        hv = _gemini_one(head)
        if "#h" in hv:
            v["#hh"], v["#hk"] = hv["#h"], hv["#k"]
        v["messages"] = [_gemini_one(m) for m in msgs if isinstance(m, dict)]
    return v
def copilot_view(rec: Any, full: bool = False) -> Any:
    if not isinstance(rec, dict):
        return 1
    typ = rec.get("type")
    v: Dict[str, Any] = {"type": _sc(typ)}
    if typ == DEMO_TYPE:
        return _demo(v, rec)
    _head(v, "copilot_cli", rec)
    _ids(v, rec, ("id", "parentId", "timestamp"))
    data = rec.get("data")
    if isinstance(data, dict):
        dv: Dict[str, Any] = {}
        _ids(dv, data, ("sessionId", "copilotVersion", "model", "toolCallId"))
        if "outputTokens" in data:
            dv["outputTokens"] = _num(data["outputTokens"])
        if isinstance(data.get("content"), str):
            dv["content"] = {"P": text_view(data["content"])}
        reqs = data.get("toolRequests")
        if isinstance(reqs, list):
            dv["toolRequests"] = [({"toolCallId": _sc(r["toolCallId"])} if "toolCallId" in r else {})
                                  if isinstance(r, dict) else 0 for r in reqs]
        v["data"] = dv
    return v
def antigravity_view(rec: Any, full: bool = False) -> Any:
    if not isinstance(rec, dict):
        return 1
    typ = rec.get("type")
    v: Dict[str, Any] = {"type": _sc(typ)}
    if typ == DEMO_TYPE:
        return _demo(v, rec)
    _head(v, "antigravity", rec)
    _ids(v, rec, ("created_at", "step_index"))
    if isinstance(rec.get("content"), str):
        v["content"] = {"P": text_view(rec["content"])}
    return v
VIEW = {"claude_code": cc_view, "codex": codex_view, "gemini_cli": gemini_view, "copilot_cli": copilot_view,
        "antigravity": antigravity_view}
def line_views(source: str, lines: Iterable[Any], full: bool = False) -> List[Any]:
    fn = VIEW[source]
    out: List[Any] = []
    for raw in lines:
        if isinstance(raw, dict):
            out.append(fn(raw, full))
            continue
        if isinstance(raw, (bytes, str)):
            raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError, TypeError):
            out.append(0)
            continue
        out.append(fn(rec, full))
    return out
def cc_context(path: str) -> Dict[str, Any]:
    parts = os.path.normpath(path).split(os.sep)
    name = parts[-1]
    stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else name
    if "subagents" in parts:
        i = parts.index("subagents")
        parent = parts[i - 1] if i >= 1 else None
        aid = stem[len("agent-"):] if stem.startswith("agent-") else None
        return {"kind": "subagent", "file_stem": stem, "parent_session": parent, "agent_id": aid, "folder": None}
    folder = parts[-2] if len(parts) >= 2 else None
    return {"kind": "main", "file_stem": stem, "parent_session": None, "agent_id": None, "folder": folder}
def context(source: str, path: str) -> Dict[str, Any]:
    if source == "claude_code":
        ctx = cc_context(path)
        if ctx.get("folder") is not None:
            ctx["folder"] = short_hash(ctx["folder"], 16)
        return ctx
    parts = os.path.normpath(path).split(os.sep)
    if source in ("codex", "gemini_cli"):
        return {"file_name": os.path.basename(path)}
    if source == "copilot_cli":
        folder = parts[-2] if parts[-1] == "events.jsonl" and len(parts) >= 2 else None
        if folder is None and parts[-1].endswith(".jsonl"):
            folder = parts[-1][:-len(".jsonl")]
        return {"folder": folder}
    if source == "antigravity":
        i = parts.index("brain") if "brain" in parts else -1
        return {"conversation_id": parts[i + 1] if 0 <= i < len(parts) - 1 else None}
    return {}
def file_views(source: str, path: str, full: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"v": VIEW_VERSION, "s": source, "x": context(source, path), "r": []}
    try:
        if source == "claude_code":
            with open(path, "rb") as fh:
                out["r"] = line_views(source, fh, full)
            return out
        from ..parsers._common import open_binary
        with open_binary(path) as fh:
            if source == "gemini_cli" and path.endswith(".json"):
                data = fh.read()
                try:
                    obj = json.loads(data)
                except ValueError:
                    obj = None
                out["r"] = line_views(source, [obj] if isinstance(obj, dict) else data.splitlines(True), full)
            else:
                out["r"] = line_views(source, iter(fh.readline, b""), full)
    except Exception:
        out["r"] = []
        out["u"] = 1
    return out
def encode(doc: Dict[str, Any]) -> str:
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
def file_blob(source: str, path: str) -> str:
    return encode(file_views(source, path))
