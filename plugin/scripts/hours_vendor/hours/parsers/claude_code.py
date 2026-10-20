from __future__ import annotations
import json
import os
import re
from collections import Counter
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Sequence, Union
from ..model import Attachment, Session, Tokens, ToolInfo, Turn, categorize_tool, parse_ts
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
DEFAULT_TOOL_RESULT_CHARS = 4000
_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)
_REJECT_PREFIXES = (
    "The user doesn't want to proceed with this tool use",
    "The user doesn't want to take this action",
)
_INTERRUPT_TOOL = "[Request interrupted by user for tool use]"
_INTERRUPT = "[Request interrupted by user"
KNOWN_TYPES = {
    "user", "assistant", "attachment", "system", "summary",
    "queue-operation", "last-prompt", "custom-title", "ai-title", "agent-name",
    "atis-latch", "bridge-session", "mode", "relocated", "cost-state",
    "file-history-snapshot", "file-history-delta",
    "started", "result", "failed", "launched",
}
def iter_records(path: str, warnings: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]:
    bad = 0
    with open(path, "rb") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                bad += 1
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                bad += 1
    if bad and warnings is not None:
        warnings.append("%s: %d malformed line(s) skipped" % (os.path.basename(path), bad))
def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif b.get("type") == "tool_reference":
                    parts.append(str(b.get("tool_name", "")))
        return "\n".join(parts)
    if isinstance(content, dict):
        return _text_of([content])
    return str(content)
def _truncate(s: Optional[str], limit: Optional[int]) -> Optional[str]:
    if s is None or limit is None or len(s) <= limit:
        return s
    return s[:limit]
def _input_summary(name: str, inp: Any, limit: int = 300) -> Optional[str]:
    if not isinstance(inp, dict):
        return None
    n = (name or "").lower()
    val: Any = None
    if n in ("bash", "monitor", "powershell"):
        val = inp.get("command")
    elif n in ("read", "edit", "multiedit", "write", "notebookedit", "notebookread"):
        val = inp.get("file_path") or inp.get("notebook_path")
    elif n in ("grep", "glob"):
        val = inp.get("pattern")
    elif n == "websearch":
        val = inp.get("query")
    elif n == "webfetch":
        val = inp.get("url")
    elif n in ("agent", "task"):
        val = "%s: %s" % (inp.get("subagent_type") or "default", inp.get("description") or "")
    elif n == "skill":
        val = inp.get("skill") or inp.get("command")
    elif n == "workflow":
        val = inp.get("workflowName") or inp.get("scriptPath") or ("inline-script" if inp.get("script") else None)
    elif n == "sendmessage":
        val = inp.get("to") or inp.get("recipient")
    elif n == "todowrite":
        todos = inp.get("todos")
        val = "%d todos" % len(todos) if isinstance(todos, list) else None
    if val is None:
        val = "keys=" + ",".join(sorted(inp.keys()))
    return _truncate(str(val), limit)
_SKIP_RE = re.compile(r"(@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|@unittest\.skip|\b(it|test|describe)\.skip\(|"
                      r"\bx(it|describe|test)\(|\bt\.Skip\(|#\[ignore\]|@Disabled\b)")
_LINT_OFF_RE = re.compile(r"(eslint-disable|#\s*noqa|#\s*type:\s*ignore|@ts-ignore|@ts-nocheck|#\s*pylint:\s*disable|"
                          r"//\s*nolint|rubocop:disable)")
