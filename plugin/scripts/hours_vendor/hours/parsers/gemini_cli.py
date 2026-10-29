from __future__ import annotations
import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, background_requested, as_uuid, bump, encode_project_dir, iter_jsonl, json_size, make_tool,
                      set_command, set_text)
SOURCE = "gemini_cli"
MAIN_RE = re.compile(r"^session-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2})-([A-Za-z0-9_-]{1,8})(?:-\d+)?\.jsonl?$")
UPDATE_KEYS = ("$set", "$patch", "$rewindTo")
MESSAGE_FIELDS = ("content", "displayContent", "toolCalls", "thoughts", "tokens", "model", "type", "timestamp")
def gemini_home() -> str:
    return os.path.expanduser("~/.gemini")
def session_files(home: Optional[str] = None) -> List[Tuple[str, str, List[str]]]:
    home = home or gemini_home()
    root = os.path.join(home, "tmp")
    out: List[Tuple[str, str, List[str]]] = []
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return out
    for proj in projects:
        chats = os.path.join(root, proj, "chats")
        try:
            names = sorted(os.listdir(chats))
        except OSError:
            continue
        stems = {}
        for n in names:
            m = MAIN_RE.match(n)
            if not m or not os.path.isfile(os.path.join(chats, n)):
                continue
            stem = n.rsplit(".", 1)[0]
            if n.endswith(".json") and (stem + ".jsonl") in names:
                continue
            stems[stem] = (n, m.group(2))
        subdirs = [n for n in names if os.path.isdir(os.path.join(chats, n))]
        for stem, (n, prefix) in sorted(stems.items()):
            subs: List[str] = []
            for d in subdirs:
                if d.startswith(prefix):
                    sd = os.path.join(chats, d)
                    try:
                        sub_names = sorted(os.listdir(sd))
                    except OSError:
                        continue
                    subs.extend(os.path.join(sd, f) for f in sub_names
                                if f.endswith(".jsonl") and os.path.isfile(os.path.join(sd, f)))
            out.append(("gemini_cli:%s:%s" % (proj, stem), os.path.join(chats, n), subs))
    return out
def _load_records(path: str, warnings: List[str]) -> Iterator[Dict[str, Any]]:
    if path.endswith(".json"):
        try:
            with open(path, "rb") as fh:
                obj = json.loads(fh.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            warnings.append("%s: invalid JSON" % os.path.basename(path))
            return
        if isinstance(obj, dict):
            msgs = obj.get("messages") if isinstance(obj.get("messages"), list) else []
            head = {k: v for k, v in obj.items() if k != "messages"}
            head["_legacy_json"] = True
            yield head
            for m in msgs:
                if isinstance(m, dict):
                    yield m
        return
    for rec in iter_jsonl(path, warnings):
        yield rec
def replay(path: str, warnings: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]],
                                                                      List[Dict[str, Any]], Dict[str, int]]:
    warnings = warnings if warnings is not None else []
    meta: Dict[str, Any] = {}
    msgs: List[Dict[str, Any]] = []
    gone: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    first = True
    for rec in _load_records(path, warnings):
        if "$set" in rec:
            bump(counts, "$set")
            upd = rec.get("$set") if isinstance(rec.get("$set"), dict) else {}
            if isinstance(upd.get("messages"), list):
                msgs = [m for m in upd["messages"] if isinstance(m, dict)]
            meta.update({k: v for k, v in upd.items() if k != "messages"})
        elif "$rewindTo" in rec:
            bump(counts, "$rewindTo")
            target = rec.get("$rewindTo")
            idx = next((i for i, m in enumerate(msgs) if m.get("id") == target), None)
            if idx is None:
                bump(counts, "rewind_target_missing")
            else:
                gone.extend(msgs[idx:])
                msgs = msgs[:idx]
        elif "$patch" in rec:
            bump(counts, "$patch")
            _apply_patch(msgs, gone, rec.get("$patch"), counts)
        elif first and "type" not in rec and ("sessionId" in rec or "startTime" in rec or rec.get("_legacy_json")):
            meta.update(rec)
        elif isinstance(rec.get("type"), str):
            mid = rec.get("id")
            hit = next((m for m in msgs if mid is not None and m.get("id") == mid), None)
            if hit is not None:
                hit.update(rec)
                bump(counts, "rewritten")
            else:
                msgs.append(dict(rec))
        else:
            bump(counts, "unknown_record")
        first = False
    return meta, msgs, gone, counts
