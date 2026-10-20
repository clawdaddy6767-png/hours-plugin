from __future__ import annotations
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Attachment, Session, Tokens, Turn, parse_ts
from ._common import (DEFAULT_TOOL_RESULT_CHARS, background_requested, as_uuid, bump, encode_project_dir, iter_jsonl, json_size, make_tool,
                      open_binary, parse_args, set_command, set_text, ZstdUnavailable)
SOURCE = "codex"
FILE_RE = re.compile(r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
                     r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
                     r"(?:_[0-9a-fA-F-]{36})?\.jsonl(?:\.zst)?$")
SUBDIRS = ("sessions", "archived_sessions")
RESPONSE_ITEM_TYPES = {"message", "agent_message", "reasoning", "function_call", "function_call_output",
                       "custom_tool_call", "custom_tool_call_output", "local_shell_call", "web_search_call",
                       "tool_search_call", "tool_search_output", "image_generation_call", "compaction",
                       "context_compaction", "compaction_trigger", "configuration_update", "additional_tools",
                       "other"}
INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions",
                     "<permissions instructions>", "<turn_aborted>", "<developer", "<system")
SHELL_PREFIX = "<user_shell_command>"
_EXIT_RE = re.compile(r"(?:Exit code|exit_code)\W{0,3}(-?\d+)", re.I)
def codex_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
def rollout_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    home = home or codex_home()
    out: List[Tuple[str, str]] = []
    for sub in SUBDIRS:
        root = os.path.join(home, sub)
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, files in os.walk(root):
            if dirpath.count(os.sep) - base_depth >= 4:
                dirnames[:] = []
            names = set(files)
            for f in sorted(files):
                m = FILE_RE.match(f)
                if not m:
                    continue
                if f.endswith(".zst") and f[:-4] in names:
                    continue
                out.append((m.group(2).lower(), os.path.join(dirpath, f)))
    return out
def thread_id_from_name(path: str) -> Optional[str]:
    m = FILE_RE.match(os.path.basename(path))
    return m.group(2).lower() if m else None
def _content_text(content: Any) -> Tuple[str, List[Attachment]]:
    if isinstance(content, str):
        return content, []
    texts: List[str] = []
    atts: List[Attachment] = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, dict):
                ct = str(c.get("type") or "")
                if isinstance(c.get("text"), str):
                    texts.append(c["text"])
                elif ct in ("input_image", "image", "output_image"):
                    atts.append(Attachment(kind="image"))
                elif ct in ("input_audio", "audio"):
                    atts.append(Attachment(kind="voice"))
                elif ct in ("input_file", "file"):
                    atts.append(Attachment(kind="file"))
    return "\n".join(texts), atts
def _patch_summary(patch: str) -> str:
    files = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch or "", re.M)
    return ("files=" + ",".join(f.strip() for f in files))[:300] if files else "patch"
def _shell_summary(args: Any) -> Optional[str]:
    a = parse_args(args)
    if isinstance(a, dict):
        cmd = a.get("command") if a.get("command") is not None else a.get("cmd")
        if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
            if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") and os.path.basename(cmd[0]) in ("bash", "sh", "zsh"):
                return cmd[2][:300]
            return " ".join(cmd)[:300]
        if isinstance(cmd, str):
            return cmd[:300]
    return None
def _output_text(out: Any) -> Tuple[str, Optional[int]]:
    exit_code: Optional[int] = None
    if isinstance(out, dict):
        if isinstance(out.get("content"), str):
            text = out["content"]
        elif isinstance(out.get("content"), list):
            text = _content_text(out["content"])[0]
        else:
            text = str(out.get("output") or "")
        if out.get("success") is False:
            exit_code = 1
        return text, exit_code
    text = out if isinstance(out, str) else ("" if out is None else str(out))
    parsed = parse_args(text)
    if isinstance(parsed, dict):
        md = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
        ec = md.get("exit_code", parsed.get("exit_code"))
        if isinstance(ec, int):
            exit_code = ec
        if isinstance(parsed.get("output"), str):
            text = parsed["output"]
    else:
        m = _EXIT_RE.search(text[:400])
        if m:
            try:
                exit_code = int(m.group(1))
            except ValueError:
                exit_code = None
    return text, exit_code
