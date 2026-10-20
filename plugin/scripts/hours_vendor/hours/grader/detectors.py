from __future__ import annotations
import difflib
import hashlib
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from hours.model import Session, Turn
from .privacy import cut_head
VERIFY_RE = re.compile(r"\b(pytest|npm (run )?test|yarn test|pnpm test|go test|cargo test|jest|vitest|make test|"
                       r"tsc\b|eslint|ruff|mypy|npm run build|playwright|unittest|tox|mocha|rspec|phpunit|"
                       r"cargo build|go build|make build|gradle (test|build)|mvn (test|package)|screenshot)\b")
TEST_RE = re.compile(r"\b(pytest|npm (run )?test|yarn test|pnpm test|go test|cargo test|jest|vitest|make test|"
                     r"unittest|tox|mocha|rspec|phpunit|gradle test|mvn test|playwright)\b")
LINT_RE = re.compile(r"\b(eslint|ruff|mypy|tsc|flake8|pylint|golangci-lint)\b")
RUN_RE = re.compile(r"(^|[;&|]\s*)(python3?|node|npm run|npx|make|cargo run|go run|bash|sh|\./)")
CODE_EXEC_RE = re.compile(r"\b(python3?|node|Rscript|ruby|julia|sqlite3|psql|duckdb|jq|awk|bc)\b")
POLL_RE = re.compile(r"^\s*(ls|test\s+-[a-z]|\[\s+-[a-z]|git\s+status|ps\b|pgrep|sleep|wc\b|cat\b|grep|rg\b|"
                     r"curl\s+\S*(health|status|ping)|tail\b|head\b|which\b|echo\b)")
READONLY_RE = re.compile(r"^\s*(cat|less|more|head|tail|wc|find|ls|grep|rg|git\s+(status|log|diff|show)|"
                         r"pwd|which|echo|stat|file|du|df|date|env|printenv|ps|pgrep|lsof|sleep)\b")
GIT_RE = re.compile(r"\bgit\s+(commit|push|revert|reset|checkout|restore|branch|worktree|merge|rebase|stash)\b")
CLAIM_RE = re.compile(r"\b(fixed|implemented|updated|added|created|removed|resolved)\b", re.I)
MEMORY_PATH_RE = re.compile(r"(^|/)(CLAUDE\.md|CLAUDE\.local\.md|AGENTS\.md)$|(^|/)\.claude/|(^|/)memory/")
MEMORY_ASK_RE = re.compile(r"\b(remember|memory|claude\.md|agents\.md|note (this|that)|add (this|that|it) to|"
                           r"record (this|that)|save (this|that)|update (the )?(memory|notes|docs|rules))\b", re.I)
AGENT_ASK_RE = re.compile(r"\b(sub-?agents?|agents?|in parallel|parallel|fan out|spawn|workers?)\b", re.I)
VERIFY_ASK_RE = re.compile(r"\b(test|tests|pytest|build|lint|verify|check|typecheck|tsc|run it|run the|make sure)\b",
                           re.I)
SEARCH_ASK_RE = re.compile(r"\b(search|look up|lookup|web|google|research|fetch|url|source|sources|docs)\b", re.I)
URL_RE = re.compile(r"https?://\S+")
FILE_REF_RE = re.compile(r"(?<!\S)@[\w./~-]+")
BASENAME_RE = re.compile(r"(?<![\w/.-])([\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,5})\b")
PROD_RE = re.compile(r"(\bprod\b|\bproduction\b|--prod\b|\blive\b|\bmain\b|\bmaster\b)", re.I)
RISKY: List[Tuple[str, "re.Pattern[str]"]] = [
    ("destructive_fs", re.compile(r"\brm\s+-[a-zA-Z]*(rf|fr)[a-zA-Z]*\b")),
    ("force_push", re.compile(r"\bgit\s+push\b.*(\s-f\b|--force)")),
    ("reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b")),
    ("db_drop", re.compile(r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE)\b", re.I)),
    ("iac_destroy", re.compile(r"\b(terraform|pulumi|cdk)\s+destroy\b")),
    ("deploy", re.compile(r"\b(vercel(\s+--prod)?|netlify\s+deploy|fly\s+deploy|heroku\s+|kubectl\s+(apply|delete)|"
                          r"helm\s+(upgrade|install)|terraform\s+apply|firebase\s+deploy|npm\s+publish|"
                          r"docker\s+push|gcloud\s+\S*\s*deploy|render\s+deploy|deploy\.sh)\b")),
    ("migration", re.compile(r"\b(alembic\s+upgrade|prisma\s+migrate|db:migrate|knex\s+migrate|manage\.py\s+migrate)\b")),
    ("credential_read", re.compile(r"\b(cat|less|more|head|tail|grep|printenv|env)\b.*(\.env\b|credentials|\.aws/|"
                                   r"\.ssh/|id_rsa|\.netrc|keychain)|security\s+find-(generic|internet)-password")),
    ("chmod_777", re.compile(r"\bchmod\s+(-R\s+)?777\b")),
]
DESTRUCTIVE_KINDS = {"destructive_fs", "force_push", "reset_hard", "db_drop", "iac_destroy"}
CORRECTION_CUES = ["no", "not", "don't", "dont", "wrong", "instead", "actually", "revert", "undo", "stop",
                   "that's not", "thats not", "why did you"]
