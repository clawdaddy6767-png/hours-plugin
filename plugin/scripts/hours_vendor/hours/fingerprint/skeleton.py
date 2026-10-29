from __future__ import annotations
from typing import Any, Dict, List, Optional
from .core import short_hash, text_hash
CC_CONVERSATION = ("user", "assistant", "attachment", "system")
def _num(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return None
def _usage(u: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(u, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: _num(u.get(k)) for k in keys if k in u}
def _blocks_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return text_hash(content)
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return text_hash("\n".join(parts)) if parts else None
    return None
def _cc(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    typ = rec.get("type")
    if typ not in CC_CONVERSATION or not rec.get("uuid"):
        return None
    sk: Dict[str, Any] = {"y": typ, "u": rec.get("uuid"), "p": rec.get("parentUuid"), "t": rec.get("timestamp"),
                          "s": rec.get("sessionId"), "sc": bool(rec.get("isSidechain"))}
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    if typ == "assistant":
        sk["m"] = msg.get("id")
        sk["r"] = rec.get("requestId")
        sk["md"] = msg.get("model")
        sk["us"] = _usage(msg.get("usage"))
        blocks: List[Any] = []
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                blocks.append(["text", text_hash(b.get("text"))])
            elif bt in ("tool_use", "server_tool_use"):
                blocks.append([bt, b.get("id"), b.get("name"), short_hash(b.get("input"), 12)])
            elif bt == "thinking":
                blocks.append(["thinking", short_hash(str(b.get("signature") or ""), 12),
                               text_hash(b.get("thinking"))])
            else:
                blocks.append([bt])
        sk["b"] = blocks
    elif typ == "user":
        content = msg.get("content")
        if isinstance(content, list):
            res = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    res.append([b.get("tool_use_id"), bool(b.get("is_error"))])
            if res:
                sk["tr"] = res
        sk["x"] = _blocks_text(content)
        if rec.get("promptId"):
            sk["pi"] = rec.get("promptId")
    elif typ == "attachment":
        att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        sk["a"] = att.get("type")
    elif typ == "system":
        sk["st"] = rec.get("subtype")
    return sk
def _codex(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    typ = rec.get("type")
    p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    sk: Dict[str, Any] = {"y": typ, "t": rec.get("timestamp"), "pt": p.get("type")}
    if typ == "session_meta":
        sk.update(id=p.get("id"), v=p.get("cli_version"), src=p.get("source"), ts=p.get("timestamp"))
    elif typ == "response_item":
        sk.update(role=p.get("role"), call=p.get("call_id"), name=p.get("name"),
                  x=_blocks_text([{"type": "text", "text": c.get("text")} for c in p.get("content") or []
                                  if isinstance(c, dict) and isinstance(c.get("text"), str)])
                  if isinstance(p.get("content"), list) else None,
                  args=short_hash(p.get("arguments"), 12) if p.get("arguments") is not None else None)
    elif typ == "event_msg":
        if p.get("type") == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            sk["tot"] = _usage_codex(info.get("total_token_usage"))
        else:
            sk["x"] = text_hash(p.get("message")) if isinstance(p.get("message"), str) else None
    elif typ == "turn_context":
        sk["md"] = p.get("model")
    return sk
def _usage_codex(u: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(u, dict):
        return None
    return {k: _num(v) for k, v in sorted(u.items()) if isinstance(v, (int, float))}
def _gemini(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if any(k.startswith("$") for k in rec):
        return {"upd": short_hash(rec, 16)}
    sk: Dict[str, Any] = {"id": rec.get("id") or rec.get("sessionId"), "t": rec.get("timestamp") or rec.get("startTime"),
                          "y": rec.get("type") or rec.get("kind"), "md": rec.get("model")}
    tok = rec.get("tokens")
    if isinstance(tok, dict):
        sk["tok"] = {k: _num(v) for k, v in sorted(tok.items()) if isinstance(v, (int, float))}
    content = rec.get("content")
    if isinstance(content, str):
        sk["x"] = text_hash(content)
    elif isinstance(content, list):
        sk["x"] = _blocks_text([{"type": "text", "text": c.get("text")} for c in content
                                if isinstance(c, dict) and isinstance(c.get("text"), str)])
    calls = rec.get("toolCalls")
    if isinstance(calls, list):
        sk["tc"] = [[c.get("id"), c.get("name"), c.get("status")] for c in calls if isinstance(c, dict)]
    return sk
def _copilot(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    sk: Dict[str, Any] = {"y": rec.get("type"), "id": rec.get("id"), "p": rec.get("parentId"),
                          "t": rec.get("timestamp")}
    if isinstance(data.get("content"), str):
        sk["x"] = text_hash(data.get("content"))
    for k in ("toolCallId", "model", "sessionId", "copilotVersion", "outputTokens"):
        if k in data:
            sk[k] = data.get(k)
    reqs = data.get("toolRequests")
    if isinstance(reqs, list):
        sk["tr"] = [[r.get("toolCallId"), r.get("name")] for r in reqs if isinstance(r, dict)]
    return sk
def _antigravity(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sk: Dict[str, Any] = {"i": rec.get("step_index"), "y": rec.get("type"), "s": rec.get("status"),
                          "t": rec.get("created_at"), "src": rec.get("source")}
    c = rec.get("content")
    if isinstance(c, str):
        sk["x"] = text_hash(c)
    elif c is not None:
        sk["x"] = short_hash(c, 12)
    return sk
_BY_SOURCE = {"claude_code": _cc, "codex": _codex, "gemini_cli": _gemini, "copilot_cli": _copilot,
              "antigravity": _antigravity}
def skeleton(source: str, rec: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(rec, dict):
        return None
    fn = _BY_SOURCE.get(source)
    if fn is None:
        return {"h": short_hash(rec, 16)}
    return fn(rec)
def record_hash(source: str, rec: Any) -> Optional[str]:
    sk = skeleton(source, rec)
    return short_hash(sk, 16) if sk is not None else None
def record_key(source: str, rec: Any) -> str:
    if not isinstance(rec, dict):
        return "00000000"
    if source == "claude_code":
        ident: Any = rec.get("uuid")
    elif source == "gemini_cli":
        ident = rec.get("id") or rec.get("sessionId") or skeleton(source, rec)
    elif source == "copilot_cli":
        ident = rec.get("id")
    elif source == "antigravity":
        ident = rec.get("step_index")
    elif source == "codex":
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ident = [rec.get("timestamp"), rec.get("type"), p.get("type"), p.get("call_id")]
    else:
        ident = None
    return short_hash(ident if ident is not None else skeleton(source, rec), 8)
