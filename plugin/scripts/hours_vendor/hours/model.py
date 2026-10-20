from __future__ import annotations
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional
SOURCES = ("claude_code", "claude_ai", "chatgpt", "codex", "copilot", "other")
ROLES = ("user", "assistant", "tool_result", "system")
KINDS = (
    "prompt",
    "command",
    "shell",
    "response",
    "thinking",
    "tool_call",
    "tool_result",
    "interrupt",
    "compaction",
    "mode_change",
    "notification",
    "error",
    "meta",
)
@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0
    @property
    def total_input(self) -> int:
        return self.input + self.cache_read + self.cache_creation
    @property
    def total(self) -> int:
        return self.total_input + self.output
    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_creation + other.cache_creation,
            self.cache_creation_5m + other.cache_creation_5m,
            self.cache_creation_1h + other.cache_creation_1h,
        )
    def merge_max(self, other: "Tokens") -> None:
        self.input = max(self.input, other.input)
        self.output = max(self.output, other.output)
        self.cache_read = max(self.cache_read, other.cache_read)
        self.cache_creation = max(self.cache_creation, other.cache_creation)
        self.cache_creation_5m = max(self.cache_creation_5m, other.cache_creation_5m)
        self.cache_creation_1h = max(self.cache_creation_1h, other.cache_creation_1h)
@dataclass
class ToolInfo:
    name: str
    id: Optional[str] = None
    input_summary: Optional[str] = None
    is_error: bool = False
    is_rejected: bool = False
    is_interrupted: bool = False
    denial_kind: Optional[str] = None
    mcp_server: Optional[str] = None
    spawned_agent_id: Optional[str] = None
    category: Optional[str] = None
@dataclass
class Attachment:
    name: Optional[str] = None
    mime: Optional[str] = None
    size_bytes: Optional[int] = None
    extracted_chars: Optional[int] = None
    kind: str = "file"
@dataclass
class Turn:
    id: str
    role: str
    kind: str
    parent_id: Optional[str] = None
    text: Optional[str] = None
    char_count: int = 0
    timestamp: Optional[datetime] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    tokens: Optional[Tokens] = None
    tool: Optional[ToolInfo] = None
    skill: Optional[str] = None
    plugin: Optional[str] = None
    permission_mode: Optional[str] = None
    is_sidechain: bool = False
    agent_id: Optional[str] = None
    message_id: Optional[str] = None
    request_id: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)
    on_main_branch: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)
    @property
    def is_human_input(self) -> bool:
        return self.role == "user" and self.kind in ("prompt", "command", "shell") and not self.is_sidechain