_ASSERT_RE = re.compile(r"(\bassert\b|\bexpect\(|\.assert[A-Z_]\w*\(|\bassert_\w+\(|\bshould\.)")
_TEST_DEF_RE = re.compile(r"(\bdef test_\w+|\bfunc Test\w+|\b(it|test)\(\s*['\"`]|#\[test\])")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
def _edit_flags(name: str, inp: Any) -> Dict[str, Any]:
    if not isinstance(inp, dict):
        return {}
    n = (name or "").lower()
    path = str(inp.get("file_path") or inp.get("notebook_path") or "")
    pairs: List[Tuple[str, str]] = []
    if n == "multiedit":
        for e in inp.get("edits") or []:
            if isinstance(e, dict):
                pairs.append((str(e.get("old_string") or ""), str(e.get("new_string") or "")))
    elif n == "write":
        pairs.append(("", str(inp.get("content") or "")))
    else:
        pairs.append((str(inp.get("old_string") or ""), str(inp.get("new_string") or inp.get("new_source") or "")))
    def more(rx: "re.Pattern", a: str, b: str) -> bool:
        return len(rx.findall(b)) > len(rx.findall(a))
    flags: Dict[str, Any] = {"test_file": bool(_TEST_PATH_RE.search(path.replace("\\", "/")))}
    if any(more(_SKIP_RE, o, w) for o, w in pairs):
        flags["adds_skip"] = True
    if any(more(_LINT_OFF_RE, o, w) for o, w in pairs):
        flags["adds_lint_disable"] = True
    if n != "write":
        if any(more(_ASSERT_RE, w, o) for o, w in pairs):
            flags["removes_assertions"] = True
        if any(more(_TEST_DEF_RE, w, o) for o, w in pairs):
            flags["removes_tests"] = True
    return flags
def _mcp_server(name: str) -> Optional[str]:
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[1]
    return None
