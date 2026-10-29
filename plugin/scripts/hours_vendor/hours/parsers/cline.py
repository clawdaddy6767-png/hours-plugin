from __future__ import annotations
import json
import os
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import DEFAULT_TOOL_RESULT_CHARS, bump, make_tool, set_text, stable_uuid, encode_project_dir
SOURCE = "cline"
EXTENSION_IDS = {"cline": "saoudrizwan.claude-dev", "roo_code": "rooveterinaryinc.roo-cline"}
EDITORS = ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Code - OSS")
UI_FILE = "ui_messages.json"
META_FILE = "task_metadata.json"
NEVER_READ = ("api_conversation_history.json", "context_history.json", "claude_messages.json", "settings.json")
_TOOL_CATEGORY = (
    ("read", "file_read"), ("list", "file_read"), ("view", "file_read"), ("definition", "file_read"),
    ("search", "search"), ("grep", "search"), ("fetch", "web"), ("web", "web"), ("browser", "web"),
    ("diff", "file_edit"), ("edit", "file_edit"), ("write", "file_edit"), ("create", "file_edit"),
    ("insert", "file_edit"), ("delete", "file_edit"), ("replace", "file_edit"), ("move", "file_edit"),
    ("command", "shell"), ("shell", "shell"), ("mcp", "mcp"), ("skill", "skill"), ("task", "agent"),
    ("agent", "agent"), ("to" "do", "plan"), ("plan", "plan"),
)
def _category(name: str) -> str:
    n = (name or "").lower()
    for key, cat in _TOOL_CATEGORY:
        if key in n:
            return cat
    return "other"
def storage_roots(home: Optional[str] = None, source: str = "cline") -> List[str]:
    home = home or os.path.expanduser("~")
    ext = EXTENSION_IDS[source]
    bases: List[str] = []
    if sys.platform == "darwin":
        bases = [os.path.join(home, "Library", "Application Support", e) for e in EDITORS]
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        bases = [os.path.join(appdata, e) for e in EDITORS]
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        bases = [os.path.join(cfg, e) for e in EDITORS]
    roots = [os.path.join(b, "User", "globalStorage", ext, "tasks") for b in bases]
    if source == "cline":
        roots.append(os.path.join(home, ".cline", "data", "tasks"))
    return [r for r in roots if os.path.isdir(r)]
def task_files(home: Optional[str] = None, source: str = "cline") -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for root in storage_roots(home, source):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n, UI_FILE)
            if os.path.isfile(p):
                out.append((n, p))
    return out
def _json_file(path: str) -> Any:
    try:
        with open(path, "rb") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
def _obj(text: Any) -> Dict[str, Any]:
    if isinstance(text, str) and text[:1] == "{":
        try:
            v = json.loads(text)
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}
    return {}
def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
def _cwd_for(task_id: str, task_dir: str, source: str) -> Optional[str]:
    if source == "roo_code":
        item = _json_file(os.path.join(task_dir, "history_item.json"))
        if isinstance(item, dict) and isinstance(item.get("workspace"), str):
            return item["workspace"]
        return None
    hist = _json_file(os.path.join(os.path.dirname(os.path.dirname(task_dir)), "state", "taskHistory.json"))
    if isinstance(hist, list):
        for it in hist:
            if isinstance(it, dict) and str(it.get("id")) == task_id:
                v = it.get("cwdOnTaskInitialization")
                return v if isinstance(v, str) else None
    return None