APPROVALS = ["yes", "y", "ok", "okay", "go ahead", "continue", "keep going", "do it", "proceed", "looks good",
             "thanks", "thank you", "sounds good", "next", "great", "perfect", "lgtm", "sure", "yep", "yup"]
_CUE_RE = re.compile(r"(?<![\w'])(" + "|".join(re.escape(c) for c in CORRECTION_CUES) + r")(?![\w'])", re.I)
BUILTIN_COMMANDS = {
    "clear", "compact", "rewind", "model", "effort", "fast", "context", "btw", "memory", "goal", "code-review",
    "security-review", "batch", "permissions", "sandbox", "help", "init", "config", "cost", "status", "login",
    "logout", "resume", "continue", "exit", "quit", "agents", "mcp", "hooks", "ide", "vim", "terminal-setup",
    "doctor", "bug", "review", "pr-comments", "export", "add-dir", "plugin", "plugins", "skills", "statusline",
    "theme", "usage", "upgrade", "release-notes", "privacy-settings", "output-style", "todos", "tasks", "bashes",
    "install-github-app", "artifacts", "loop", "schedule", "simplify", "fewer-permission-prompts", "keybindings",
    "keybindings-help", "update-config", "run", "plugin-authoring", "workflow-authoring", "claude-api",
    "artifact-design", "artifact-diagramming", "artifact-capabilities", "dataviz", "rename", "branch", "insights",
}
BUILTIN_AGENT_TYPES = {"general-purpose", "explore", "plan", "statusline-setup", "claude-code-guide", "default",
                       "output-style-setup", "fork"}
AUTOMATED_ORIGINS = {"scheduled", "cron", "schedule", "routine", "automation", "remote-trigger", "trigger",
                     "headless", "loop"}
BACKGROUND_TOOLS = {"monitor": "monitor", "schedulewakeup": "schedule", "croncreate": "cron", "taskcreate": "task",
                    "remotetrigger": "schedule"}
SCHEDULE_KINDS = ("schedule", "cron")
SCHEDULE_MCP_RE = re.compile(r"(create|add|update)_?(scheduled|schedule|cron|routine)", re.I)
CHECK_TOOLS = {"taskoutput", "bashoutput", "taskget", "monitor_output"}
BACKGROUND_LONG_S = 60
AUTONOMY_STRETCH = 8
STREAK_MIN = 3
OD6_MIN = 15
FAST_UNATTENDED_S = 600
def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())
def _sig(name: str, arg: str) -> str:
    return hashlib.sha1((name + "|" + _norm_ws(arg)).encode("utf-8")).hexdigest()[:8]
def basename(path: Optional[str]) -> str:
    p = (path or "").strip().rstrip("/")
    return os.path.basename(p) if p else ""
def _is_approval(text: str) -> bool:
    t = _norm_ws(text).strip(".!,")
    return any(t == a or (t.startswith(a + " ") and len(t.split()) <= 4) or (t.startswith(a + ",")
               and len(t.split()) <= 4) for a in APPROVALS)
def _words(text: str) -> int:
    return len((text or "").split())
class _Ctx:
    def __init__(self, turns: List[Turn], id_of: Dict[str, str]):
        self.turns = turns
        self.id_of = id_of
        self.result_of: Dict[str, Turn] = {}
        for t in turns:
            if t.kind == "tool_result" and t.tool and t.tool.id:
                self.result_of[t.tool.id] = t
    def rid(self, t: Turn) -> Optional[str]:
        return self.id_of.get(t.id)
    def outcome(self, t: Turn) -> str:
        r = self.result_of.get((t.tool.id or "") if t.tool else "")
        if r is None or r.tool is None:
            return "unknown"
        if r.tool.is_rejected:
            return "rejected"
        if r.tool.is_interrupted:
            return "interrupted"
        return "fail" if r.tool.is_error else "ok"
def _calls(ctx: _Ctx) -> List[Turn]:
    return [t for t in ctx.turns if t.kind == "tool_call" and t.tool and not t.is_sidechain and ctx.rid(t)]
def _is_edit(t: Turn) -> bool:
    return bool(t.tool) and (t.tool.category == "file_edit") and t.tool.name.lower() in (
        "edit", "multiedit", "write", "notebookedit", "create_file", "str_replace", "apply_patch")
def _is_read(t: Turn) -> bool:
    return bool(t.tool) and t.tool.name.lower() in ("read", "grep", "glob", "ls", "notebookread", "view")
def _shell_cmd(t: Turn) -> Optional[str]:
    if t.tool and t.tool.category == "shell":
        return t.tool.input_summary or ""
    return None
