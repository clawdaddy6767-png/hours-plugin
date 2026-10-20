from __future__ import annotations
import datetime as _dt
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Session, Tokens, Turn
from ._common import bump, input_summary, make_tool, set_command, set_text, stable_uuid
SOURCE = "aider"
HISTORY_NAME = ".aider.chat.history.md"
HEADER = b"# aider chat started at "
NEVER_READ = (".aider.input.history", ".aider.llm.history")
SKIP_DIRS = frozenset({"node_modules", ".git", "Library", ".venv", "venv", "__pycache__", ".cache", "site-packages",
                       "target", "build", "dist", ".npm", ".cargo", ".rustup", ".Trash", "AppData", ".local", ".config"})
START_FOLDERS = ("Desktop", "Documents", "Developer", "dev", "code", "Code", "src", "projects", "Projects", "repos",
                 "work", "workspace", "workspaces", "github", "GitHub", "git", "source")
MAX_DEPTH = 4
MAX_DIRS = 20000
_TOKENS_RE = re.compile(
    r"Tokens:\s*([\d.,]+[kKmM]?) sent(?:,\s*([\d.,]+[kKmM]?) cache write)?(?:,\s*([\d.,]+[kKmM]?) cache hit)?"
    r",\s*([\d.,]+[kKmM]?) received\.?(?:\s*Cost:\s*\$([\d.]+) message,\s*\$([\d.]+) session)?")
_MODEL_RE = re.compile(r"Main model:\s*(\S+)")
_VERSION_RE = re.compile(r"Aider v(\d[\w.\-+]*)")
_APPLIED_RE = re.compile(r"^Applied edit to (.+)$")
_COMMIT_RE = re.compile(r"^Commit ([0-9a-f]{6,40})\b")
_RUNNING_RE = re.compile(r"^Running (.+)$")
_ERROR_RE = re.compile(r"(?i)\b(error|exception|traceback|failed)\b")
def _num(text: Optional[str]) -> int:
    s = (text or "").strip().replace(",", "")
    if not s:
        return 0
    mult = 1
    if s[-1] in "kK":
        mult, s = 1000, s[:-1]
    elif s[-1] in "mM":
        mult, s = 1000000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0
def _walk(root: str, depth: int, budget: List[int]) -> Iterator[str]:
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for e in entries:
        try:
            if e.is_file(follow_symlinks=False) and e.name == HISTORY_NAME:
                yield e.path
        except OSError:
            continue
    for e in entries:
        if e.name in SKIP_DIRS or (e.name.startswith(".") and e.name != ".aider"):
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                yield from _walk(e.path, depth + 1, budget)
        except OSError:
            continue
def history_files(home: Optional[str] = None) -> List[str]:
    home = home or os.path.expanduser("~")
    found: List[str] = []
    seen = set()
    def add(p: str) -> None:
        try:
            real = os.path.realpath(p)
        except OSError:
            return
        if real not in seen and os.path.isfile(real):
            seen.add(real)
            found.append(real)
    env = os.environ.get("AIDER_CHAT_HISTORY_FILE")
    if env:
        add(os.path.expanduser(env))
    add(os.path.join(home, HISTORY_NAME))
    budget = [MAX_DIRS]
    for name in START_FOLDERS:
        top = os.path.join(home, name)
        if os.path.isdir(top):
            for p in _walk(top, 0, budget):
                add(p)
    return sorted(found)
def run_spans(path: str) -> List[Tuple[str, int, int]]:
    out: List[Tuple[str, int, int]] = []
    try:
        with open(path, "rb") as fh:
            pos = 0
            for raw in fh:
                if raw.startswith(HEADER):
                    out.append([raw[len(HEADER):].decode("utf-8", "replace").strip(), pos, 0])
                pos += len(raw)
            size = pos
    except OSError:
        return []
    for i, r in enumerate(out):
        r[2] = out[i + 1][1] if i + 1 < len(out) else size
    return [(a, b, c) for a, b, c in out]
def session_id_for(path: str, start: str) -> str:
    return stable_uuid("aider", os.path.realpath(path), start)
def _start_time(text: str) -> Optional[_dt.datetime]:
    try:
        naive = _dt.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.astimezone(_dt.timezone.utc)