def parse_task(path: str, *, source: str = SOURCE, keep_text: bool = True,
               tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    ui = path if path.endswith(".json") else os.path.join(path, UI_FILE)
    task_dir = os.path.dirname(ui)
    task_id = os.path.basename(task_dir)
    warnings: List[str] = []
    msgs = _json_file(ui)
    if not isinstance(msgs, list):
        msgs = []
        warnings.append("ui_messages.json unreadable or not a list")
    s = Session(id="", source=source)
    types: Dict[str, int] = {}
    n = 0
    model: Optional[str] = None
    pending_tokens: List[Tokens] = []
    last_call: List[Optional[Turn]] = [None]
    meta = _json_file(os.path.join(task_dir, META_FILE))
    if isinstance(meta, dict) and isinstance(meta.get("model_usage"), list):
        for mu in meta["model_usage"]:
            if isinstance(mu, dict) and isinstance(mu.get("model_id"), str):
                model = mu["model_id"]
    def add(role: str, kind: str, ts: Any) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind, timestamp=parse_ts(ts))
        if role == "assistant":
            t.model = model
            if pending_tokens and kind in ("response", "tool_call", "thinking"):
                t.tokens = pending_tokens.pop(0)
        s.turns.append(t)
        return t
    def tool_call(name: str, ts: Any, args: Any, category: Optional[str] = None, server: Optional[str] = None) -> None:
        t = add("assistant", "tool_call", ts)
        t.tool = make_tool(name, args=args, mcp_server=server)
        t.tool.category = category or _category(name)
        t.char_count = len(json.dumps(args)) if args is not None else 0
        last_call[0] = t
    def tool_result(ts: Any, text: Any) -> None:
        r = add("tool_result", "tool_result", ts)
        c = last_call[0]
        r.tool = make_tool(c.tool.name if c and c.tool else "unknown")
        if c and c.tool:
            r.tool.category = c.tool.category
        set_text(r, text if isinstance(text, str) else "", keep_text=keep_text, limit=tool_result_chars)
    for m in msgs:
        if not isinstance(m, dict) or m.get("partial") is True:
            continue
        kind = m.get("say") if m.get("type") == "say" else m.get("ask")
        side = str(m.get("type") or "")
        ts = m.get("ts")
        text = m.get("text") if isinstance(m.get("text"), str) else ""
        bump(types, "%s:%s" % (side, kind))
        mi = m.get("modelInfo")
        if isinstance(mi, dict) and isinstance(mi.get("modelId"), str):
            model = mi["modelId"]
        if side == "say" and kind in ("task", "user_feedback"):
            t = add("user", "prompt", ts)
            set_text(t, text, keep_text=keep_text)
            for _img in m.get("images") or []:
                t.attachments.append(Attachment(kind="image"))
            for _f in m.get("files") or []:
                t.attachments.append(Attachment(kind="file"))
        elif kind == "api_req_started":
            info = _obj(text)
            tk = Tokens(input=_int(info.get("tokensIn")), output=_int(info.get("tokensOut")),
                        cache_read=_int(info.get("cacheReads")), cache_creation=_int(info.get("cacheWrites")))
            if tk.total:
                pending_tokens.append(tk)
            if info.get("cancelReason") or info.get("streamingFailedMessage"):
                add("system", "error", ts).char_count = len(str(info.get("streamingFailedMessage") or ""))
        elif kind in ("text", "completion_result", "plan_mode_respond", "act_mode_respond", "followup"):
            if kind in ("plan_mode_respond", "act_mode_respond", "followup"):
                obj = _obj(text)
                text = str(obj.get("response") or obj.get("question") or text) if obj else text
            if text.strip():
                r = add("assistant", "response", ts)
                set_text(r, text, keep_text=keep_text)
        elif kind == "reasoning":
            th = add("assistant", "thinking", ts)
            th.char_count = len(text) or len(str(m.get("reasoning") or ""))
        elif kind == "tool":
            obj = _obj(text)
            name = str(obj.get("tool") or "tool")
            args = {"path": obj["path"]} if obj.get("path") else ({"regex": obj["regex"]} if obj.get("regex") else None)
            tool_call(name, ts, args)
        elif kind == "command":
            tool_call("execute_command", ts, {"command": text}, "shell")
        elif kind in ("command_output", "mcp_server_response", "browser_action_result"):
            tool_result(ts, text)
        elif kind == "use_mcp_server":
            obj = _obj(text)
            tool_call(str(obj.get("toolName") or obj.get("type") or "mcp"), ts, None, "mcp",
                      str(obj.get("serverName") or "") or None)
        elif kind in ("browser_action_launch", "browser_action"):
            tool_call("browser_action", ts, None, "web")
        elif kind in ("error", "api_req_failed", "diff_error"):
            add("system", "error", ts).char_count = len(text)
        elif kind in ("condense", "summarize_task", "compaction", "condense_context"):
            c = add("system", "compaction", ts)
            c.extra.update({"boundary": True, "trigger": "cline"})
    sid = stable_uuid(source, task_id)
    s.id = sid
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    stamps = [t.timestamp for t in s.turns if t.timestamp is not None]
    if not stamps:
        first = next((parse_ts(m.get("ts")) for m in msgs if isinstance(m, dict) and m.get("ts")), None)
        stamps = [first] if first else []
    s.started_at = min(stamps) if stamps else None
    s.ended_at = max(stamps) if stamps else None
    s.cwd = _cwd_for(task_id, task_dir, source)
    s.project = os.path.basename(s.cwd.rstrip("/\\")) if s.cwd else None
    s.metadata.update({"event_types": types, "task_id": task_id, "model": model,
                       "encoded_project_dir": encode_project_dir(s.cwd)})
    s.warnings.extend(warnings)
    return s
def iter_sessions(home: Optional[str] = None, source: str = SOURCE, **kwargs: Any) -> Iterator[Session]:
    for _tid, p in task_files(home, source):
        yield parse_task(p, source=source, **kwargs)
__all__ = ["parse_task", "task_files", "storage_roots", "iter_sessions", "SOURCE", "EXTENSION_IDS", "NEVER_READ"]