def _tokens_from_usage(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    return Tokens(
        input=_i(u.get("input_tokens")),
        output=_i(u.get("output_tokens")),
        cache_read=_i(u.get("cache_read_input_tokens")),
        cache_creation=_i(u.get("cache_creation_input_tokens")),
        cache_creation_5m=_i(cc.get("ephemeral_5m_input_tokens")),
        cache_creation_1h=_i(cc.get("ephemeral_1h_input_tokens")),
    )
class _FileParser:
    def __init__(self, path: str, *, is_sidechain_file: bool, seen_messages: Optional[Set[str]],
                 tool_result_chars: Optional[int], keep_text: bool) -> None:
        self.path = path
        self.is_sidechain_file = is_sidechain_file
        self.seen_messages = seen_messages if seen_messages is not None else set()
        self.tool_result_chars = tool_result_chars
        self.keep_text = keep_text
        base = os.path.basename(path)
        sid = base[:-6] if base.endswith(".jsonl") else base
        if sid.startswith("agent-"):
            sid = sid[len("agent-"):]
        self.session = Session(id=sid, source="claude_code")
        self.legacy_sidechains: Dict[str, Session] = {}
        self.seen_uuids: Set[str] = set()
        self.usage_holder: Dict[str, Tokens] = {}
        self.tool_names: Dict[str, Tuple[str, Turn]] = {}
        self.agent_links: Dict[str, str] = {}
        self.workflow_links: Dict[str, str] = {}
        self.permission_mode: Optional[str] = None
        self.counts: Counter = Counter()
        self.attachment_types: Counter = Counter()
        self.unknown_types: Counter = Counter()
        self.titles: Dict[str, Optional[str]] = {"custom": None, "ai": None, "summary": None}
        self.first_ts = None
        self.last_ts = None
        self.meta: Dict[str, Any] = self.session.metadata
        self.mode_changes: List[Dict[str, Any]] = []
    def _target(self, rec: Dict[str, Any]) -> Session:
        if self.is_sidechain_file or not rec.get("isSidechain"):
            return self.session
        key = rec.get("agentId") or "legacy-sidechain"
        s = self.legacy_sidechains.get(key)
        if s is None:
            s = Session(id=str(key), source="claude_code", agent_id=rec.get("agentId"),
                        parent_session_id=self.session.id, metadata={"legacy_inline_sidechain": True})
            self.legacy_sidechains[key] = s
        return s
    def _base_turn(self, rec: Dict[str, Any], tid: str, role: str, kind: str) -> Turn:
        return Turn(
            id=tid,
            role=role,
            kind=kind,
            parent_id=rec.get("parentUuid"),
            timestamp=parse_ts(rec.get("timestamp")),
            permission_mode=self.permission_mode,
            is_sidechain=bool(rec.get("isSidechain")) or self.is_sidechain_file,
            agent_id=rec.get("agentId"),
        )
    def _set_text(self, turn: Turn, text: str, limit: Optional[int] = None) -> None:
        turn.char_count = len(text)
        turn.text = _truncate(text, limit) if self.keep_text else None
    def _note_common(self, rec: Dict[str, Any]) -> None:
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        for k_src, k_dst in (("cwd", "cwd"), ("gitBranch", "git_branch"), ("version", "version"),
                             ("entrypoint", "entrypoint"), ("slug", "slug"), ("userType", "user_type")):
            v = rec.get(k_src)
            if v and k_dst not in self.meta:
                self.meta[k_dst] = v
        v = rec.get("version")
        if v:
            self.meta.setdefault("versions", set()).add(v)
        if rec.get("sessionId") and "session_id" not in self.meta:
            self.meta["session_id"] = rec["sessionId"]
    def run(self) -> Session:
        warnings = self.session.warnings
        for rec in iter_records(self.path, warnings):
            try:
                self._handle(rec)
            except Exception as exc:
                self.counts["record_errors"] += 1
                if self.counts["record_errors"] <= 5:
                    warnings.append("record error (%s): %s" % (rec.get("type"), type(exc).__name__))
        return self._finish()
    def _handle(self, rec: Dict[str, Any]) -> None:
        rtype = rec.get("type")
        self.counts["records"] += 1
        self.counts["type:%s" % rtype] += 1
        if rtype not in KNOWN_TYPES:
            self.unknown_types[str(rtype)] += 1
        uuid = rec.get("uuid")
        if uuid:
            if uuid in self.seen_uuids:
                self.counts["duplicate_uuid"] += 1
                return
            self.seen_uuids.add(uuid)
        self._note_common(rec)
        h = getattr(self, "_on_" + str(rtype).replace("-", "_"), None)
        if h is not None:
            h(rec)
    def _on_assistant(self, rec: Dict[str, Any]) -> None:
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        target = self._target(rec)
        model = msg.get("model")
        mid = msg.get("id")
        rid = rec.get("requestId")
        is_err = bool(rec.get("isApiErrorMessage")) or model == "<synthetic>"
        effort = rec.get("perTurnEffort") if isinstance(rec.get("perTurnEffort"), str) else rec.get("effort")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or not content:
            content = [{"type": "text", "text": ""}]
        tokens_for_first: Optional[Tokens] = None
        usage = _tokens_from_usage(msg.get("usage"))
        usage_key = "%s|%s" % (mid, rid) if mid else None
        if usage is not None and not is_err:
            if usage_key and usage_key in self.usage_holder:
                self.usage_holder[usage_key].merge_max(usage)
            elif usage_key and usage_key in self.seen_messages:
                self.counts["usage_duplicate_cross_file"] += 1
            else:
                tokens_for_first = usage
                if usage_key:
                    self.usage_holder[usage_key] = usage
                    self.seen_messages.add(usage_key)
        u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            tid = rec.get("uuid") or "%s#%s" % (mid, i)
            if len(content) > 1:
                tid = "%s#%d" % (tid, i)
            if is_err:
                turn = self._base_turn(rec, tid, "system", "error")
                self._set_text(turn, _text_of([block]) if btype == "text" else "", 500)
                turn.extra["error"] = rec.get("error")
                turn.extra["api_error_status"] = rec.get("apiErrorStatus")
            elif btype == "text":
                turn = self._base_turn(rec, tid, "assistant", "response")
                self._set_text(turn, block.get("text") or "")
            elif btype in ("thinking", "redacted_thinking"):
                turn = self._base_turn(rec, tid, "assistant", "thinking")
                self._set_text(turn, block.get("thinking") or "")
                turn.extra["redacted"] = btype == "redacted_thinking" or not block.get("thinking")
            elif btype in ("tool_use", "server_tool_use"):
                name = str(block.get("name") or "")
                turn = self._base_turn(rec, tid, "assistant", "tool_call")
                inp = block.get("input")
                turn.tool = ToolInfo(
                    name=name, id=block.get("id"), input_summary=_input_summary(name, inp),
                    mcp_server=_mcp_server(name) or rec.get("attributionMcpServer"),
                    category=categorize_tool(name),
                )
                turn.char_count = len(json.dumps(inp)) if inp is not None else 0
                if self.keep_text and isinstance(inp, dict):
                    payload = inp.get("prompt") if name.lower() in ("agent", "task") else inp.get("plan")
                    if isinstance(payload, str):
                        turn.text = _truncate(payload, 4000)
                if name.lower() in ("edit", "multiedit", "write", "notebookedit"):
                    turn.extra["edit_flags"] = _edit_flags(name, inp)
                if isinstance(inp, dict) and inp.get("dangerouslyDisableSandbox"):
                    turn.extra["sandbox_disabled"] = True
                if isinstance(inp, dict) and inp.get("run_in_background") is True:
                    turn.extra["background"] = True
                if isinstance(inp, dict) and name.lower() in ("agent", "task") and inp.get("model"):
                    turn.extra["agent_model"] = str(inp.get("model"))[:40]
                if btype == "server_tool_use":
                    turn.extra["server_tool"] = True
                if block.get("id"):
                    self.tool_names[block["id"]] = (name, turn)
            else:
                turn = self._base_turn(rec, tid, "assistant", "meta")
                turn.extra["block_type"] = btype
                self.counts["assistant_block:%s" % btype] += 1
            turn.model = model
            turn.effort = effort
            turn.message_id = mid
            turn.request_id = rid
            turn.skill = rec.get("attributionSkill")
            turn.plugin = rec.get("attributionPlugin")
            if rec.get("attributionAgent"):
                turn.extra["attribution_agent"] = rec["attributionAgent"]
            if rec.get("attributionMcpTool"):
                turn.extra["attribution_mcp_tool"] = rec["attributionMcpTool"]
            if rec.get("isAbortedMidStream"):
                turn.extra["aborted_mid_stream"] = True
            if tokens_for_first is not None:
                turn.tokens = tokens_for_first
                tokens_for_first = None
                for k in ("service_tier", "speed", "inference_geo"):
                    if u.get(k):
                        turn.extra[k] = u[k]
                stu = u.get("server_tool_use")
                if isinstance(stu, dict) and any(stu.values()):
                    turn.extra["server_tool_use"] = {k: v for k, v in stu.items() if v}
                if msg.get("stop_reason"):
                    turn.extra["stop_reason"] = msg.get("stop_reason")
            target.turns.append(turn)
    def _on_user(self, rec: Dict[str, Any]) -> None:
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        target = self._target(rec)
        pm = rec.get("permissionMode")
        if isinstance(pm, str) and target is self.session:
            if self.permission_mode is not None and pm != self.permission_mode:
                mc = self._base_turn(rec, (rec.get("uuid") or "") + "#mode", "system", "mode_change")
                mc.extra.update({"what": "permission_mode", "from": self.permission_mode, "to": pm})
                mc.permission_mode = pm
                target.turns.append(mc)
                self.mode_changes.append({"timestamp": rec.get("timestamp"), "from": self.permission_mode, "to": pm})
            self.permission_mode = pm
        content = msg.get("content")
        blocks = content if isinstance(content, list) else None
        tool_results = [b for b in (blocks or []) if isinstance(b, dict) and b.get("type") == "tool_result"]
        if tool_results:
            self._on_tool_results(rec, target, tool_results)
            other = [b for b in blocks if not (isinstance(b, dict) and b.get("type") == "tool_result")]
            if not _text_of(other).strip():
                return
            content = other
        text = _text_of(content)
        images = [b for b in (blocks or []) if isinstance(b, dict) and b.get("type") in ("image", "document")]
        kind, role = self._classify_user_text(rec, text)
        turn = self._base_turn(rec, rec.get("uuid") or "", role, kind)
        turn.message_id = rec.get("promptId")
        if kind == "command":
            m = _CMD_NAME_RE.search(text)
            a = _CMD_ARGS_RE.search(text)
            turn.extra["command"] = (m.group(1).strip() if m else None)
            self._set_text(turn, a.group(1).strip() if a else "")
        elif kind == "shell":
            m = _BASH_INPUT_RE.search(text)
            self._set_text(turn, m.group(1) if m else text)
        elif kind in ("meta", "notification", "compaction"):
            self._set_text(turn, text, 2000)
        else:
            self._set_text(turn, text)
        for b in images:
            src = b.get("source") if isinstance(b.get("source"), dict) else {}
            turn.attachments.append(Attachment(kind="image" if b.get("type") == "image" else "file",
                                               mime=src.get("media_type")))
        if kind == "compaction":
            turn.extra["boundary"] = False
        if rec.get("sourceToolUseID"):
            turn.extra["source_tool_use_id"] = rec["sourceToolUseID"]
            name = self.tool_names.get(rec["sourceToolUseID"], ("", None))[0]
            if name == "Skill":
                turn.extra["skill_body"] = True
        origin = rec.get("origin")
        if isinstance(origin, dict) and origin.get("kind"):
            turn.extra["origin"] = origin.get("kind")
        for k in ("promptSource", "turnOrigin"):
            if rec.get(k):
                turn.extra[k] = rec[k]
        target.turns.append(turn)
    def _classify_user_text(self, rec: Dict[str, Any], text: str) -> Tuple[str, str]:
        t = text.lstrip()
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
        if rec.get("isCompactSummary"):
            return "compaction", "system"
        if t.startswith(_INTERRUPT):
            return "interrupt", "user"
        if rec.get("isMeta"):
            return "meta", "system"
        if "<command-name>" in t[:500]:
            return "command", "user"
        if t.startswith("<local-command-stdout>") or t.startswith("<local-command-stderr>") or t.startswith("<local-command-caveat>"):
            return "meta", "system"
        if t.startswith("<bash-input>"):
            return "shell", "user"
        if t.startswith("<bash-stdout>") or t.startswith("<bash-stderr>"):
            return "meta", "system"
        if origin.get("kind") in ("task-notification", "peer", "coordinator") or t.startswith("<task-notification>"):
            return "notification", "system"
        if t.startswith("This session is being continued from a previous conversation"):
            return "compaction", "system"
        return "prompt", "user"
    def _on_tool_results(self, rec: Dict[str, Any], target: Session, blocks: List[Dict[str, Any]]) -> None:
        tur = rec.get("toolUseResult")
        denial = rec.get("toolDenialKind")
        for i, b in enumerate(blocks):
            tuid = b.get("tool_use_id")
            name, call_turn = self.tool_names.get(tuid, ("unknown", None))
            text = _text_of(b.get("content"))
            tid = rec.get("uuid") or ""
            if len(blocks) > 1:
                tid = "%s#%d" % (tid, i)
            turn = self._base_turn(rec, tid, "tool_result", "tool_result")
            self._set_text(turn, text, self.tool_result_chars)
            info = ToolInfo(name=name, id=tuid, is_error=bool(b.get("is_error")),
                            mcp_server=_mcp_server(name), category=categorize_tool(name))
            head = text.lstrip()[:200]
            if denial:
                info.denial_kind = denial
                if denial == "cancelled":
                    info.is_interrupted = True
                else:
                    info.is_rejected = True
            elif head.startswith(_REJECT_PREFIXES):
                info.is_rejected = True
                info.denial_kind = "user-rejected"
            if _INTERRUPT_TOOL in head:
                info.is_interrupted = True
            if isinstance(tur, dict):
                if tur.get("interrupted") is True:
                    info.is_interrupted = True
                if tur.get("agentId"):
                    self.agent_links[tuid] = tur["agentId"]
                    info.spawned_agent_id = tur["agentId"]
                    if call_turn is not None and call_turn.tool:
                        call_turn.tool.spawned_agent_id = tur["agentId"]
                    for k in ("status", "totalTokens", "totalDurationMs", "totalToolUseCount", "agentType", "resolvedModel"):
                        if k in tur:
                            turn.extra["agent_" + k] = tur[k]
                if tur.get("runId"):
                    self.workflow_links[tuid] = tur["runId"]
                    turn.extra["workflow_run_id"] = tur["runId"]
                    if call_turn is not None:
                        call_turn.extra["workflow_run_id"] = tur["runId"]
                if "returnCodeInterpretation" in tur:
                    turn.extra["return_code_interpretation"] = tur["returnCodeInterpretation"]
            turn.tool = info
            if rec.get("sourceToolAssistantUUID"):
                turn.extra["source_assistant_uuid"] = rec["sourceToolAssistantUUID"]
            target.turns.append(turn)
    def _on_attachment(self, rec: Dict[str, Any]) -> None:
        a = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        atype = a.get("type")
        self.attachment_types[str(atype)] += 1
        target = self._target(rec)
        uid = rec.get("uuid") or ""
        if atype == "queued_command":
            mode = a.get("commandMode")
            prompt = a.get("prompt")
            text = _text_of(prompt)
            if a.get("isMeta"):
                kind, role = "meta", "system"
            elif mode == "task-notification" or text.lstrip().startswith("<task-notification>"):
                kind, role = "notification", "system"
            else:
                kind, role = self._classify_user_text({}, text)
            turn = self._base_turn(rec, uid, role, kind)
            self._set_text(turn, text, None if kind == "prompt" else 2000)
            turn.extra["queued"] = True
            if kind == "command":
                m = _CMD_NAME_RE.search(text)
                turn.extra["command"] = m.group(1).strip() if m else None
            target.turns.append(turn)
        elif atype in ("plan_mode", "plan_mode_exit", "ultra_effort_enter", "ultra_effort_exit"):
            turn = self._base_turn(rec, uid, "system", "mode_change")
            turn.extra["what"] = atype
            target.turns.append(turn)
        elif atype == "invoked_skills":
            names = [s.get("name") for s in a.get("skills") or [] if isinstance(s, dict) and s.get("name")]
            self.meta.setdefault("invoked_skills", Counter()).update(names)
        elif atype == "hook_additional_context":
            self.counts["hook_additional_context"] += 1
    def _on_system(self, rec: Dict[str, Any]) -> None:
        sub = rec.get("subtype")
        target = self._target(rec)
        uid = rec.get("uuid") or ""
        if sub == "compact_boundary":
            turn = self._base_turn(rec, uid, "system", "compaction")
            cm = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
            turn.extra.update({"boundary": True, "trigger": cm.get("trigger"), "pre_tokens": cm.get("preTokens"),
                               "post_tokens": cm.get("postTokens"), "logical_parent": rec.get("logicalParentUuid")})
            target.turns.append(turn)
        elif sub == "api_error":
            turn = self._base_turn(rec, uid, "system", "error")
            err = rec.get("error") if isinstance(rec.get("error"), dict) else {}
            turn.extra.update({"status": err.get("status"), "retry_attempt": rec.get("retryAttempt")})
            target.turns.append(turn)
        elif sub == "stop_hook_summary":
            self.counts["hook_runs"] += int(rec.get("hookCount") or 0)
            if rec.get("preventedContinuation"):
                self.counts["hook_prevented_continuation"] += 1
        else:
            turn = self._base_turn(rec, uid, "system", "meta")
            turn.extra["subtype"] = sub
            self._set_text(turn, _text_of(rec.get("content")), 1000)
            target.turns.append(turn)
    def _on_summary(self, rec: Dict[str, Any]) -> None:
        if isinstance(rec.get("summary"), str):
            self.titles["summary"] = rec["summary"]
    def _on_custom_title(self, rec: Dict[str, Any]) -> None:
        self.titles["custom"] = rec.get("customTitle") or self.titles["custom"]
    def _on_ai_title(self, rec: Dict[str, Any]) -> None:
        self.titles["ai"] = rec.get("aiTitle") or self.titles["ai"]
    def _on_agent_name(self, rec: Dict[str, Any]) -> None:
        self.meta["agent_name"] = rec.get("agentName")
    def _on_mode(self, rec: Dict[str, Any]) -> None:
        self.meta.setdefault("session_modes", Counter())[str(rec.get("mode"))] += 1
    def _on_relocated(self, rec: Dict[str, Any]) -> None:
        self.meta.setdefault("relocated_cwds", []).append(rec.get("relocatedCwd"))
    def _on_cost_state(self, rec: Dict[str, Any]) -> None:
        keep = ("totalCostUSD", "totalAPIDuration", "totalAPIDurationWithoutRetries", "totalToolDuration",
                "totalLinesAdded", "totalLinesRemoved", "totalDuration", "startTime", "hasUnknownModelCost")
        cs = {k: rec.get(k) for k in keep if k in rec}
        mu = rec.get("modelUsage")
        if isinstance(mu, dict):
            cs["models"] = sorted(mu.keys())
        self.meta["cost_state"] = cs
    def _on_bridge_session(self, rec: Dict[str, Any]) -> None:
        self.meta["remote_bridge"] = True
    def _on_queue_operation(self, rec: Dict[str, Any]) -> None:
        self.counts["queue:%s" % rec.get("operation")] += 1
    def _finish(self) -> Session:
        s = self.session
        s.started_at, s.ended_at = self.first_ts, self.last_ts
        s.title = self.titles["custom"] or self.titles["ai"] or self.titles["summary"]
        s.cwd = self.meta.get("cwd")
        s.project = os.path.basename(s.cwd.rstrip("/")) if s.cwd else None
        if "versions" in self.meta:
            self.meta["versions"] = sorted(self.meta["versions"])
        self.meta["record_counts"] = {k: v for k, v in self.counts.items()}
        self.meta["attachment_types"] = dict(self.attachment_types)
        if self.unknown_types:
            self.meta["unknown_record_types"] = dict(self.unknown_types)
        if self.mode_changes:
            self.meta["permission_mode_changes"] = self.mode_changes
        if "invoked_skills" in self.meta:
            self.meta["invoked_skills"] = dict(self.meta["invoked_skills"])
        if "session_modes" in self.meta:
            self.meta["session_modes"] = dict(self.meta["session_modes"])
        for side in self.legacy_sidechains.values():
            ts = side.timestamps()
            side.started_at, side.ended_at = (ts[0], ts[-1]) if ts else (None, None)
            side.cwd, side.project = s.cwd, s.project
            s.subagents.append(side)
        return s
def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "rb") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
def _parse_file(path: str, *, sidechain: bool, seen: Set[str], tool_result_chars: Optional[int],
                keep_text: bool) -> Tuple[Session, _FileParser]:
    fp = _FileParser(path, is_sidechain_file=sidechain, seen_messages=seen,
                     tool_result_chars=tool_result_chars, keep_text=keep_text)
    return fp.run(), fp