def compute(turns: List[Turn], id_of: Dict[str, str], session: Session,
            scrub: Callable[[Optional[str]], Optional[str]], secret_by_turn: Dict[str, Dict[str, int]],
            is_session_start: bool = False, is_session_end: bool = False) -> Dict[str, Any]:
    ctx = _Ctx(turns, id_of)
    calls = _calls(ctx)
    humans = [t for t in turns if t.is_human_input and ctx.rid(t)]
    m: Dict[str, Any] = {}
    m["M01_human_turns"] = len(humans)
    order = {t.id: i for i, t in enumerate(turns)}
    def last_human_before(t: Turn) -> Optional[Turn]:
        best = None
        for h in humans:
            if order[h.id] < order[t.id]:
                best = h
            else:
                break
        return best
    def human_text(h: Optional[Turn]) -> str:
        if h is None:
            return ""
        if h.kind == "command":
            return ((h.extra.get("command") or "") + " " + (h.text or ""))
        return h.text or ""
    ints = []
    tool_k = 0
    for i, t in enumerate(turns):
        if t.is_human_input:
            tool_k = 0
        elif t.kind == "tool_call" and not t.is_sidechain:
            tool_k += 1
        elif t.kind == "interrupt" and ctx.rid(t):
            nxt = [x for x in turns[i + 1:] if x.kind not in ("meta", "thinking", "notification", "tool_result")][:2]
            follow = next((x for x in nxt if x.is_human_input and x.kind == "prompt"), None)
            ints.append({"turn": ctx.rid(t), "after_tool_calls": tool_k, "correction_follows": follow is not None,
                         "next_user": ctx.rid(follow) if follow else None})
    m["M02_interrupts"] = ints
    cmds = []
    for h in humans:
        if h.kind == "command":
            name = (h.extra.get("command") or (h.text or "").split(" ")[0] or "").strip()
            bare = name.lstrip("/")
            entry = {"turn": ctx.rid(h), "name": name if name.startswith("/") else "/" + name,
                     "builtin": bare.split(":")[-1] in BUILTIN_COMMANDS and ":" not in bare}
            if bare in ("model", "effort", "fast", "permissions", "compact"):
                entry["args"] = cut_head(scrub((h.text or "").strip()) or "", 60)
            cmds.append(entry)
    m["M03_slash_commands"] = cmds
    m["M04_compactions"] = [{"turn": ctx.rid(t), "trigger": t.extra.get("trigger"),
                             "pre_tokens": t.extra.get("pre_tokens")}
                            for t in turns if t.kind == "compaction" and t.role == "system"
                            and t.extra.get("boundary") is not False and ctx.rid(t)]
    batches: Dict[str, Dict[str, Any]] = {}
    border: List[str] = []
    for t in calls:
        if t.tool.category != "agent" or t.tool.name.lower() not in ("agent", "task"):
            continue
        key = t.request_id or t.message_id or t.id
        h = last_human_before(t)
        atype = (t.tool.input_summary or "").split(":")[0].strip().lower()
        if key not in batches:
            init = "agent"
            if atype and atype not in BUILTIN_AGENT_TYPES:
                init = "user_configured"
            if AGENT_ASK_RE.search(human_text(h)):
                init = "user_requested"
            batches[key] = {"turn": ctx.rid(t), "briefs": [], "initiator": init}
            border.append(key)
        brief = scrub(t.text or t.tool.input_summary or "") or ""
        batches[key]["briefs"].append(cut_head(brief, 240))
        if t.extra.get("agent_model"):
            batches[key].setdefault("agent_models", []).append(t.extra["agent_model"])
    m["M05_agent_batches"] = [batches[k] for k in border]
    ver, code_exec, git, risky = [], [], [], []
    committed_at: List[int] = []
    for t in calls:
        cmd = _shell_cmd(t)
        if cmd is None:
            continue
        rid = ctx.rid(t)
        out = ctx.outcome(t)
        h = last_human_before(t)
        if VERIFY_RE.search(cmd):
            kind = "test" if TEST_RE.search(cmd) else "lint" if LINT_RE.search(cmd) else "build"
            init = "user_requested" if VERIFY_ASK_RE.search(human_text(h)) else "agent"
            ver.append({"turn": rid, "kind": kind, "exit": out, "initiator": init,
                        "cmd": cut_head(scrub(cmd) or "", 160), "sig": _sig("cmd", cmd)})
        elif CODE_EXEC_RE.search(cmd):
            code_exec.append({"turn": rid, "exit": out})
        g = GIT_RE.search(cmd)
        if g:
            files = [basename(x) for x in re.findall(r"(?:--\s+|\s)([\w./-]+\.[A-Za-z0-9]{1,6})\b", cmd)]
            git.append({"turn": rid, "op": g.group(1), "exit": out, "files": sorted(set(f for f in files if f))})
            if g.group(1) == "commit" and out == "ok":
                committed_at.append(order[t.id])
        for kind, rx in RISKY:
            if rx.search(cmd):
                risky.append({"turn": rid, "kind": kind, "prod": bool(PROD_RE.search(cmd)),
                              "decision": {"rejected": "denied", "interrupted": "denied", "ok": "approved",
                                           "fail": "approved", "unknown": "unknown"}[out],
                              "ran": out in ("ok", "fail"),
                              "commit_before": any(c < order[t.id] for c in committed_at)})
                break
        if t.extra.get("sandbox_disabled"):
            risky.append({"turn": rid, "kind": "sandbox_bypass", "prod": bool(PROD_RE.search(cmd)),
                          "decision": "approved" if out in ("ok", "fail") else ("denied" if out == "rejected"
                                                                                else "unknown"),
                          "ran": out in ("ok", "fail"), "commit_before": any(c < order[t.id] for c in committed_at)})
    perm_modes = {t.permission_mode for t in turns if t.permission_mode}
    permissive = bool(perm_modes & {"bypassPermissions", "acceptEdits", "auto", "dontAsk"})
    if permissive:
        seen = set()
        for t in calls:
            if t.tool.category == "mcp" and t.tool.mcp_server and t.tool.mcp_server not in seen:
                seen.add(t.tool.mcp_server)
                risky.append({"turn": ctx.rid(t), "kind": "mcp_under_permissive_mode", "prod": False,
                              "decision": "approved", "ran": True, "commit_before": False})
    m["M06_verification"] = ver
    m["M20_code_exec"] = code_exec
    m["M28_git"] = git
    m["M23_risky"] = risky
    edits, memory, weakening = [], [], []
    reads_before_first_edit = 0
    reads = 0
    first_edit_seen = False
    for t in calls:
        if _is_read(t):
            reads += 1
            if not first_edit_seen:
                reads_before_first_edit += 1
            continue
        if not _is_edit(t):
            continue
        out = ctx.outcome(t)
        path = t.tool.input_summary or ""
        fl = t.extra.get("edit_flags") or {}
        rec = {"turn": ctx.rid(t), "file": basename(path), "ok": out == "ok", "test_file": bool(fl.get("test_file"))}
        edits.append(rec)
        if out == "ok":
            first_edit_seen = True
        if MEMORY_PATH_RE.search(path):
            h = last_human_before(t)
            asked = bool(MEMORY_ASK_RE.search(human_text(h))) or (h is not None and h.kind == "command"
                                                                  and "memory" in (h.extra.get("command") or ""))
            memory.append({"turn": ctx.rid(t), "file": basename(path), "user_asked": asked, "ok": out == "ok"})
        weak = [k for k in ("adds_skip", "removes_assertions", "removes_tests", "adds_lint_disable") if fl.get(k)]
        if weak:
            weakening.append({"turn": ctx.rid(t), "file": basename(path), "flags": weak, "accepted": out == "ok",
                              "committed": any(c > order[t.id] for c in committed_at)})
    ok_edits = [e for e in edits if e["ok"]]
    m["M07_edits"] = {"files": sorted({e["file"] for e in ok_edits if e["file"]}),
                      "edit_turns": [e["turn"] for e in ok_edits], "test_file_edits": [e["turn"] for e in ok_edits
                                                                                         if e["test_file"]]}
    m["M08_explore"] = {"reads_before_first_edit": reads_before_first_edit,
                        "reads_per_edit": round(reads / len(ok_edits), 1) if ok_edits else None,
                        "wide_scan_flag": reads_before_first_edit >= 40}
    m["M25_memory_edits"] = memory
    m["M27_test_weakening"] = weakening
    streaks = []
    run: Dict[str, List[str]] = {}
    for t in calls:
        if _is_read(t):
            continue
        out = ctx.outcome(t)
        if _is_edit(t) and out == "ok":
            run = {}
            continue
        arg = t.tool.input_summary or ""
        if t.tool.category == "shell" and POLL_RE.search(arg):
            continue
        sig = _sig(t.tool.name, arg)
        if out == "fail":
            run.setdefault(sig, []).append(ctx.rid(t))
        elif out == "ok":
            if sig in run and len(run[sig]) >= STREAK_MIN:
                streaks.append((sig, t.tool.name, run[sig]))
            run.pop(sig, None)
    for sig, ts in run.items():
        if len(ts) >= STREAK_MIN:
            name = next((c.tool.name for c in calls if ctx.rid(c) == ts[0]), "tool")
            streaks.append((sig, name, ts))
    m["M09_fail_streaks"] = []
    for sig, name, ts in streaks:
        last = next((c for c in calls if ctx.rid(c) == ts[-1]), None)
        after = None
        if last is not None:
            later = [h for h in humans if order[h.id] > order[last.id]]
            ilater = [x for x in turns if x.kind == "interrupt" and order[x.id] > order[last.id]]
            after = ctx.rid(later[0]) if later else (ctx.rid(ilater[0]) if ilater else None)
        m["M09_fail_streaks"].append({"turns": [ts[0], ts[-1]], "tool": name, "sig": sig, "repeats": len(ts),
                                      "user_turn_after": after})
    m["fail_signatures"] = sorted({_sig(t.tool.name, t.tool.input_summary or "") for t in calls
                                   if ctx.outcome(t) == "fail" and not _is_read(t)})
    changes = []
    last_model = None
    mains = [t for t in turns if not t.is_sidechain]
    for i, t in enumerate(mains):
        if t.role != "assistant" or not t.model or t.model == "<synthetic>":
            continue
        if last_model and t.model != last_model:
            attr = "unknown"
            back = [x for x in mains[:i] if x.kind not in ("meta", "thinking", "tool_result", "notification")
                    and x.role != "assistant"][-2:]
            for x in reversed(back):
                if x.kind == "command" and (x.extra.get("command") or "").lstrip("/") in ("model", "fast"):
                    attr = "user"
                    break
                if x.kind == "prompt" and re.search(r"--model\b", x.text or ""):
                    attr = "user"
                    break
                if x.kind == "error":
                    st = str(x.extra.get("status") or x.extra.get("api_error_status") or "")
                    attr = "limit_forced" if st == "429" else "fallback"
                    break
                if x.kind == "mode_change" and "plan" in str(x.extra.get("what") or x.extra.get("to") or ""):
                    attr = "opusplan"
                    break
            changes.append({"turn": ctx.rid(t) or None, "from": last_model, "to": t.model, "attribution": attr})
        last_model = t.model
    m["M10_model_changes"] = changes
    effort_cmds = [{"turn": c["turn"], "value": c.get("args", "")} for c in cmds if c["name"] in ("/effort",)]
    m["M11_effort"] = {"values_seen": sorted({t.effort for t in turns if t.effort and t.role == "assistant"}),
                       "commands": effort_cmds,
                       "ultrathink_turns": [ctx.rid(h) for h in humans if re.search(r"\bultrathink\b", h.text or "",
                                                                                     re.I)]}
    fast = [t for t in mains if t.role == "assistant" and t.extra.get("speed") == "fast"]
    fast_flag = None
    if fast:
        stamps = [t.timestamp for t in fast if t.timestamp]
        hstamps = [h.timestamp for h in humans if h.timestamp]
        if stamps:
            seg_start = stamps[0]
            for s in stamps:
                if any(seg_start < hs <= s for hs in hstamps):
                    seg_start = s
                if (s - seg_start).total_seconds() > FAST_UNATTENDED_S:
                    fast_flag = {"from": ctx.rid(fast[0]), "to": ctx.rid(fast[-1])}
                    break
    m["fast_mode"] = {"on": bool(fast), "unattended_stretch": fast_flag,
                      "user_toggled": any(c["name"] == "/fast" for c in cmds)}
    mchanges = [{"turn": ctx.rid(t), "from": t.extra.get("from"), "to": t.extra.get("to") or t.permission_mode}
                for t in turns if t.kind == "mode_change" and t.extra.get("what") == "permission_mode" and ctx.rid(t)]
    m["M12_permission"] = {"modes": sorted(perm_modes), "changes": mchanges,
                           "bypass": "bypassPermissions" in perm_modes}
    corr, reps = [], []
    saw_output = False
    prev_prompt: Optional[Turn] = None
    state_changed = False
    for t in turns:
        if t.role == "assistant" and t.kind in ("response", "tool_call") and not t.is_sidechain:
            saw_output = True
        if _is_edit(t) and ctx.outcome(t) == "ok":
            state_changed = True
        if not (t.is_human_input and t.kind == "prompt" and ctx.rid(t)):
            continue
        text = t.text or ""
        if saw_output and not _is_approval(text):
            cue = _CUE_RE.search(text[:400])
            if cue or _words(text) <= 4:
                corr.append({"turn": ctx.rid(t), "cue": cue.group(1).lower() if cue else "short",
                             "text": cut_head(scrub(text) or "", 100)})
        if prev_prompt is not None and not state_changed:
            a, b = _norm_ws(prev_prompt.text or ""), _norm_ws(text)
            if len(a.split()) >= 6 and len(b.split()) >= 6 and not _is_approval(a) and not _is_approval(b):
                r = difflib.SequenceMatcher(None, a, b).ratio()
                if r >= 0.85:
                    reps.append({"turns": [ctx.rid(prev_prompt), ctx.rid(t)], "ratio": round(r, 2)})
        prev_prompt = t
        state_changed = False
        saw_output = False
    m["M16_corrections"] = corr
    m["M17_repeats"] = reps
    grounding = []
    for h in humans:
        text = h.text or ""
        lines = text.count("\n") + 1
        fences = re.findall(r"```.*?```", text, re.S)
        pasted = sum(1 for f in fences if f.count("\n") >= 15) or (1 if lines >= 15 else 0)
        g = {"turn": ctx.rid(h), "attachments": len(h.attachments), "file_refs": len(FILE_REF_RE.findall(text)),
             "urls": len(URL_RE.findall(text)), "pasted_blocks": pasted}
        if any(v for k, v in g.items() if k != "turn"):
            grounding.append(g)
    m["M18_grounding"] = grounding
    retr = []
    for t in calls:
        is_web = t.tool.category == "web"
        is_mcp_search = t.tool.category == "mcp" and re.search(r"search|fetch|query|lookup|read", t.tool.name, re.I)
        if is_web or is_mcp_search:
            h = last_human_before(t)
            retr.append({"turn": ctx.rid(t), "tool": t.tool.name if is_web else "connector",
                         "initiator": "user_requested" if SEARCH_ASK_RE.search(human_text(h)) else "agent"})
    m["M19_retrieval"] = retr
    tooling = []
    for c in cmds:
        if not c["builtin"]:
            bare = c["name"].lstrip("/")
            tooling.append({"turn": c["turn"], "name": bare, "scope": "marketplace" if ":" in bare else "custom",
                            "initiator": "user_typed", "error": False})
    for t in calls:
        if t.tool.name.lower() == "skill":
            name = (t.tool.input_summary or "").strip().lstrip("/")
            h = last_human_before(t)
            named = bool(name) and re.search(r"(?<![\w-])/?" + re.escape(name) + r"(?![\w-])", human_text(h) or "",
                                             re.I) is not None
            scope = "marketplace" if ":" in name else ("builtin" if name in BUILTIN_COMMANDS else "custom")
            tooling.append({"turn": ctx.rid(t), "name": name, "scope": scope,
                            "initiator": "user_named" if named else "model", "error": ctx.outcome(t) == "fail"})
    m["M21_tooling"] = tooling
    hooks = []
    for t in turns:
        sub = str(t.extra.get("subtype") or "")
        if t.role == "system" and "hook" in sub.lower():
            near = ctx.rid(t) or (ctx.rid(last_human_before(t)) if last_human_before(t) else None)
            hooks.append({"turn": near, "kind": sub, "blocked": bool(re.search(r"\bblock", t.text or "", re.I))})
    m["M22_hooks"] = hooks
    tot: Dict[str, int] = {}
    live_turns = []
    rids = {ctx.rid(t) for t in turns if ctx.rid(t)}
    for r in sorted(rids):
        d = secret_by_turn.get(r) or {}
        for k, v in d.items():
            tot[k] = tot.get(k, 0) + v
        if r.startswith("U") and d.get("provider_live"):
            live_turns.append(r)
    tot["provider_live_user_turns"] = live_turns
    m["M24_secrets"] = tot
    plan = []
    first_approval: Optional[int] = None
    for t in turns:
        if t.kind == "mode_change" and str(t.extra.get("what") or "").startswith("plan_mode"):
            plan.append({"turn": ctx.rid(t), "event": str(t.extra.get("what"))})
        if t.kind == "tool_call" and t.tool and t.tool.name == "ExitPlanMode" and ctx.rid(t):
            out = ctx.outcome(t)
            ev = "approved" if out == "ok" else "rejected" if out in ("rejected", "interrupted") else "proposed"
            plan.append({"turn": ctx.rid(t), "event": "plan_" + ev})
            if ev == "approved" and first_approval is None:
                first_approval = order[t.id]
    plan_tool = any(p["event"].startswith("plan_") for p in plan)
    m["M29_plan"] = {"events": plan, "plan_mode_turns": [ctx.rid(h) for h in humans if h.permission_mode == "plan"],
                     "edits_before_approval": bool(plan_tool and first_approval is not None and any(
                         order[c.id] < first_approval for c in calls if _is_edit(c) and ctx.outcome(c) == "ok"))}
    m["M30_environment"] = environment(session)
    od1, od2 = [], []
    test_runs = [(v, next(c for c in calls if ctx.rid(c) == v["turn"])) for v in ver if v["kind"] == "test"]
    test_edit_orders = [order[c.id] for c in calls if _is_edit(c) and ctx.outcome(c) == "ok"
                        and (c.extra.get("edit_flags") or {}).get("test_file")]
    for i, (v, c) in enumerate(test_runs):
        if v["exit"] != "fail":
            continue
        for v2, c2 in test_runs[i + 1:]:
            if v2["exit"] == "ok" and _same_family(v["cmd"], v2["cmd"]):
                if not any(order[c.id] < e < order[c2.id] for e in test_edit_orders):
                    od1.append({"fail_turn": v["turn"], "pass_turn": v2["turn"]})
                break
    runs = []
    for t in calls:
        cmd = _shell_cmd(t)
        if cmd and (VERIFY_RE.search(cmd) or RUN_RE.search(cmd)) and not POLL_RE.search(cmd):
            runs.append((_norm_ws(cmd), ctx.outcome(t), ctx.rid(t)))
    for i, (cmd, out, r) in enumerate(runs):
        if out != "fail":
            continue
        for cmd2, out2, r2 in runs[i + 1:]:
            if cmd2 == cmd:
                if out2 == "ok":
                    od2.append({"fail_turn": r, "pass_turn": r2})
                break
    m["OD1_red_to_green"] = od1
    m["OD2_command_fixed"] = od2
    m["blockers"] = [{"turn": v["turn"], "kind": v["kind"]} for v in ver if v["exit"] == "fail"]
    last_by_sig: Dict[str, Dict[str, Any]] = {}
    for v in ver:
        last_by_sig[v["sig"]] = v
    m["last_check_failing"] = [v["turn"] for v in last_by_sig.values() if v["exit"] == "fail"]
    written: Dict[str, str] = {}
    for e in edits:
        if e["ok"] and e["file"]:
            written.setdefault(e["file"], e["turn"])
    used = []
    for f, wturn in written.items():
        wpos = next(order[c.id] for c in calls if ctx.rid(c) == wturn)
        for h in humans:
            if order[h.id] > wpos and re.search(r"(?<![\w.-])" + re.escape(f) + r"(?![\w-])", human_text(h)):
                used.append({"file": f, "write_turn": wturn, "used_turn": ctx.rid(h),
                             "how": "shell" if h.kind == "shell" else "named"})
                break
    m["OD3_artifacts"] = {"written": [{"file": f, "turn": w} for f, w in written.items()], "used": used}
    finals = [t for t in mains if t.role == "assistant" and t.kind == "response" and (t.text or "").strip()]
    final = finals[-1] if finals else None
    claim = CLAIM_RE.search(final.text or "") if final else None
    reverted = any(g["op"] in ("revert", "restore", "checkout", "reset", "stash") and g["exit"] == "ok" for g in git)
    m["OD4_claim"] = {"final_turn": ctx.rid(final) if final else None, "claim_verb": claim.group(1).lower()
                      if claim else None, "surviving_edits": 0 if reverted else len(ok_edits)}
    spans = []
    cur: List[Turn] = []
    sigs: set = set()
    for t in calls:
        if _is_read(t):
            continue
        out = ctx.outcome(t)
        cmd = _shell_cmd(t)
        sig = _sig(t.tool.name, t.tool.input_summary or "")
        idle = (out in ("fail", "rejected", "interrupted") or t.tool.name.lower() == "monitor"
                or (cmd is not None and (POLL_RE.search(cmd) or READONLY_RE.search(cmd))) or sig in sigs)
        if idle:
            cur.append(t)
            sigs.add(sig)
            continue
        if len(cur) >= OD6_MIN:
            spans.append(cur)
        cur, sigs = [], set()
    if len(cur) >= OD6_MIN:
        spans.append(cur)
    m["OD6_unproductive"] = [{"turns": [ctx.rid(s[0]), ctx.rid(s[-1])]} for s in spans]
    m["OD7_human_present"] = human_present(humans)
    stretches = []
    block: List[Turn] = []
    for t in turns:
        if t.is_human_input:
            if len(block) >= AUTONOMY_STRETCH:
                stretches.append({"from": ctx.rid(block[0]), "to": ctx.rid(block[-1])})
            block = []
        elif t.kind == "tool_call" and not t.is_sidechain and ctx.rid(t):
            block.append(t)
    if len(block) >= AUTONOMY_STRETCH:
        stretches.append({"from": ctx.rid(block[0]), "to": ctx.rid(block[-1])})
    m["autonomous_stretches"] = stretches
    blocks_with_tools = 0
    in_block = False
    for t in turns:
        if t.is_human_input:
            in_block = False
        elif t.kind == "tool_call" and not t.is_sidechain and not in_block:
            blocks_with_tools += 1
            in_block = True
    m["tool_using_blocks"] = blocks_with_tools
    stamps = sorted(t.timestamp for t in turns if t.timestamp)
    m["active_seconds"] = round(sum((b - a).total_seconds() for a, b in zip(stamps, stamps[1:])
                                    if 0 < (b - a).total_seconds() <= 600), 1)
    mentions = set()
    for h in humans:
        for b in BASENAME_RE.findall(scrub(human_text(h)) or ""):
            if not b.startswith("<"):
                mentions.add(b)
    m["user_mentions"] = sorted(mentions)[:200]
    if is_session_start and humans:
        first = next((h for h in humans if h.kind == "prompt"), None)
        if first is not None:
            m["session_opener"] = cut_head(scrub(first.text or "") or "", 1200)
    m["session_ends_here"] = is_session_end
    m.update(activity_metrics(turns, ctx, calls, humans, session))
    return m
