from __future__ import annotations
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, UUID_RE, as_uuid, bump, iter_jsonl, json_size, make_tool, parse_args,
                      set_command, set_text)
SOURCE = "antigravity"
APP_DIRS = ("antigravity-cli", "antigravity-ide", "antigravity")
LOGS = os.path.join(".system_generated", "logs")
USER_TYPES = {"USER_INPUT"}
PLANNER_TYPES = {"PLANNER_RESPONSE"}
META_TYPES = {"EPHEMERAL_MESSAGE", "CONVERSATION_HISTORY", "SYSTEM_MESSAGE", "CHECKPOINT", "KNOWLEDGE_ARTIFACTS",
              "MEMORY", "USER_MEMORY", "RETRIEVE_MEMORY", "TASK_BOUNDARY", "NOTIFY_USER", "ARTIFACT_SUMMARY",
              "MANAGER_FEEDBACK", "CONVERSATION_SUMMARY"}
ERROR_TYPES = {"ERROR_MESSAGE", "ERROR"}
INTERRUPT_STATUSES = {"CANCELED", "CANCELLED", "INTERRUPTED", "USER_CANCELED"}
def gemini_home() -> str:
    return os.path.expanduser("~/.gemini")
def conversation_dirs(home: Optional[str] = None) -> List[Tuple[str, str, str]]:
    home = home or gemini_home()
    out: List[Tuple[str, str, str]] = []
    for app in APP_DIRS:
        brain = os.path.join(home, app, "brain")
        try:
            ids = sorted(os.listdir(brain))
        except OSError:
            continue
        for cid in ids:
            logs = os.path.join(brain, cid, LOGS)
            if any(os.path.isfile(os.path.join(logs, f)) for f in ("transcript.jsonl", "transcript_full.jsonl")):
                out.append((cid.lower() if UUID_RE.match(cid) else cid, logs, app))
    return out
def transcript_paths(logs_dir: str) -> List[str]:
    return [p for p in (os.path.join(logs_dir, "transcript_full.jsonl"), os.path.join(logs_dir, "transcript.jsonl"))
            if os.path.isfile(p)]
def logs_dir_for(path: str) -> str:
    if os.path.isfile(path):
        return os.path.dirname(path)
    if os.path.isdir(os.path.join(path, LOGS)):
        return os.path.join(path, LOGS)
    return path
def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(x if isinstance(x, str) else str((x or {}).get("text") or "") for x in v
                         if isinstance(x, (str, dict)))
    if isinstance(v, dict) and isinstance(v.get("text"), str):
        return v["text"]
    return ""
def _call_parts(tc: Any) -> Tuple[str, Any, Optional[str]]:
    if not isinstance(tc, dict):
        return "unknown", None, None
    name = tc.get("name") or tc.get("tool_name") or tc.get("toolName") or tc.get("function") or "unknown"
    if isinstance(name, dict):
        name = name.get("name") or "unknown"
    args = None
    for k in ("args", "arguments", "arguments_json", "argumentsJson", "input", "parameters"):
        if tc.get(k) is not None:
            args = parse_args(tc[k])
            break
    cid = tc.get("id") or tc.get("call_id") or tc.get("tool_call_id") or tc.get("toolCallId")
    return str(name), args, (str(cid) if cid else None)
def _media(step: Dict[str, Any]) -> List[Attachment]:
    out = []
    for m in step.get("media") or []:
        if isinstance(m, dict):
            mime = m.get("mime_type") or m.get("mimeType")
            kind = "image" if str(mime or "").startswith("image/") else (
                "voice" if str(mime or "").startswith("audio/") else "file")
            out.append(Attachment(mime=mime, kind=kind))
    return out