@dataclass
class Session:
    id: str
    source: str
    title: Optional[str] = None
    project: Optional[str] = None
    cwd: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    turns: List[Turn] = field(default_factory=list)
    subagents: List["Session"] = field(default_factory=list)
    parent_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    account_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    def iter_turns(self, recursive: bool = False) -> Iterator[Turn]:
        for t in self.turns:
            yield t
        if recursive:
            for s in self.subagents:
                for t in s.iter_turns(recursive=True):
                    yield t
    def iter_sessions(self) -> Iterator["Session"]:
        yield self
        for s in self.subagents:
            for x in s.iter_sessions():
                yield x
    def human_inputs(self, main_branch_only: bool = True) -> List[Turn]:
        return [t for t in self.turns if t.is_human_input and (t.on_main_branch or not main_branch_only)]
    def prompts(self, main_branch_only: bool = True) -> List[Turn]:
        return [t for t in self.human_inputs(main_branch_only) if t.kind == "prompt"]
    def tool_calls(self, recursive: bool = False) -> List[Turn]:
        return [t for t in self.iter_turns(recursive) if t.kind == "tool_call"]
    def tool_results(self, recursive: bool = False) -> List[Turn]:
        return [t for t in self.iter_turns(recursive) if t.kind == "tool_result"]
    def timestamps(self, recursive: bool = False) -> List[datetime]:
        return sorted(t.timestamp for t in self.iter_turns(recursive) if t.timestamp is not None)
    def wall_time(self) -> Optional[timedelta]:
        if self.started_at and self.ended_at:
            return self.ended_at - self.started_at
        return None
    def active_time(self, idle_cutoff: timedelta = timedelta(minutes=5), recursive: bool = False) -> timedelta:
        ts = self.timestamps(recursive)
        total = timedelta(0)
        for a, b in zip(ts, ts[1:]):
            gap = b - a
            if gap <= idle_cutoff:
                total += gap
        return total
    def token_totals(self, recursive: bool = True) -> Tokens:
        tot = Tokens()
        for t in self.iter_turns(recursive):
            if t.tokens is not None:
                tot = tot + t.tokens
        return tot
    def tokens_by_model(self, recursive: bool = True) -> Dict[str, Tokens]:
        out: Dict[str, Tokens] = {}
        for t in self.iter_turns(recursive):
            if t.tokens is not None:
                key = t.model or "unknown"
                out[key] = out.get(key, Tokens()) + t.tokens
        return out
    def estimated_cost_usd(self, recursive: bool = True) -> float:
        total = 0.0
        for t in self.iter_turns(recursive):
            if t.tokens is not None:
                total += estimate_cost_usd(t.model, t.tokens, speed=t.extra.get("speed"),
                                           inference_geo=t.extra.get("inference_geo"))
        return total
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
_FRAC_RE = re.compile(r"\.(\d+)")
def parse_ts(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            if v > 1e12:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(value).strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        m = _FRAC_RE.search(s)
        if m:
            frac = (m.group(1) + "000000")[:6]
            s = s[: m.start()] + "." + frac + s[m.end():]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
PRICING: List[tuple] = [
    ("claude-fable-5-1", Price(10, 50, 0.25, 12.5, 20)),
    ("claude-mythos-5-1", Price(10, 50, 0.25, 12.5, 20)),
    ("claude-fable-5", Price(10, 50, 1.0, 12.5, 20)),
    ("claude-mythos-5", Price(10, 50, 1.0, 12.5, 20)),
    ("claude-opus-5-5", Price(4, 20, 0.20, 5, 8)),
    ("claude-opus-5", Price(5, 25, 0.50, 6.25, 10)),
    ("claude-opus-4-8", Price(5, 25, 0.50, 6.25, 10)),
    ("claude-opus-4-7", Price(5, 25, 0.50, 6.25, 10)),
    ("claude-opus-4-6", Price(5, 25, 0.50, 6.25, 10)),
    ("claude-opus-4-5", Price(5, 25, 0.50, 6.25, 10)),
    ("claude-opus-4-1", Price(15, 75, 1.50, 18.75, 30)),
    ("claude-opus-4", Price(15, 75, 1.50, 18.75, 30)),
    ("claude-sonnet-5-5", Price(2, 10, 0.10, 2.5, 4)),
    ("claude-sonnet-5", Price(2, 10, 0.20, 2.5, 4)),
    ("claude-sonnet-4", Price(3, 15, 0.30, 3.75, 6)),
    ("claude-3-7-sonnet", Price(3, 15, 0.30, 3.75, 6)),
    ("claude-haiku-5-5", Price(0.10, 0.50, 0.01, 0.125, 0.20)),
    ("claude-haiku-4", Price(1, 5, 0.10, 1.25, 2)),
    ("claude-3-5-haiku", Price(0.8, 4, 0.08, 1, 1.6)),
]
FAST_PRICING = {"claude-opus-5-5": (8.0, 40.0), "claude-opus-5": (10.0, 50.0), "claude-opus-4-8": (10.0, 50.0)}
def normalize_model(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    m = model.strip().lower()
    m = re.sub(r"\[.*?\]$", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    m = re.sub(r"^(anthropic\.|us\.anthropic\.|global\.anthropic\.)", "", m)
    m = re.sub(r"-v\d+(:\d+)?$", "", m)
    m = m.replace("@", "-")
    return m
def model_family(model: Optional[str]) -> Optional[str]:
    m = normalize_model(model) or ""
    for fam in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if fam in m:
            return fam
    if m.startswith("<synthetic"):
        return None
    return "other" if m else None
def price_for(model: Optional[str]) -> Optional[Price]:
    m = normalize_model(model)
    if not m:
        return None
    for prefix, p in PRICING:
        if m.startswith(prefix):
            return p
    return None
def estimate_cost_usd(model: Optional[str], tokens: Tokens, speed: Optional[str] = None,
                      inference_geo: Optional[str] = None) -> float:
    p = price_for(model)
    if p is None:
        return 0.0
    inp, out = p.input, p.output
    if speed == "fast":
        nm = normalize_model(model) or ""
        for k, (fi, fo) in FAST_PRICING.items():
            if nm.startswith(k):
                scale = fi / p.input
                inp, out = fi, fo
                p = Price(fi, fo, p.cache_read * scale, p.cache_write_5m * scale, p.cache_write_1h * scale)
                break
    w5, w1 = tokens.cache_creation_5m, tokens.cache_creation_1h
    unsplit = max(0, tokens.cache_creation - w5 - w1)
    w5 += unsplit
    cost = (tokens.input * inp + tokens.output * out + tokens.cache_read * p.cache_read
            + w5 * p.cache_write_5m + w1 * p.cache_write_1h) / 1_000_000
    if inference_geo == "us":
        cost *= 1.1
    return cost
_TOOL_CATEGORIES = {
    "shell": {"bash", "bash_tool", "monitor", "powershell", "shell", "local_shell", "exec_command"},
    "file_read": {"read", "view", "notebookread", "glob", "ls"},
    "file_edit": {"edit", "multiedit", "write", "notebookedit", "create_file", "str_replace", "apply_patch", "artifacts"},
    "search": {"grep", "toolsearch", "conversation_search", "recent_chats", "searchskills", "searchplugins"},
    "web": {"websearch", "webfetch", "web_search", "web_fetch", "web_search_fast"},
    "agent": {"agent", "task", "sendmessage", "listagents", "taskstop", "workflow", "taskoutput"},
    "skill": {"skill", "listskills"},
    "plan": {"todowrite", "exitplanmode", "enterplanmode", "askuserquestion", "ask_user_input_v0"},
}
def categorize_tool(name: Optional[str]) -> str:
    if not name:
        return "other"
    n = name.lower()
    if n.startswith("mcp__") or ":" in name:
        return "mcp"
    for cat, names in _TOOL_CATEGORIES.items():
        if n in names:
            return cat
    return "other"
_TEST_CMD_RE = re.compile(r"\b(pytest|unittest|npm (run )?test|yarn test|pnpm test|go test|cargo test|jest|vitest|mocha|rspec|phpunit|mvn test|gradle test|make test|tox)\b")
_GIT_CMD_RE = re.compile(r"(^|[;&|\s])git\s+(commit|push|merge|rebase|checkout|switch|pull)\b")
def compute_signals(session: Session, idle_cutoff: timedelta = timedelta(minutes=5)) -> Dict[str, Any]:
    main = session.turns
    everything = list(session.iter_turns(recursive=True))
    prompts = session.prompts()
    prompt_lens = [t.char_count for t in prompts]
    tool_calls = [t for t in everything if t.kind == "tool_call" and t.tool]
    tool_results = [t for t in everything if t.kind == "tool_result" and t.tool]
    tokens = session.token_totals(recursive=True)
    shell_cmds = [t.tool.input_summary or "" for t in tool_calls if t.tool.category == "shell"]
    branches = sum(1 for t in main if not t.on_main_branch)
    wall = session.wall_time()
    modes = [t.permission_mode for t in main if t.permission_mode]
    mode_changes = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
    return {
        "source": session.source,
        "human_prompts": len(prompts),
        "slash_commands": sum(1 for t in main if t.kind == "command" and not t.is_sidechain),
        "shell_escapes": sum(1 for t in main if t.kind == "shell"),
        "avg_prompt_chars": (sum(prompt_lens) / len(prompt_lens)) if prompt_lens else 0.0,
        "max_prompt_chars": max(prompt_lens) if prompt_lens else 0,
        "prompts_with_attachments": sum(1 for t in prompts if t.attachments),
        "assistant_responses": sum(1 for t in main if t.kind == "response"),
        "thinking_blocks": sum(1 for t in everything if t.kind == "thinking"),
        "tool_calls_total": len(tool_calls),
        "tool_calls_main": sum(1 for t in tool_calls if not t.is_sidechain),
        "tool_calls_by_name": dict(Counter(t.tool.name for t in tool_calls)),
        "tool_calls_by_category": dict(Counter(t.tool.category or "other" for t in tool_calls)),
        "tool_errors": sum(1 for t in tool_results if t.tool.is_error and not t.tool.is_rejected),
        "tool_rejections": sum(1 for t in tool_results if t.tool.is_rejected),
        "tool_interruptions": sum(1 for t in tool_results if t.tool.is_interrupted),
        "interrupts": sum(1 for t in main if t.kind == "interrupt"),
        "test_runs": sum(1 for c in shell_cmds if _TEST_CMD_RE.search(c)),
        "git_write_cmds": sum(1 for c in shell_cmds if _GIT_CMD_RE.search(c)),
        "mcp_calls": sum(1 for t in tool_calls if t.tool.category == "mcp"),
        "mcp_servers": sorted({t.tool.mcp_server for t in tool_calls if t.tool.mcp_server}),
        "subagents_direct": len(session.subagents),
        "subagents_total": sum(1 for _ in session.iter_sessions()) - 1,
        "skills_used": sorted({t.skill for t in everything if t.skill}
                              | {t.tool.input_summary for t in tool_calls if t.tool.name == "Skill" and t.tool.input_summary}),
        "plugins_used": sorted({t.plugin for t in everything if t.plugin}),
        "model_mix": dict(Counter(normalize_model(t.model) for t in everything if t.kind == "response" and t.model)),
        "effort_levels": dict(Counter(t.effort for t in everything if t.kind in ("response", "tool_call", "thinking") and t.effort)),
        "permission_modes": sorted(set(modes)),
        "permission_mode_changes": mode_changes,
        "mode_change_events": sum(1 for t in main if t.kind == "mode_change"),
        "compactions": sum(1 for t in main if t.kind == "compaction" and t.extra.get("boundary", True)),
        "api_errors": sum(1 for t in everything if t.kind == "error"),
        "branches_abandoned_turns": branches,
        "active_minutes": session.active_time(idle_cutoff).total_seconds() / 60.0,
        "wall_minutes": (wall.total_seconds() / 60.0) if wall is not None else None,
        "tokens": asdict(tokens),
        "estimated_cost_usd": round(session.estimated_cost_usd(recursive=True), 4),
        "background_launches": sum(1 for t in main if t.kind == "tool_call" and t.tool and (
            t.extra.get("background") or (t.tool.name or "").lower() in _BACKGROUND_NAMES)),
        "queued_prompts": sum(1 for t in main if t.is_human_input and t.extra.get("queued")),
        "active_spans": active_spans([t.timestamp for t in main if t.timestamp and not t.is_sidechain]),
    }
_BACKGROUND_NAMES = {"monitor", "schedulewakeup", "croncreate", "taskcreate", "remotetrigger"}
SPAN_IDLE = timedelta(minutes=5)
MAX_SPANS = 200
def active_spans(stamps: List[datetime], idle: timedelta = SPAN_IDLE) -> List[List[str]]:
    ts = sorted(stamps)
    out: List[List[datetime]] = []
    for t in ts:
        if out and t - out[-1][1] <= idle:
            out[-1][1] = t
        else:
            out.append([t, t])
    iso = [[a.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            b.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")] for a, b in out]
    return iso[-MAX_SPANS:]