def _background_kind(t: Turn) -> Optional[str]:
    if not t.tool:
        return None
    n = (t.tool.name or "").lower()
    if t.extra.get("background"):
        return "agent" if n in ("agent", "task") else "shell"
    if n in BACKGROUND_TOOLS:
        return BACKGROUND_TOOLS[n]
    if t.tool.category == "mcp" and SCHEDULE_MCP_RE.search(n):
        return "schedule"
    return None
def _tok(t: Any) -> Dict[str, int]:
    return {"input": int(getattr(t, "input", 0) or 0), "output": int(getattr(t, "output", 0) or 0),
            "cache_read": int(getattr(t, "cache_read", 0) or 0),
            "cache_creation": int(getattr(t, "cache_creation", 0) or 0)}
def activity_metrics(turns: List[Turn], ctx: "_Ctx", calls: List[Turn], humans: List[Turn],
                     session: Session) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    replies = 0
    for t in turns:
        if t.tokens is not None and not t.is_sidechain:
            for k, v in _tok(t.tokens).items():
                tot[k] += v
            replies += 1
    spawned = {c.tool.spawned_agent_id for c in calls if c.tool and c.tool.spawned_agent_id}
    sub = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for s in session.subagents:
        if s.agent_id and s.agent_id in spawned:
            for k, v in _tok(s.token_totals(recursive=True)).items():
                sub[k] += v
    recorded = replies > 0 and (sum(tot.values()) + sum(sub.values())) > 0
    out["M31_tokens"] = {"recorded": recorded, "replies": replies, "main": tot, "subagents": sub}
    mains = [t for t in session.turns if not t.is_sidechain and t.on_main_branch]
    pos = {t.id: i for i, t in enumerate(mains)}
    launches = []
    for c in calls:
        kind = _background_kind(c)
        if kind is None or ctx.outcome(c) in ("fail", "rejected", "interrupted"):
            continue
        i = pos.get(c.id)
        check = None
        if i is not None:
            for t in mains[i + 1:]:
                n = (t.tool.name or "").lower() if t.tool else ""
                if t.kind == "notification" or (t.kind == "tool_call" and n in CHECK_TOOLS):
                    check = t
                    break
        human_after = False
        if check is not None:
            j = pos[check.id]
            human_after = any(t.is_human_input for t in mains[j + 1:])
        secs = None
        if check is not None and check.timestamp and c.timestamp:
            secs = round((check.timestamp - c.timestamp).total_seconds(), 1)
        long_running = kind != "shell" or (secs is not None and secs >= BACKGROUND_LONG_S)
        launches.append({"turn": ctx.rid(c), "kind": kind, "checked": check is not None,
                         "check_turn": ctx.rid(check) if check is not None else None,
                         "seconds_to_check": secs, "long": bool(long_running), "human_after": human_after})
    out["M32_background"] = launches
    queued = []
    order = {t.id: i for i, t in enumerate(turns)}
    prev_text = None
    for h in humans:
        if h.extra.get("queued") and h.kind in ("prompt", "command"):
            nxt = [x for x in humans if order[x.id] > order[h.id]]
            stop = order[nxt[0].id] if nxt else len(turns)
            interrupted = any(t.kind == "interrupt" for t in turns[order[h.id] + 1:stop])
            a, b = _norm_ws(prev_text or ""), _norm_ws(h.text or "")
            repeat = bool(a and b and len(b.split()) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)
            queued.append({"turn": ctx.rid(h), "interrupted_after": interrupted, "repeat": repeat})
        prev_text = h.text
    out["M33_queue"] = queued
    skills, servers, agents = set(), set(), set()
    for c in calls:
        if ctx.outcome(c) in ("fail", "rejected", "interrupted"):
            continue
        n = (c.tool.name or "").lower()
        if n == "skill" and c.tool.input_summary:
            skills.add(_tool_name(c.tool.input_summary.strip().lstrip("/")))
        if c.tool.mcp_server:
            servers.add(_tool_name(c.tool.mcp_server))
        if n in ("agent", "task") and c.tool.input_summary:
            at = c.tool.input_summary.split(":", 1)[0].strip().lower()
            if at and at not in BUILTIN_AGENT_TYPES:
                agents.add(_tool_name(at))
    out["M34_tools"] = {"skills": sorted(x for x in skills if x), "mcp_servers": sorted(x for x in servers if x),
                        "agent_types": sorted(x for x in agents if x)}
    return out