def parse_aider_run(path: str, start: Optional[str] = None, begin: int = 0, end: Optional[int] = None, *,
                    keep_text: bool = True) -> Session:
    warnings: List[str] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(begin)
            data = fh.read(None if end is None else max(0, end - begin))
    except OSError:
        data = b""
        warnings.append("history file unreadable")
    text = data.decode("utf-8", "replace")
    s = Session(id="", source=SOURCE)
    n = 0
    kinds: Dict[str, int] = {}
    model: Optional[str] = None
    assistant: List[str] = []
    user: List[str] = []
    tool: List[str] = []
    last_response: List[Optional[Turn]] = [None]
    started = _start_time(start or "")
    def add(role: str, kind: str) -> Turn:
        nonlocal n
        n += 1
        t = Turn(id="#%d" % n, role=role, kind=kind)
        if role == "assistant":
            t.model = model
        s.turns.append(t)
        return t
    def flush_assistant() -> None:
        body = "\n".join(assistant).strip()
        assistant.clear()
        if body:
            r = add("assistant", "response")
            set_text(r, body, keep_text=keep_text)
            last_response[0] = r
            bump(kinds, "response")
    def flush_user() -> None:
        body = "\n".join(user).strip()
        user.clear()
        if not body or body == "<blank>":
            return
        stripped = body.lstrip()
        kind = "command" if stripped.startswith("/") else ("shell" if stripped.startswith("!") else "prompt")
        t = add("user", kind)
        if kind == "command":
            set_command(t, stripped, keep_text=keep_text)
        else:
            set_text(t, stripped[1:].strip() if kind == "shell" else body, keep_text=keep_text)
        bump(kinds, kind)
    def flush_tool() -> None:
        nonlocal model
        lines = [x for x in tool if x.strip()]
        tool.clear()
        if not lines:
            return
        joined = "\n".join(lines)
        m = _VERSION_RE.search(joined)
        if m:
            s.metadata.setdefault("versions", [m.group(1)])
        m = _MODEL_RE.search(joined)
        if m:
            model = m.group(1)
            s.metadata["model"] = model
        m = _TOKENS_RE.search(joined)
        if m:
            r = last_response[0]
            if r is None:
                r = add("assistant", "response")
                last_response[0] = r
            r.tokens = Tokens(input=_num(m.group(1)), output=_num(m.group(4)), cache_creation=_num(m.group(2)),
                              cache_read=_num(m.group(3)))
            if m.group(5):
                try:
                    r.extra["cost_usd"] = float(m.group(5))
                except ValueError:
                    pass
            bump(kinds, "tokens")
        for line in lines:
            a = _APPLIED_RE.match(line)
            c = _COMMIT_RE.match(line)
            rn = _RUNNING_RE.match(line)
            if a:
                call = add("assistant", "tool_call")
                call.tool = make_tool("apply_edit", args={"path": a.group(1)})
                call.tool.category = "file_edit"
                call.char_count = len(a.group(1))
                bump(kinds, "edit")
            elif c:
                call = add("assistant", "tool_call")
                call.tool = make_tool("git_commit", args={"command": "git commit"})
                call.tool.category = "shell"
                bump(kinds, "commit")
            elif rn:
                call = add("assistant", "tool_call")
                call.tool = make_tool("run_command", args={"command": rn.group(1)})
                call.tool.category = "shell"
                call.char_count = len(rn.group(1))
                bump(kinds, "run")
        if "Tokens:" not in joined and _ERROR_RE.search(lines[0]) and not _APPLIED_RE.match(lines[0]):
            e = add("system", "error")
            e.char_count = len(joined)
    mode = ""
    for line in text.splitlines():
        if line.startswith("# aider chat started at "):
            if started is None:
                started = _start_time(line[len("# aider chat started at "):])
            continue
        if line.startswith("####"):
            if mode != "user":
                flush_assistant()
                flush_tool()
                flush_user()
            mode = "user"
            user.append(line[5:] if line.startswith("#### ") else "")
            if user and user[-1].endswith("  "):
                user[-1] = user[-1][:-2]
            continue
        if line.startswith(">"):
            if mode != "tool":
                flush_assistant()
                flush_user()
            mode = "tool"
            tool.append(line[2:] if line.startswith("> ") else "")
            continue
        if mode == "user":
            if not line.strip():
                flush_user()
                mode = ""
                continue
        if mode == "tool":
            flush_tool()
            mode = ""
        if line.strip() or assistant:
            mode = "assistant"
            assistant.append(line)
    flush_assistant()
    flush_tool()
    flush_user()
    sid = session_id_for(path, start or "")
    s.id = sid
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    s.started_at = started
    s.ended_at = started
    folder = os.path.dirname(os.path.realpath(path))
    s.cwd = folder
    s.project = os.path.basename(folder) or None
    s.metadata.update({"event_types": kinds, "history_file": HISTORY_NAME, "encoded_project_dir": None,
                       "timestamps": "run start only"})
    if folder:
        s.metadata["encoded_project_dir"] = re.sub(r"[^A-Za-z0-9]", "-", folder)
    s.warnings.extend(warnings)
    if not any(t.kind in ("prompt", "command", "shell") for t in s.turns):
        s.warnings.append("no typed input in this run")
    return s
__all__ = ["parse_aider_run", "history_files", "run_spans", "session_id_for", "SOURCE", "HISTORY_NAME", "NEVER_READ"]