def parse_session_file(path: str, *, include_subagents: bool = True,
                       tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS,
                       keep_text: bool = True, seen_messages: Optional[Set[str]] = None) -> Session:
    seen = seen_messages if seen_messages is not None else set()
    session, fp = _parse_file(path, sidechain=False, seen=seen, tool_result_chars=tool_result_chars,
                              keep_text=keep_text)
    session.metadata["path_kind"] = "main"
    session.metadata["encoded_project_dir"] = os.path.basename(os.path.dirname(path))
    if include_subagents:
        _attach_subagents(session, fp, path[:-6] if path.endswith(".jsonl") else path, seen,
                          tool_result_chars, keep_text)
    return session
def _attach_subagents(session: Session, main_fp: _FileParser, session_dir: str, seen: Set[str],
                      tool_result_chars: Optional[int], keep_text: bool) -> None:
    sub_root = os.path.join(session_dir, "subagents")
    agents: Dict[str, Session] = {}
    parsers: Dict[str, _FileParser] = {}
    metas: Dict[str, Dict[str, Any]] = {}
    if os.path.isdir(sub_root):
        for dirpath, _dirs, files in os.walk(sub_root):
            for fn in sorted(files):
                if not (fn.startswith("agent-") and fn.endswith(".jsonl")):
                    continue
                p = os.path.join(dirpath, fn)
                sub, fp = _parse_file(p, sidechain=True, seen=seen, tool_result_chars=tool_result_chars,
                                      keep_text=keep_text)
                aid = fn[len("agent-"):-len(".jsonl")]
                sub.id = aid
                sub.agent_id = aid
                sub.parent_session_id = session.id
                sub.cwd = sub.cwd or session.cwd
                sub.project = sub.project or session.project
                meta = _load_json(p[:-len(".jsonl")] + ".meta.json")
                meta = meta if isinstance(meta, dict) else {}
                metas[aid] = meta
                rel = os.path.relpath(dirpath, sub_root)
                if rel.startswith("workflows"):
                    sub.metadata["workflow_run"] = os.path.basename(dirpath)
                    sub.metadata["path_kind"] = "workflow_subagent"
                else:
                    sub.metadata["path_kind"] = "subagent"
                for k_src, k_dst in (("agentType", "agent_type"), ("description", "description"),
                                     ("toolUseId", "parent_tool_use_id"), ("parentAgentId", "parent_agent_id"),
                                     ("spawnDepth", "spawn_depth"), ("requestShape", "request_shape"),
                                     ("workflowPhase", "workflow_phase"), ("model", "requested_model"),
                                     ("stoppedByUser", "stopped_by_user")):
                    if k_src in meta:
                        sub.metadata[k_dst] = meta[k_src]
                if not sub.title:
                    sub.title = meta.get("description")
                agents[aid] = sub
                parsers[aid] = fp
        wf_root = os.path.join(sub_root, "workflows")
        if os.path.isdir(wf_root):
            journals = {}
            for wf in sorted(os.listdir(wf_root)):
                jp = os.path.join(wf_root, wf, "journal.jsonl")
                if os.path.isfile(jp):
                    c: Counter = Counter(str(r.get("type")) for r in iter_records(jp))
                    journals[wf] = dict(c)
            if journals:
                session.metadata["workflow_journals"] = journals
    wf_dir = os.path.join(session_dir, "workflows")
    if os.path.isdir(wf_dir):
        runs = []
        for fn in sorted(os.listdir(wf_dir)):
            if fn.endswith(".json"):
                d = _load_json(os.path.join(wf_dir, fn))
                if isinstance(d, dict):
                    runs.append({k: d.get(k) for k in ("runId", "workflowName", "status", "agentCount", "durationMs",
                                                       "totalTokens", "totalToolCalls", "defaultModel", "startTime")})
        if runs:
            session.metadata["workflows"] = runs
    tool_to_agent: Dict[str, str] = {}
    for fp in [main_fp] + list(parsers.values()):
        tool_to_agent.update(fp.agent_links)
    for aid, meta in metas.items():
        if meta.get("toolUseId"):
            tool_to_agent[meta["toolUseId"]] = aid
    all_parsers = [main_fp] + list(parsers.values())
    tool_owner: Dict[str, Turn] = {}
    for fp in all_parsers:
        for tuid, (_name, call_turn) in fp.tool_names.items():
            tool_owner[tuid] = call_turn
    for tuid, aid in tool_to_agent.items():
        call = tool_owner.get(tuid)
        if call is not None and call.tool is not None:
            call.tool.spawned_agent_id = aid
        if aid in agents and "parent_tool_use_id" not in agents[aid].metadata:
            agents[aid].metadata["parent_tool_use_id"] = tuid
    for aid, sub in agents.items():
        parent_aid = sub.metadata.get("parent_agent_id")
        if parent_aid is None:
            tuid = sub.metadata.get("parent_tool_use_id")
            for paid, fp in parsers.items():
                if tuid and tuid in fp.tool_names and paid != aid:
                    parent_aid = paid
                    break
        if parent_aid and parent_aid in agents and parent_aid != aid:
            agents[parent_aid].subagents.append(sub)
            sub.parent_session_id = parent_aid
        else:
            session.subagents.append(sub)
    unlinked = [a for a, s in agents.items() if "parent_tool_use_id" not in s.metadata
                and "workflow_run" not in s.metadata]
    if unlinked:
        session.metadata["unlinked_subagents"] = len(unlinked)