def _apply_patch(msgs: List[Dict[str, Any]], gone: List[Dict[str, Any]], p: Any, counts: Dict[str, int]) -> None:
    if not isinstance(p, dict):
        bump(counts, "bad_patch")
        return
    by_id = {m.get("id"): m for m in msgs if m.get("id") is not None}
    if p.get("id") is not None:
        m = by_id.get(p["id"])
        if m is None:
            bump(counts, "patch_target_missing")
        else:
            for k in MESSAGE_FIELDS:
                if k in p:
                    m[k] = p[k]
            if isinstance(p.get("updates"), dict):
                m.update(p["updates"])
    upd = p.get("updates")
    if isinstance(upd, list):
        for u in upd:
            if isinstance(u, dict) and u.get("id") in by_id:
                by_id[u["id"]].update(u)
    rm = p.get("removeIds")
    if isinstance(rm, list) and rm:
        drop = set(x for x in rm if isinstance(x, (str, int)))
        gone.extend(m for m in msgs if m.get("id") in drop)
        msgs[:] = [m for m in msgs if m.get("id") not in drop]
    order = p.get("orderIds")
    if isinstance(order, list) and order:
        pos = {x: i for i, x in enumerate(order) if isinstance(x, (str, int))}
        msgs.sort(key=lambda m: pos.get(m.get("id"), len(pos)))
