from __future__ import annotations
import json
import os
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple
from ..model import Session, parse_ts
from ._common import as_uuid, encode_project_dir
from . import claude_code
SOURCE = "claude_cowork"
NEVER_READ = ("outputs", "uploads", "cowork_settings.json", ".claude.json", "scheduled-tasks.json")
def cowork_root(home: Optional[str] = None) -> str:
    home = home or os.path.expanduser("~")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"), "Claude",
                            "local-agent-mode-sessions")
    return os.path.join(home, "Library", "Application Support", "Claude", "local-agent-mode-sessions")
def session_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    root = cowork_root(home)
    out: List[Tuple[str, str]] = []
    try:
        accounts = sorted(os.listdir(root))
    except OSError:
        return out
    for a in accounts:
        try:
            orgs = sorted(os.listdir(os.path.join(root, a)))
        except OSError:
            continue
        for o in orgs:
            odir = os.path.join(root, a, o)
            try:
                names = sorted(os.listdir(odir))
            except OSError:
                continue
            for n in names:
                p = os.path.join(odir, n, "audit.jsonl")
                if n.startswith("local_") and os.path.isfile(p):
                    out.append((n, p))
    return out
def _meta(audit_path: str) -> Dict[str, Any]:
    folder = os.path.dirname(audit_path)
    try:
        with open(folder + ".json", "rb") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: doc.get(k) for k in ("cwd", "title", "model", "createdAt")}
def parse_cowork_session(path: str, *, keep_text: bool = True,
                         tool_result_chars: Optional[int] = claude_code.DEFAULT_TOOL_RESULT_CHARS) -> Session:
    folder = os.path.basename(os.path.dirname(path))
    fp = claude_code._FileParser(path, is_sidechain_file=False, seen_messages=set(),
                                 tool_result_chars=tool_result_chars, keep_text=keep_text)
    for rec in claude_code.iter_records(path, fp.session.warnings):
        if "timestamp" not in rec and rec.get("_audit_timestamp"):
            rec["timestamp"] = rec["_audit_timestamp"]
        try:
            fp._handle(rec)
        except Exception as exc:
            fp.counts["record_errors"] += 1
            if fp.counts["record_errors"] <= 5:
                fp.session.warnings.append("record error (%s): %s" % (rec.get("type"), type(exc).__name__))
    s = fp._finish()
    sid = as_uuid(folder[len("local_"):] if folder.startswith("local_") else folder, "claude_cowork", folder)
    s.id = sid
    s.source = SOURCE
    for i, t in enumerate(s.turns):
        t.id = "%s#%d" % (sid, i + 1)
    meta = _meta(path)
    if not s.cwd and isinstance(meta.get("cwd"), str):
        s.cwd = meta["cwd"]
        s.project = os.path.basename(s.cwd.rstrip("/\\")) or None
    if not s.title and isinstance(meta.get("title"), str):
        s.title = meta["title"]
    if s.started_at is None and meta.get("createdAt"):
        s.started_at = parse_ts(meta["createdAt"])
    s.metadata.update({"path_kind": "cowork_audit", "folder_id": folder, "model": meta.get("model"),
                       "encoded_project_dir": encode_project_dir(s.cwd) or "cowork"})
    return s
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    for _n, p in session_files(home):
        yield parse_cowork_session(p, **kwargs)
__all__ = ["parse_cowork_session", "session_files", "cowork_root", "iter_sessions", "SOURCE", "NEVER_READ"]