def iter_session_files(project_dir: str) -> List[str]:
    try:
        names = sorted(os.listdir(project_dir))
    except OSError:
        return []
    return [os.path.join(project_dir, n) for n in names
            if n.endswith(".jsonl") and os.path.isfile(os.path.join(project_dir, n))]
def parse_project_dir(project_dir: str, *, include_subagents: bool = True,
                      tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS, keep_text: bool = True,
                      seen_messages: Optional[Set[str]] = None) -> List[Session]:
    seen = seen_messages if seen_messages is not None else set()
    return [parse_session_file(p, include_subagents=include_subagents, tool_result_chars=tool_result_chars,
                               keep_text=keep_text, seen_messages=seen)
            for p in iter_session_files(project_dir)]
def parse_projects_root(root: Union[str, Sequence[str]] = DEFAULT_ROOT, **kwargs: Any) -> Iterator[Session]:
    roots = [root] if isinstance(root, str) else list(root)
    best: Dict[str, str] = {}
    for r in roots:
        try:
            dirs = sorted(os.listdir(r))
        except OSError:
            continue
        for d in dirs:
            p = os.path.join(r, d)
            if not os.path.isdir(p):
                continue
            for f in iter_session_files(p):
                sid = os.path.splitext(os.path.basename(f))[0]
                if sid not in best or os.path.getsize(f) > os.path.getsize(best[sid]):
                    best[sid] = f
    seen: Set[str] = set()
    for f in sorted(best.values()):
        yield parse_session_file(f, seen_messages=seen, **kwargs)
def parse_path(path: str, **kwargs: Any) -> List[Session]:
    if os.path.isfile(path):
        return [parse_session_file(path, **kwargs)]
    if iter_session_files(path):
        return parse_project_dir(path, **kwargs)
    return list(parse_projects_root(path, **kwargs))
def iter_all(sessions: Iterable[Session]) -> Iterator[Session]:
    for s in sessions:
        for x in s.iter_sessions():
            yield x