def parse_antigravity_conversation(path: str, *, keep_text: bool = True,
                                   tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS,
                                   profile: Optional[str] = None) -> Session:
    logs = logs_dir_for(path)
    warnings: List[str] = []
    steps: List[Dict[str, Any]] = []
    used = None
    for p in transcript_paths(logs):
        steps = [r for r in iter_jsonl(p, warnings)]
        if steps:
            used = os.path.basename(p)
            break
    conv_dir = os.path.dirname(os.path.dirname(logs)) if logs.endswith(LOGS) else logs
    folder_id = os.path.basename(conv_dir)
    sid = as_uuid(folder_id, "antigravity", conv_dir)
    s = Session(id=sid, source=SOURCE)
    if profile is None:
        parts = os.path.normpath(conv_dir).split(os.sep)
        profile = next((x for x in parts if x in APP_DIRS), None)
    def key(st: Dict[str, Any]) -> Tuple[int, int]:
        si = st.get("step_index")
        return (0, int(si)) if isinstance(si, int) and not isinstance(si, bool) else (1, 0)
    order = sorted(range(len(steps)), key=lambda i: (key(steps[i]), i))
    types: Dict[str, int] = {}
    pending: List[Turn] = []
    truncated = 0
    idxs: List[int] = []
    n = 0
    def add(role: str, kind: str, st: Dict[str, Any]) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="%s#%d" % (sid, n), role=role, kind=kind, timestamp=parse_ts(st.get("created_at")))
        si = st.get("step_index")
        if isinstance(si, int) and not isinstance(si, bool):
            t.extra["step_index"] = si
        s.turns.append(t)
        return t
    for i in order:
        st = steps[i]
        typ = str(st.get("type") or "").upper()
        source = str(st.get("source") or "").upper()
        status = str(st.get("status") or "").upper()
        bump(types, typ or "untyped")
        if isinstance(st.get("step_index"), int):
            idxs.append(st["step_index"])
        if st.get("truncated_fields"):
            truncated += 1
        if typ in USER_TYPES:
            if status in ("", "DONE", "COMPLETED"):
                text = _text(st.get("content"))
                t = add("user", "command" if text.lstrip().startswith("/") else "prompt", st)
                if t.kind == "command":
                    set_command(t, text, keep_text=keep_text)
                else:
                    set_text(t, text, keep_text=keep_text)
                t.attachments = _media(st)
            else:
                t = add("system", "meta", st)
                t.extra["user_input_status"] = status
            continue
        if typ in PLANNER_TYPES:
            thinking = _text(st.get("thinking"))
            if thinking:
                t = add("assistant", "thinking", st)
                t.char_count = len(thinking)
            text = _text(st.get("content"))
            if text.strip():
                t = add("assistant", "response", st)
                set_text(t, text, keep_text=keep_text)
            for k, tc in enumerate(st.get("tool_calls") or []):
                name, args, cid = _call_parts(tc)
                c = add("assistant", "tool_call", st)
                c.tool = make_tool(name, call_id=cid or "%s.s%s.%d" % (sid, st.get("step_index"), k), args=args)
                c.char_count = json_size(args)
                pending.append(c)
            if status == "ERROR":
                e = add("system", "error", st)
                e.char_count = len(text)
            continue
        if typ in META_TYPES or (source.startswith("USER") and typ not in USER_TYPES):
            t = add("system", "meta", st)
            t.extra["step_type"] = typ
            continue
        if typ in ERROR_TYPES:
            t = add("system", "error", st)
            t.char_count = len(_text(st.get("content")))
            continue
        name = typ.lower() or "tool"
        call = next((c for c in pending if c.tool and c.tool.name.lower() == name), None)
        if call is None and pending:
            call = pending[0]
        if call is None:
            call = add("assistant", "tool_call", st)
            call.tool = make_tool(name, call_id="%s.s%s" % (sid, st.get("step_index")), args=st.get("args"))
            call.extra["from_step"] = True
        else:
            pending.remove(call)
        r = add("tool_result", "tool_result", st)
        r.tool = make_tool(call.tool.name if call.tool else name, call_id=call.tool.id if call.tool else None)
        r.tool.is_error = status in ("ERROR", "FAILED")
        r.tool.is_interrupted = status in INTERRUPT_STATUSES
        set_text(r, _text(st.get("content")), keep_text=keep_text, limit=tool_result_chars)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    s.started_at = min(stamps) if stamps else None
    s.ended_at = max(stamps) if stamps else None
    gaps = 0
    if idxs:
        srt = sorted(set(idxs))
        gaps = (srt[-1] - srt[0] + 1) - len(srt)
    s.metadata.update({
        "profile": profile, "transcript_file": used, "step_types": types, "steps": len(steps),
        "step_index_gaps": gaps, "truncated_steps": truncated,
        "partial": used == "transcript.jsonl" and truncated > 0,
        "folder_id": folder_id,
    })
    if s.metadata["partial"]:
        warnings.append("only the compact transcript was available and some steps were cut")
    s.warnings.extend(warnings)
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _cid, logs, app in conversation_dirs(home):
        yield parse_antigravity_conversation(logs, profile=app, **kwargs)
__all__ = ["parse_antigravity_conversation", "conversation_dirs", "transcript_paths", "iter_sessions",
           "logs_dir_for", "SOURCE", "APP_DIRS"]