def _parts(content: Any) -> Tuple[str, List[Attachment]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    items = content if isinstance(content, list) else [content]
    texts: List[str] = []
    atts: List[Attachment] = []
    for p in items:
        if isinstance(p, str):
            texts.append(p)
        elif isinstance(p, dict):
            if isinstance(p.get("text"), str) and not p.get("thought"):
                texts.append(p["text"])
            for key in ("inlineData", "fileData"):
                d = p.get(key)
                if isinstance(d, dict):
                    mime = d.get("mimeType")
                    kind = "image" if str(mime or "").startswith("image/") else (
                        "voice" if str(mime or "").startswith("audio/") else "file")
                    atts.append(Attachment(mime=mime, kind=kind))
    return "\n".join(texts), atts
def _result_text(tc: Dict[str, Any]) -> str:
    rd = tc.get("resultDisplay")
    if isinstance(rd, str):
        return rd
    res = tc.get("result")
    texts: List[str] = []
    for p in (res if isinstance(res, list) else [res]):
        if isinstance(p, dict):
            fr = p.get("functionResponse")
            if isinstance(fr, dict):
                r = fr.get("response")
                if isinstance(r, dict):
                    for k in ("output", "llmContent", "error", "content"):
                        if isinstance(r.get(k), str):
                            texts.append(r[k])
                            break
                    else:
                        texts.append(json.dumps(r)[:4000])
            elif isinstance(p.get("text"), str):
                texts.append(p["text"])
        elif isinstance(p, str):
            texts.append(p)
    return "\n".join(texts)
def _tokens(t: Any) -> Optional[Tokens]:
    if not isinstance(t, dict):
        return None
    def i(k: str) -> int:
        v = t.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
    inp, cached = i("input"), i("cached")
    tok = Tokens(input=max(0, inp - cached) + i("tool"), output=i("output") + i("thoughts"), cache_read=cached)
    return tok if (tok.input or tok.output or tok.cache_read) else None
class _Builder:
    def __init__(self, sid: str, keep_text: bool, limit: Optional[int], sidechain: bool, agent_id: Optional[str]):
        self.sid = sid
        self.keep_text = keep_text
        self.limit = limit
        self.sidechain = sidechain
        self.agent_id = agent_id
        self.turns: List[Turn] = []
        self.types: Dict[str, int] = {}
    def add(self, mid: Any, role: str, kind: str, ts: Any, main: bool) -> Turn:
        t = Turn(id="%s#%s.%d" % (self.sid, mid, len(self.turns)), role=role, kind=kind, timestamp=parse_ts(ts),
                 message_id=str(mid) if mid is not None else None, on_main_branch=main,
                 is_sidechain=self.sidechain, agent_id=self.agent_id)
        self.turns.append(t)
        return t
    def message(self, m: Dict[str, Any], main: bool) -> None:
        typ = str(m.get("type") or "")
        bump(self.types, typ or "untyped")
        mid = m.get("id")
        ts = m.get("timestamp")
        if typ == "user":
            text, atts = _parts(m.get("displayContent") if m.get("displayContent") is not None else m.get("content"))
            s = text.lstrip()
            kind = "command" if s.startswith("/") else ("shell" if s.startswith("!") else "prompt")
            t = self.add(mid, "user", kind, ts, main)
            if kind == "command":
                set_command(t, s, keep_text=self.keep_text)
            else:
                set_text(t, s[1:].strip() if kind == "shell" else text, keep_text=self.keep_text)
            t.attachments = atts
        elif typ == "gemini":
            model = m.get("model") if isinstance(m.get("model"), str) else None
            first: Optional[Turn] = None
            for th in m.get("thoughts") or []:
                t = self.add(mid, "assistant", "thinking", (th or {}).get("timestamp") or ts, main)
                t.char_count = len(str((th or {}).get("description") or "")) if isinstance(th, dict) else 0
                t.model = model
                first = first or t
            text, _atts = _parts(m.get("content"))
            if text.strip():
                t = self.add(mid, "assistant", "response", ts, main)
                set_text(t, text, keep_text=self.keep_text)
                t.model = model
                first = first or t
            for tc in m.get("toolCalls") or []:
                if not isinstance(tc, dict):
                    continue
                cid = str(tc.get("id") or "") or None
                call = self.add(mid, "assistant", "tool_call", tc.get("timestamp") or ts, main)
                call.tool = make_tool(tc.get("name"), call_id=cid, args=tc.get("args"))
                if background_requested(tc.get("args")):
                    call.extra["background"] = True
                call.char_count = json_size(tc.get("args"))
                call.model = model
                first = first or call
                status = str(tc.get("status") or "").lower()
                if "result" in tc or "resultDisplay" in tc or status:
                    r = self.add(mid, "tool_result", "tool_result", tc.get("timestamp") or ts, main)
                    r.tool = make_tool(tc.get("name"), call_id=cid)
                    r.tool.is_error = status in ("error", "failed")
                    if status in ("cancelled", "canceled"):
                        r.tool.is_rejected = True
                        r.tool.denial_kind = "cancelled"
                    set_text(r, _result_text(tc), keep_text=self.keep_text, limit=self.limit)
            tok = _tokens(m.get("tokens"))
            if tok is not None:
                if first is None:
                    first = self.add(mid, "assistant", "response", ts, main)
                    first.model = model
                first.tokens = tok
        elif typ == "error":
            t = self.add(mid, "system", "error", ts, main)
            t.char_count = len(_parts(m.get("content"))[0])
        else:
            t = self.add(mid, "system", "meta", ts, main)
            t.char_count = len(_parts(m.get("content"))[0])
            t.extra["message_type"] = typ
def parse_gemini_cli_file(path: str, *, subagent_paths: Optional[List[str]] = None, keep_text: bool = True,
                          tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS,
                          _sidechain: bool = False) -> Session:
    warnings: List[str] = []
    meta, msgs, gone, counts = replay(path, warnings)
    m = MAIN_RE.match(os.path.basename(path))
    prefix = m.group(2) if m else None
    raw_id = meta.get("sessionId")
    sid = as_uuid(raw_id, "gemini_cli", path)
    agent_id = sid if _sidechain else None
    b = _Builder(sid, keep_text, tool_result_chars, _sidechain, agent_id)
    for msg in msgs:
        b.message(msg, True)
    for msg in gone:
        b.message(msg, False)
    s = Session(id=sid, source=SOURCE, turns=b.turns, started_at=parse_ts(meta.get("startTime")),
                ended_at=parse_ts(meta.get("lastUpdated")))
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    if stamps:
        s.started_at = s.started_at or min(stamps)
        s.ended_at = max([x for x in (s.ended_at, max(stamps)) if x is not None])
    dirs = meta.get("directories")
    if isinstance(dirs, list) and dirs and isinstance(dirs[0], str):
        s.cwd = dirs[0]
    project_folder = os.path.basename(os.path.dirname(os.path.dirname(path)))
    if _sidechain:
        project_folder = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    s.title = None
    s.metadata.update({
        "kind": meta.get("kind") or ("subagent" if _sidechain else "main"),
        "project_hash": meta.get("projectHash"),
        "legacy_json": bool(meta.get("_legacy_json")) or path.endswith(".json"),
        "filename_prefix": prefix,
        "filename_matches_meta": (str(raw_id or "").startswith(prefix) if (prefix and raw_id) else None),
        "updates": counts,
        "message_types": b.types,
        "abandoned_messages": len(gone),
        "encoded_project_dir": encode_project_dir(s.cwd) or ("gemini-" + project_folder if project_folder else None),
    })
    s.warnings.extend(warnings)
    if not raw_id:
        s.warnings.append("no sessionId in the metadata line")
    for sp in subagent_paths or []:
        try:
            sub = parse_gemini_cli_file(sp, keep_text=keep_text, tool_result_chars=tool_result_chars,
                                        _sidechain=True)
        except Exception as exc:
            s.warnings.append("sub-agent file skipped: %s" % type(exc).__name__)
            continue
        sub.parent_session_id = s.id
        sub.agent_id = sub.id
        s.subagents.append(sub)
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _key, path, subs in session_files(home):
        yield parse_gemini_cli_file(path, subagent_paths=subs, **kwargs)
__all__ = ["parse_gemini_cli_file", "session_files", "iter_sessions", "replay", "gemini_home", "SOURCE"]