def _usage(u: Any) -> Optional[Tokens]:
    if not isinstance(u, dict):
        return None
    def i(k: str) -> int:
        v = u.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
    inp, cached = i("input_tokens"), i("cached_input_tokens")
    t = Tokens(input=max(0, inp - cached), output=i("output_tokens"), cache_read=cached,
               cache_creation=i("cache_write_input_tokens"))
    return t if (t.input or t.output or t.cache_read or t.cache_creation) else None
def _sort_by_time(turns: List[Turn]) -> None:
    keys: Dict[int, float] = {}
    last = 0.0
    for t in turns:
        if t.timestamp is not None:
            last = t.timestamp.timestamp()
        keys[id(t)] = last
    turns.sort(key=lambda t: keys[id(t)])
def _minus(a: Tokens, b: Tokens) -> Tokens:
    return Tokens(max(0, a.input - b.input), max(0, a.output - b.output), max(0, a.cache_read - b.cache_read),
                  max(0, a.cache_creation - b.cache_creation))
class _Parser:
    def __init__(self, path: str, keep_text: bool, tool_result_chars: Optional[int]) -> None:
        self.path = path
        self.keep_text = keep_text
        self.limit = tool_result_chars
        self.s = Session(id="", source=SOURCE)
        self.n = 0
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.approval: Optional[str] = None
        self.calls: Dict[str, Turn] = {}
        self.results: Dict[str, Turn] = {}
        self.pending_status: Dict[str, bool] = {}
        self.resp_first: Optional[Turn] = None
        self.carry: Optional[Tokens] = None
        self.prev_total = Tokens()
        self.prompt_events: List[Tuple[Any, str]] = []
        self.agent_events: List[Tuple[Any, str]] = []
        self.types: Dict[str, int] = {}
        self.unknown: Dict[str, int] = {}
        self.first_ts: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None
        self.meta_seen = False
    def turn(self, role: str, kind: str, ts: Any, text: Optional[str] = None) -> Turn:
        self.n += 1
        t = Turn(id="%s#%d" % (self.s.id or "codex", self.n), role=role, kind=kind, timestamp=parse_ts(ts))
        if text is not None:
            set_text(t, text, keep_text=self.keep_text)
        if role == "assistant":
            t.model = self.model
            t.effort = self.effort
            if self.resp_first is None:
                self.resp_first = t
                if self.carry is not None:
                    t.tokens, self.carry = self.carry, None
        if role == "user":
            t.permission_mode = self.approval
        self.s.turns.append(t)
        return t
    def run(self) -> Session:
        fid = thread_id_from_name(self.path)
        self.s.id = fid or ""
        warnings: List[str] = []
        first = True
        for rec in iter_jsonl(self.path, warnings, label=os.path.basename(self.path)):
            ts = rec.get("timestamp")
            d = parse_ts(ts)
            if d is not None:
                self.first_ts = d if self.first_ts is None or d < self.first_ts else self.first_ts
                self.last_ts = d if self.last_ts is None or d > self.last_ts else self.last_ts
            typ = rec.get("type")
            payload = rec.get("payload")
            if typ is None and first and rec.get("id") and "record_type" not in rec:
                self.on_meta(rec, ts)
            elif typ == "session_meta" and isinstance(payload, dict):
                self.on_meta(payload, ts, rec.get("git") if isinstance(rec.get("git"), dict) else None)
            elif typ == "turn_context" and isinstance(payload, dict):
                self.on_turn_context(payload, ts)
            elif typ == "response_item" and isinstance(payload, dict):
                self.on_item(payload, ts)
            elif typ == "event_msg" and isinstance(payload, dict):
                self.on_event(payload, ts)
            elif typ == "compacted":
                t = self.turn("system", "compaction", ts)
                t.extra.update({"boundary": True, "trigger": "codex"})
                msg = payload.get("message") if isinstance(payload, dict) else None
                t.char_count = len(msg) if isinstance(msg, str) else 0
            elif payload is None and isinstance(typ, str) and typ in RESPONSE_ITEM_TYPES:
                self.on_item(rec, ts)
            elif "record_type" in rec:
                bump(self.types, "state")
            else:
                bump(self.unknown, typ or "untyped")
            if isinstance(typ, str):
                bump(self.types, typ)
            first = False
        return self.finish(fid, warnings)
    def on_meta(self, p: Dict[str, Any], ts: Any, git: Optional[Dict[str, Any]] = None) -> None:
        self.meta_seen = True
        sid = p.get("id") or p.get("session_id")
        if sid:
            self.s.metadata["meta_id"] = str(sid).lower()
        self.s.started_at = parse_ts(p.get("timestamp")) or parse_ts(ts)
        self.s.cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else None
        src = p.get("source")
        self.s.metadata.update({
            "cli_version": p.get("cli_version"), "originator": p.get("originator"),
            "entry_source": src if isinstance(src, str) else ("subagent" if isinstance(src, dict) else None),
            "model_provider": p.get("model_provider"), "history_mode": p.get("history_mode"),
        })
        if isinstance(src, dict) or p.get("parent_thread_id"):
            self.s.metadata["is_subagent_thread"] = True
            if p.get("parent_thread_id"):
                self.s.parent_session_id = str(p["parent_thread_id"]).lower()
        if p.get("forked_from_id"):
            self.s.metadata["forked_from_id"] = str(p["forked_from_id"]).lower()
        g = git or (p.get("git") if isinstance(p.get("git"), dict) else None)
        if g:
            self.s.metadata["git_branch"] = g.get("branch")
        if p.get("cli_version"):
            self.s.metadata["versions"] = [str(p["cli_version"])]
    def on_turn_context(self, p: Dict[str, Any], ts: Any) -> None:
        if isinstance(p.get("model"), str):
            self.model = p["model"]
        if isinstance(p.get("effort"), str):
            self.effort = p["effort"]
        appr = p.get("approval_policy")
        if isinstance(appr, str) and appr != self.approval:
            if self.approval is not None:
                t = self.turn("system", "mode_change", ts)
                t.extra.update({"what": "permission_mode", "from": self.approval, "to": appr})
                t.permission_mode = appr
            self.approval = appr
        if not self.s.cwd and isinstance(p.get("cwd"), str):
            self.s.cwd = p["cwd"]
    def on_item(self, p: Dict[str, Any], ts: Any) -> None:
        it = p.get("type") or p.get("item_type")
        if it in ("message", "agent_message", "assistant_message"):
            role = p.get("role") or ("assistant" if it != "message" else "")
            text, atts = _content_text(p.get("content") if p.get("content") is not None else p.get("message"))
            if role == "user":
                s = text.lstrip()
                if s.startswith(SHELL_PREFIX):
                    t = self.turn("user", "shell", ts, re.sub(r"</?user_shell_command>", "", s).strip())
                elif s.startswith(INJECTED_PREFIXES):
                    t = self.turn("system", "meta", ts)
                    t.char_count = len(text)
                    t.extra["injected"] = True
                    return
                elif s.startswith("/"):
                    t = self.turn("user", "command", ts)
                    set_command(t, s, keep_text=self.keep_text)
                else:
                    t = self.turn("user", "prompt", ts, text)
                t.attachments = atts
                self.resp_first = None
            elif role in ("developer", "system"):
                t = self.turn("system", "meta", ts)
                t.char_count = len(text)
                t.extra["injected"] = True
            else:
                t = self.turn("assistant", "response", ts, text)
                if p.get("id"):
                    t.message_id = str(p["id"])
        elif it == "reasoning":
            t = self.turn("assistant", "thinking", ts)
            summ = p.get("summary")
            t.char_count = sum(len(x.get("text") or "") for x in summ if isinstance(x, dict)) \
                if isinstance(summ, list) else 0
            t.extra["encrypted"] = bool(p.get("encrypted_content"))
        elif it in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call",
                    "image_generation_call"):
            self.on_call(it, p, ts)
        elif it in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            self.on_output(p, ts)
        elif it in ("compaction", "context_compaction"):
            t = self.turn("system", "compaction", ts)
            t.extra.update({"boundary": True, "trigger": "codex"})
        else:
            bump(self.unknown, "response_item:%s" % it)
    def on_call(self, it: str, p: Dict[str, Any], ts: Any) -> None:
        call_id = str(p.get("call_id") or p.get("id") or "") or None
        bg = False
        if it == "local_shell_call":
            action = p.get("action") if isinstance(p.get("action"), dict) else {}
            tool = make_tool("local_shell", call_id=call_id)
            tool.input_summary = _shell_summary({"command": action.get("command")})
            size = json_size(action)
        elif it == "web_search_call":
            action = p.get("action") if isinstance(p.get("action"), dict) else {}
            tool = make_tool("web_search", call_id=call_id, args={"query": action.get("query") or ""})
            size = json_size(action)
        elif it == "custom_tool_call":
            name = str(p.get("name") or "custom_tool")
            raw = p.get("input")
            tool = make_tool(name, call_id=call_id)
            tool.input_summary = _patch_summary(raw) if name == "apply_patch" and isinstance(raw, str) \
                else make_tool(name, args=raw).input_summary
            size = json_size(raw)
        else:
            name = str(p.get("name") or it)
            args = p.get("arguments") if p.get("arguments") is not None else p.get("input")
            tool = make_tool(name, call_id=call_id, args=args)
            bg = background_requested(args)
            if tool.category == "shell":
                tool.input_summary = _shell_summary(args) or tool.input_summary
            if name == "apply_patch":
                a = parse_args(args)
                patch = a.get("input") if isinstance(a, dict) else (a if isinstance(a, str) else "")
                tool.input_summary = _patch_summary(patch if isinstance(patch, str) else "")
            size = json_size(args)
        t = self.turn("assistant", "tool_call", ts)
        t.tool = tool
        if bg:
            t.extra["background"] = True
        t.char_count = size
        if call_id:
            self.calls[call_id] = t
        if it in ("web_search_call", "image_generation_call") and p.get("status"):
            r = self.turn("tool_result", "tool_result", ts)
            r.tool = make_tool(tool.name, call_id=call_id)
            r.tool.is_error = str(p.get("status")).lower() in ("failed", "error")
    def on_output(self, p: Dict[str, Any], ts: Any) -> None:
        call_id = str(p.get("call_id") or "") or None
        call = self.calls.get(call_id or "")
        text, exit_code = _output_text(p.get("output"))
        t = self.turn("tool_result", "tool_result", ts)
        set_text(t, text, keep_text=self.keep_text, limit=self.limit)
        t.tool = make_tool(call.tool.name if call and call.tool else "unknown", call_id=call_id)
        failed = self.pending_status.pop(call_id or "", None)
        t.tool.is_error = bool(failed) if failed is not None else (exit_code is not None and exit_code != 0)
        if call_id:
            self.results[call_id] = t
    def on_event(self, p: Dict[str, Any], ts: Any) -> None:
        et = p.get("type")
        if et == "user_message":
            msg = p.get("message")
            self.prompt_events.append((ts, msg if isinstance(msg, str) else ""))
        elif et in ("agent_message", "assistant_message"):
            msg = p.get("message")
            self.agent_events.append((ts, msg if isinstance(msg, str) else ""))
        elif et == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            total = _usage(info.get("total_token_usage"))
            last = _usage(info.get("last_token_usage"))
            if last is None and total is not None:
                last = _minus(total, self.prev_total)
            if total is not None:
                self.prev_total = total
            if last is not None:
                if self.resp_first is not None and self.resp_first.tokens is None:
                    self.resp_first.tokens = last
                elif self.carry is not None:
                    self.carry = self.carry + last
                else:
                    self.carry = last
            self.resp_first = None
        elif et in ("exec_command_end", "patch_apply_end", "mcp_tool_call_end"):
            cid = str(p.get("call_id") or "")
            failed = False
            if et == "exec_command_end":
                ec = p.get("exit_code")
                failed = isinstance(ec, int) and ec != 0
            elif et == "patch_apply_end":
                failed = p.get("success") is False
            else:
                res = p.get("result")
                failed = isinstance(res, dict) and ("Err" in res or res.get("is_error") is True)
            if cid in self.results and self.results[cid].tool is not None:
                self.results[cid].tool.is_error = self.results[cid].tool.is_error or failed
            elif cid:
                self.pending_status[cid] = failed
        elif et == "turn_aborted":
            t = self.turn("system", "interrupt", ts)
            t.extra["reason"] = p.get("reason")
        elif et in ("error", "stream_error"):
            t = self.turn("system", "error", ts)
            msg = p.get("message")
            t.char_count = len(msg) if isinstance(msg, str) else 0
        elif et in ("context_compacted",):
            bump(self.types, "event:context_compacted")
        else:
            bump(self.types, "event:%s" % et)
    def finish(self, fid: Optional[str], warnings: List[str]) -> Session:
        s = self.s
        meta_id = s.metadata.get("meta_id")
        s.id = as_uuid(meta_id or fid, "codex", self.path)
        s.metadata["filename_id"] = fid
        s.metadata["filename_matches_meta"] = (fid == meta_id) if (fid and meta_id) else None
        s.metadata["compressed"] = self.path.endswith(".zst")
        prompts = [t for t in s.turns if t.role == "user" and t.kind in ("prompt", "command", "shell")]
        s.metadata["prompt_items"] = len(prompts)
        s.metadata["prompt_events"] = len(self.prompt_events)
        if not prompts and self.prompt_events:
            for ts, msg in self.prompt_events:
                t = Turn(id="%s#e%d" % (s.id, len(s.turns) + 1), role="user", kind="prompt", timestamp=parse_ts(ts))
                set_text(t, msg, keep_text=self.keep_text)
                s.turns.append(t)
            _sort_by_time(s.turns)
        if not any(t.kind == "response" for t in s.turns) and self.agent_events:
            for ts, msg in self.agent_events:
                t = Turn(id="%s#a%d" % (s.id, len(s.turns) + 1), role="assistant", kind="response",
                         timestamp=parse_ts(ts), model=self.model)
                set_text(t, msg, keep_text=self.keep_text)
                s.turns.append(t)
            _sort_by_time(s.turns)
        if self.carry is not None:
            for t in reversed(s.turns):
                if t.role == "assistant":
                    t.tokens = (t.tokens + self.carry) if t.tokens else self.carry
                    break
        for i, t in enumerate(s.turns):
            t.id = "%s#%d" % (s.id, i + 1)
            if s.metadata.get("is_subagent_thread"):
                t.is_sidechain = True
        s.started_at = s.started_at or self.first_ts
        s.ended_at = self.last_ts or s.started_at
        s.metadata["record_types"] = self.types
        if self.unknown:
            s.metadata["unknown_types"] = self.unknown
        s.metadata["encoded_project_dir"] = encode_project_dir(s.cwd)
        s.warnings.extend(warnings)
        if not self.meta_seen:
            s.warnings.append("no session_meta line")
        return s
def parse_rollout(path: str, *, keep_text: bool = True,
                  tool_result_chars: Optional[int] = DEFAULT_TOOL_RESULT_CHARS) -> Session:
    return _Parser(path, keep_text, tool_result_chars).run()
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _tid, path in rollout_files(home):
        try:
            yield parse_rollout(path, **kwargs)
        except ZstdUnavailable:
            continue
__all__ = ["parse_rollout", "rollout_files", "iter_sessions", "codex_home", "thread_id_from_name", "SOURCE",
           "ZstdUnavailable", "open_binary"]