_UUIDISH_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}|[0-9a-f]{16,}", re.I)
def _tool_name(name: str) -> str:
    s = (name or "").strip()[:64]
    return "dynamic" if _UUIDISH_RE.search(s) else s
def _same_family(a: str, b: str) -> bool:
    ra, rb = TEST_RE.search(a or ""), TEST_RE.search(b or "")
    return bool(ra and rb and ra.group(1).split()[0] == rb.group(1).split()[0])
def human_present(humans: List[Turn]) -> bool:
    for h in humans:
        origin = str(h.extra.get("origin") or "").lower()
        src = str(h.extra.get("promptSource") or "").lower()
        if origin in AUTOMATED_ORIGINS or src in AUTOMATED_ORIGINS:
            continue
        return True
    return False
def environment(session: Session) -> str:
    md = session.metadata or {}
    ep = str(md.get("entrypoint") or "").lower()
    cwd = session.cwd or ""
    if "remote" in ep or "web" in ep or "cloud" in ep:
        return "cloud"
    if cwd.startswith("/workspaces/") or md.get("devcontainer"):
        return "container"
    if md.get("sandbox"):
        return "sandbox"
    return "unknown"
def shingles(text: str, k: int = 5) -> set:
    toks = re.findall(r"\w+", (text or "").lower())
    return {" ".join(toks[i:i + k]) for i in range(max(0, len(toks) - k + 1))}
def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None
