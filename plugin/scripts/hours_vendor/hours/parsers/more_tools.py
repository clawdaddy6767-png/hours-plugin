from __future__ import annotations
import os
from typing import Any, Dict, List, NamedTuple, Optional
from ..model import Session
from . import aider, claude_cowork, cline, continue_dev, goose, qwen_code, roo_code, zed
TOOLS = ("aider", "cline", "roo_code", "continue_dev", "goose", "qwen_code", "zed", "claude_cowork")
LABELS = {"aider": "Aider", "cline": "Cline", "roo_code": "Roo Code", "continue_dev": "Continue", "goose": "Goose",
          "qwen_code": "Qwen Code", "zed": "Zed", "claude_cowork": "Claude Desktop (Cowork)"}
PLUGIN_NAMES = {"aider": "hours-aider", "cline": "hours-cline", "roo_code": "hours-roo-code",
                "continue_dev": "hours-continue", "goose": "hours-goose", "qwen_code": "hours-qwen-code",
                "zed": "hours-zed", "claude_cowork": "hours-claude-cowork"}
FOLDERS = {"aider": "aider", "cline": "cline", "roo_code": "roo-code", "continue_dev": "continue", "goose": "goose",
           "qwen_code": "qwen-code", "zed": "zed", "claude_cowork": "claude-cowork"}
class Found(NamedTuple):
    tool: str
    session_id: str
    path: str
    signature: str
    newest_mtime: float
    birth: float
    size: int
    raw_bytes: int
    ref: str
def _stat(path: str) -> Optional[os.stat_result]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if st.st_size > 0 else None
def _birth(st: os.stat_result) -> float:
    return getattr(st, "st_birthtime", st.st_mtime)
def _epoch(text: str, default: float) -> float:
    from ..model import parse_ts
    try:
        t = parse_ts(text)
    except Exception:
        t = None
    return t.timestamp() if t is not None else default
def _file_found(tool: str, sid: str, path: str, ref: str = "") -> Optional[Found]:
    st = _stat(path)
    if st is None:
        return None
    return Found(tool, sid, path, "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime, _birth(st), st.st_size,
                 st.st_size, ref)
def _aider() -> List[Found]:
    out: List[Found] = []
    for path in aider.history_files():
        st = _stat(path)
        if st is None:
            continue
        spans = aider.run_spans(path)
        for i, (start, begin, end) in enumerate(spans):
            if end - begin < 40:
                continue
            last = i == len(spans) - 1
            nxt = _epoch(spans[i + 1][0], st.st_mtime) if not last else st.st_mtime
            t0 = _epoch(start.replace(" ", "T"), st.st_mtime)
            out.append(Found("aider", aider.session_id_for(path, start), path, "%d:%d" % (begin, end),
                             st.st_mtime if last else max(t0, nxt), t0, end - begin, end - begin,
                             "%d:%d:%s" % (begin, end, start)))
    return out
def _db_found(tool: str, rows: List[Any], path: str) -> List[Found]:
    st = _stat(path)
    base = st.st_mtime if st else 0.0
    out: List[Found] = []
    for row in rows:
        sid, updated = row[0], row[1]
        count = row[2] if tool == "goose" else 0
        size = row[3] if tool == "goose" else row[2]
        t = _epoch(updated, base)
        out.append(Found(tool, sid, path, "%s:%d:%d" % (updated, count, size), t, t, size, size, ""))
    return out
def discover() -> List[Found]:
    out: List[Found] = []
    steps = (
        ("aider", _aider),
        ("cline", lambda: [f for f in (_file_found("cline", i, p) for i, p in cline.task_files(None, "cline")) if f]),
        ("roo_code", lambda: [f for f in (_file_found("roo_code", i, p) for i, p in roo_code.task_files()) if f]),
        ("continue_dev", lambda: [f for f in (_file_found("continue_dev", i, p)
                                              for i, p in continue_dev.session_files()) if f]),
        ("goose", lambda: _db_found("goose", goose.session_rows(), goose.db_path() or "")),
        ("qwen_code", lambda: [f for f in (_file_found("qwen_code", i, p) for i, p in qwen_code.session_files()) if f]),
        ("zed", lambda: _db_found("zed", zed.thread_rows(), zed.db_path() or "")),
        ("claude_cowork", lambda: [f for f in (_file_found("claude_cowork", i, p)
                                               for i, p in claude_cowork.session_files()) if f]),
    )
    for _name, fn in steps:
        try:
            out.extend(fn())
        except Exception:
            continue
    return out
def parse(tool: str, session_id: str, path: str, ref: str = "") -> Session:
    if tool == "aider":
        begin, end, start = ref.split(":", 2)
        return aider.parse_aider_run(path, start, int(begin), int(end))
    if tool == "cline":
        return cline.parse_task(path, source="cline")
    if tool == "roo_code":
        return roo_code.parse_roo_task(path)
    if tool == "continue_dev":
        return continue_dev.parse_continue_session(path)
    if tool == "goose":
        return goose.parse_goose_session(session_id, path)
    if tool == "qwen_code":
        return qwen_code.parse_qwen_session(path)
    if tool == "zed":
        return zed.parse_zed_thread(session_id, path)
    if tool == "claude_cowork":
        return claude_cowork.parse_cowork_session(path)
    raise ValueError("unknown tool: " + tool)
__all__ = ["TOOLS", "LABELS", "PLUGIN_NAMES", "FOLDERS", "Found", "discover", "parse"]
