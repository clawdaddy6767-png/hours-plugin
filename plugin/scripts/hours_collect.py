from __future__ import annotations
import argparse
import dataclasses
import datetime as _dt
import errno
import fcntl
import gzip
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import socket
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
VENDOR = os.path.join(HERE, "hours_vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
from hours.grader import health as _health
from hours.grader import render as _render
from hours.grader.privacy import Scrubber
from hours.model import compute_signals
from hours.parsers import antigravity as _agy
from hours.parsers import codex as _codex
from hours.parsers import copilot_cli as _copilot
from hours.parsers import cursor as _cursor
from hours.parsers import hermes as _hermes
from hours.parsers import nanoclaw as _nanoclaw
from hours.parsers import openclaw as _openclaw
from hours.parsers import opencode as _opencode
from hours.parsers import pi as _pi
from hours.parsers import gemini_cli as _gemini
from hours.parsers import more_tools as _more
from hours.parsers import sources as _sources
from hours.parsers._common import ZstdUnavailable, zstd_decoder
from hours.parsers.claude_code import parse_session_file
from hours.fingerprint import views as _views
from hours.fingerprint.core import parse_ts as _auth_ts
from hours.fingerprint.skeleton import record_hash as _rec_hash, record_key as _rec_key
BUNDLE_FORMAT = "hours.bundle/1"
SCHEMA_VERSION = 1
SIGNALS_VERSION = 2
IDLE_MINUTES = 10
STOP_THROTTLE_SECONDS = 600
STOP_SWEEP_TOOLS = ("antigravity",)
WITNESS_FILE = "witness.json"
WITNESS_CHUNK = 5000
WITNESS_READ_CAP = 64 << 20
WITNESS_KEEP = 500
DEMO_LINE_TYPE = "x-hours-demo"
WITNESS_TOOLS = ("claude_code", "codex", "gemini_cli", "antigravity", "copilot_cli")
TOOLS = WITNESS_TOOLS + ("cursor", "opencode", "pi", "hermes", "openclaw", "nanoclaw")
TOOL_LABELS = {"claude_code": "Claude Code", "codex": "Codex", "gemini_cli": "Gemini CLI",
               "antigravity": "Antigravity", "copilot_cli": "Copilot CLI", "cursor": "Cursor", "opencode": "OpenCode",
               "pi": "Pi", "hermes": "Hermes", "openclaw": "OpenClaw", "nanoclaw": "NanoClaw"}
PLUGIN_NAMES = {"claude_code": "hours", "codex": "hours-codex", "gemini_cli": "hours-gemini-cli",
                "antigravity": "hours-antigravity", "copilot_cli": "hours-copilot-cli", "cursor": "hours-cursor"}
TOOLS = TOOLS + _more.TOOLS
TOOL_LABELS.update(_more.LABELS)
PLUGIN_NAMES.update(_more.PLUGIN_NAMES)
MANIFESTS = (os.path.join(".claude-plugin", "plugin.json"), os.path.join(".codex-plugin", "plugin.json"),
             os.path.join(".cursor-plugin", "plugin.json"), "gemini-extension.json", "plugin.json")
LOCK_WAIT_SECONDS = 900
LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
FIRST_REQUEST_TIMEOUT = 90.0
REQUEST_TIMEOUT = 60.0
PROGRESS_TIMEOUT = 10.0
RETRIES = 3
PROGRESS_MIN_INTERVAL = 1.0
MAX_REPLY_BYTES = 1_000_000
CODE_RE = re.compile(r"^HRS-[A-Z2-9]{4}-[A-Z2-9]{4}$")
CLOCK_START_RE = re.compile(r"^[0-9]{9,11}$")
CLOCK_START_SKEW = 600
CODES_TRIED_FILE = "codes_tried.json"
CODES_TRIED_KEEP = 50
AUTO_PAIR_LOCK_WAIT = 120
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HTTPS_HOST_RE = re.compile(r"^(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
                            r"(?::(?P<port>\d{1,5}))?$")
_HTTP_LOCAL_RE = re.compile(r"^(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?$")
log = logging.getLogger("hours.collector")
RUNNING_TOOL = ["claude_code"]
def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, ""))
        return v if v >= 0 else default
    except ValueError:
        return default
def retry_base_seconds() -> float:
    return _env_float("HOURS_RETRY_BASE_SECONDS", 2.0)
def wake_seconds() -> float:
    return _env_float("HOURS_WAKE_SECONDS", 90.0)
def plugin_version() -> str:
    for rel in MANIFESTS:
        try:
            with open(os.path.join(PLUGIN_ROOT, rel), "r") as fh:
                return str(json.load(fh).get("version") or "0.0.0")
        except Exception:
            continue
    return "0.0.0"
def vendor_info() -> Dict[str, Any]:
    try:
        with open(os.path.join(VENDOR, "VENDOR_MANIFEST.json"), "r") as fh:
            m = json.load(fh)
        return {"vendor_hash": m.get("vendor_hash"), "built_at_utc": m.get("built_at_utc")}
    except Exception:
        return {"vendor_hash": None, "built_at_utc": None}
def home_pointer_path() -> str:
    return os.path.expanduser(os.path.join("~", ".hours", "home.json"))
def read_home_pointer() -> Optional[str]:
    try:
        with open(home_pointer_path(), "r") as fh:
            d = str(json.load(fh).get("data_dir") or "")
    except Exception:
        return None
    if d and os.path.isabs(d) and os.path.isfile(os.path.join(d, "device.json")):
        return d
    return None
def write_home_pointer(ddir: str) -> None:
    real = os.path.realpath(ddir)
    base = os.path.dirname(home_pointer_path())
    if real == os.path.realpath(base):
        return
    os.makedirs(base, mode=0o700, exist_ok=True)
    write_atomic(home_pointer_path(), (json.dumps({"data_dir": real, "written_at_utc": _iso(
        _dt.datetime.now(_dt.timezone.utc))}, indent=1) + "\n").encode())
def data_dir(override: Optional[str] = None, tool: str = "claude_code") -> str:
    if override and "${" in override:
        override = None
    d = override or os.environ.get("HOURS_DATA_DIR")
    if not d and tool == "claude_code":
        d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d and tool != "claude_code":
        d = read_home_pointer()
    d = d or os.path.expanduser("~/.hours")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d
def endpoint() -> str:
    return (os.environ.get("HOURS_ENDPOINT") or os.environ.get("CLAUDE_PLUGIN_OPTION_ENDPOINT") or "").strip()
def dev_inbox(override: Optional[str] = None) -> str:
    return override or os.environ.get("HOURS_DEV_INBOX") or os.path.expanduser("~/.hours/dev_inbox")
def setup_logging(ddir: str, also_stderr: bool = False) -> None:
    logdir = os.path.join(ddir, "logs")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(logdir, "collector.log"), maxBytes=LOG_BYTES,
                                             backupCount=LOG_BACKUPS)
    h.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    log.handlers[:] = [h]
    if also_stderr:
        s = logging.StreamHandler(sys.stderr)
        s.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(s)
    log.setLevel(logging.INFO)
    log.propagate = False
def log_exc(where: str) -> None:
    et, _ev, tb = sys.exc_info()
    frames = "".join(traceback.format_tb(tb)[-3:]).replace("\n", " | ")
    log.error("%s failed: %s at %s", where, et.__name__ if et else "?", frames)
def origin_of(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url or len(url) > 2048 or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        return None
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme, netloc = p.scheme.lower(), p.netloc.lower()
    if not netloc or "@" in netloc:
        return None
    if scheme == "https":
        m = _HTTPS_HOST_RE.match(netloc)
        if not m or (m.group("port") and not 0 < int(m.group("port")) < 65536):
            return None
    elif scheme == "http":
        if not _HTTP_LOCAL_RE.match(netloc):
            return None
    else:
        return None
    return "%s://%s" % (scheme, netloc)
def normalize_site(url: str) -> Optional[str]:
    o = origin_of(url)
    if not o:
        return None
    p = urllib.parse.urlsplit(url)
    if p.path not in ("", "/") or p.query or p.fragment or url.endswith("?") or url.endswith("#"):
        return None
    return o
def _endpoint_allowed(url: str) -> bool:
    return origin_of(url) is not None
def device_path(ddir: str) -> str:
    return os.path.join(ddir, "device.json")
def load_device(ddir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(device_path(ddir), "r") as fh:
            rec = json.load(fh)
    except Exception:
        return None
    if not isinstance(rec, dict) or not origin_of(str(rec.get("origin") or "")) \
            or not TOKEN_RE.match(str(rec.get("device_token") or "")):
        return None
    return rec
def save_device(ddir: str, rec: Dict[str, Any]) -> None:
    write_atomic(device_path(ddir), (json.dumps(rec, indent=1) + "\n").encode())
    os.chmod(device_path(ddir), 0o600)
def has_server_opt_in(ddir: str, origin: str) -> bool:
    try:
        with open(os.path.join(ddir, "opt_in.json"), "r") as fh:
            rec = json.load(fh)
    except Exception:
        return False
    return isinstance(rec, dict) and rec.get("source") == "server" and rec.get("origin") == origin \
        and bool(rec.get("version")) and bool(rec.get("accepted_at"))
def has_dev_opt_in(ddir: str) -> bool:
    return os.path.isfile(os.path.join(ddir, "dev_opt_in.json"))
def _pt_offset_hours(utc: _dt.datetime) -> int:
    y = utc.year
    mar_first_sun = 1 + (6 - _dt.date(y, 3, 1).weekday()) % 7
    nov_first_sun = 1 + (6 - _dt.date(y, 11, 1).weekday()) % 7
    start = _dt.datetime(y, 3, mar_first_sun + 7, 10, tzinfo=_dt.timezone.utc)
    end = _dt.datetime(y, 11, nov_first_sun, 9, tzinfo=_dt.timezone.utc)
    return -7 if start <= utc < end else -8
def to_pt(ts: float) -> _dt.datetime:
    utc = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return utc.astimezone(_dt.timezone(_dt.timedelta(hours=_pt_offset_hours(utc))))
def pt_date(ts: float) -> str:
    d = to_pt(ts)
    return "%s %d, %d" % (d.strftime("%b"), d.day, d.year)
def human_bytes(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return "%.1f GB" % (n / 1e9)
    if n >= 1_000_000:
        return "%.1f MB" % (n / 1e6)
    if n >= 1_000:
        return "%.0f KB" % (n / 1e3)
    return "%d bytes" % n
def os_label() -> str:
    s = platform.system()
    if s == "Darwin":
        v = platform.mac_ver()[0]
        return ("macOS " + v).strip()
    return ("%s %s" % (s, platform.release())).strip()[:60]
def transcript_roots() -> List[Tuple[str, str]]:
    roots: List[Tuple[str, str]] = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.append((os.path.join(os.path.expanduser(cfg), "projects"), "claude_code"))
    roots.append((os.path.expanduser("~/.claude/projects"), "claude_code"))
    cowork = os.path.expanduser("~/Library/Application Support/Claude/local-agent-mode-sessions")
    if os.path.isdir(cowork):
        base_depth = cowork.rstrip("/").count("/")
        for dirpath, dirnames, _files in os.walk(cowork):
            if dirpath.count("/") - base_depth >= 6:
                dirnames[:] = []
                continue
            if os.path.basename(dirpath) == ".claude" and "projects" in dirnames:
                roots.append((os.path.join(dirpath, "projects"), "cowork_local"))
                dirnames[:] = []
    out, seen = [], set()
    for r, origin in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        if os.path.isdir(real) and real not in seen:
            seen.add(real)
            out.append((real, origin))
    return out
@dataclasses.dataclass
class Candidate:
    session_id: str
    path: str
    origin: str
    signature: str
    newest_mtime: float
    birth: float
    size: int
    raw_bytes: int = 0
    tool: str = "claude_code"
    key: str = ""
    extra_paths: List[str] = dataclasses.field(default_factory=list)
    compressed: bool = False
    ref: str = ""
    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.session_id
    @property
    def label(self) -> str:
        return self.key[:8] if self.tool == "claude_code" else self.key[:len(self.tool) + 9]
def _session_dir_stats(session_dir: str) -> Tuple[int, int, float]:
    n = total = 0
    newest = 0.0
    if not os.path.isdir(session_dir):
        return 0, 0, 0.0
    for dirpath, _d, files in os.walk(session_dir):
        for f in files:
            if not f.endswith((".jsonl", ".json")):
                continue
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return n, total, newest
def discover(since_days: Optional[float] = None) -> Dict[str, Candidate]:
    best: Dict[str, Candidate] = {}
    cutoff = time.time() - since_days * 86400 if since_days else None
    for root, origin in transcript_roots():
        try:
            projects = sorted(os.listdir(root))
        except OSError:
            continue
        for p in projects:
            pdir = os.path.join(root, p)
            if not os.path.isdir(pdir):
                continue
            try:
                names = sorted(os.listdir(pdir))
            except OSError:
                continue
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                path = os.path.join(pdir, n)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path) or st.st_size == 0:
                    continue
                cnt, sub_bytes, sub_newest = _session_dir_stats(path[:-6])
                newest = max(st.st_mtime, sub_newest)
                if cutoff and newest < cutoff:
                    continue
                sid = n[:-6]
                sig = "%d:%d:%d:%d" % (st.st_size, int(st.st_mtime), cnt, sub_bytes)
                birth = getattr(st, "st_birthtime", st.st_mtime)
                c = Candidate(sid, path, origin, sig, newest, birth, st.st_size, st.st_size + sub_bytes)
                if sid not in best or st.st_size > best[sid].size:
                    best[sid] = c
    return best
def _stat(path: str) -> Optional[os.stat_result]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if st.st_size > 0 else None
def _birth(st: os.stat_result) -> float:
    return getattr(st, "st_birthtime", st.st_mtime)
def discover_other_tools(since_days: Optional[float] = None,
                         skipped: Optional[Dict[str, int]] = None) -> Dict[str, Candidate]:
    skipped = skipped if skipped is not None else {}
    cutoff = time.time() - since_days * 86400 if since_days else None
    out: Dict[str, Candidate] = {}
    decoder = zstd_decoder()
    def keep(c: Candidate) -> None:
        if cutoff and c.newest_mtime < cutoff:
            return
        if c.key not in out or c.size > out[c.key].size:
            out[c.key] = c
    def codex() -> None:
        for tid, path in _codex.rollout_files():
            st = _stat(path)
            if st is None:
                continue
            comp = path.endswith(".zst")
            if comp and decoder is None:
                skipped["codex_compressed"] = skipped.get("codex_compressed", 0) + 1
                continue
            sig = "codex-zst" if comp else "%d:%d" % (st.st_size, int(st.st_mtime))
            keep(Candidate(tid, path, "codex", sig, st.st_mtime, _birth(st), st.st_size, st.st_size, tool="codex",
                           key="codex:" + tid, compressed=comp))
    def gemini() -> None:
        for key, path, subs in _gemini.session_files():
            st = _stat(path)
            if st is None:
                continue
            sub_st = [s for s in (_stat(x) for x in subs) if s is not None]
            sub_bytes = sum(s.st_size for s in sub_st)
            newest = max([st.st_mtime] + [s.st_mtime for s in sub_st])
            sig = "%d:%d:%d:%d" % (st.st_size, int(st.st_mtime), len(sub_st), sub_bytes)
            keep(Candidate(key.rsplit("-", 1)[-1], path, "gemini_cli", sig, newest, _birth(st), st.st_size,
                           st.st_size + sub_bytes, tool="gemini_cli", key=key, extra_paths=list(subs)))
    def antigravity() -> None:
        for cid, logs, app in _agy.conversation_dirs():
            sts = [(p, _stat(p)) for p in _agy.transcript_paths(logs)]
            sts = [(p, s) for p, s in sts if s is not None]
            if not sts:
                continue
            sig = ":".join("%d:%d" % (s.st_size, int(s.st_mtime)) for _p, s in sts)
            size = max(s.st_size for _p, s in sts)
            keep(Candidate(cid, logs, app, sig, max(s.st_mtime for _p, s in sts), min(_birth(s) for _p, s in sts),
                           size, sum(s.st_size for _p, s in sts), tool="antigravity", key="antigravity:" + cid))
    def copilot() -> None:
        for sid, path in _copilot.session_files():
            st = _stat(path)
            if st is None:
                continue
            keep(Candidate(sid, path, "copilot_cli", "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime,
                           _birth(st), st.st_size, st.st_size, tool="copilot_cli", key="copilot_cli:" + sid))
    def db_refs(tool: str, refs: List[Dict[str, Any]]) -> None:
        for r in refs:
            sid = str(r["id"])
            upd = float(r.get("updated") or 0.0)
            n = max(1, int(r.get("messages") or 0))
            size = n * 1024
            keep(Candidate(sid, r["path"], tool, "%d:%d" % (int(upd * 1000), n), upd, float(r.get("created") or upd),
                           size, size, tool=tool, key="%s:%s" % (tool, sid.lower()), ref=sid))
    def cursor() -> None:
        refs = _cursor.composer_refs()
        db_refs("cursor", refs)
        seen = {str(r["id"]).lower() for r in refs}
        for sid, path in _cursor.transcript_files():
            st = _stat(path)
            if st is None or sid.lower() in seen:
                continue
            keep(Candidate(sid, path, "cursor", "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime, _birth(st),
                           st.st_size, st.st_size, tool="cursor", key="cursor:" + sid.lower()))
    def pi_files() -> None:
        for sid, path in _pi.session_files():
            st = _stat(path)
            if st is None:
                continue
            keep(Candidate(sid, path, "pi", "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime, _birth(st),
                           st.st_size, st.st_size, tool="pi", key="pi:" + sid.lower()))
    for name, fn in (("codex", codex), ("gemini_cli", gemini), ("antigravity", antigravity), ("copilot_cli", copilot),
                     ("cursor", cursor), ("opencode", lambda: db_refs("opencode", _opencode.session_refs())),
                     ("pi", pi_files), ("hermes", lambda: db_refs("hermes", _hermes.session_refs())),
                     ("openclaw", lambda: db_refs("openclaw", _openclaw.session_refs())),
                     ("nanoclaw", lambda: db_refs("nanoclaw", _nanoclaw.session_refs()))):
        try:
            fn()
        except Exception:
            log_exc("discover " + name)
    try:
        for f in _more.discover():
            keep(Candidate(f.session_id, f.path, f.tool, f.signature, f.newest_mtime, f.birth, f.size, f.raw_bytes,
                           tool=f.tool, key="%s:%s" % (f.tool, f.session_id.lower()), ref=f.ref))
    except Exception:
        log_exc("discover more tools")
    return out
def discover_all(since_days: Optional[float] = None,
                 skipped: Optional[Dict[str, int]] = None) -> Dict[str, Candidate]:
    cands = discover(since_days)
    for k, c in discover_other_tools(since_days, skipped).items():
        cands.setdefault(k, c)
    return cands
class State:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, signature TEXT, "
                        "revision INTEGER, last_bundle_id TEXT, updated_utc TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen_keys (key TEXT PRIMARY KEY, session_id TEXT)")
        self.db.commit()
    def get(self, sid: str) -> Optional[Tuple[str, int, str]]:
        row = self.db.execute("SELECT signature, revision, last_bundle_id FROM sessions WHERE session_id=?",
                              (sid,)).fetchone()
        return (row[0], int(row[1]), row[2]) if row else None
    def owners(self, keys: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = "SELECT key, session_id FROM seen_keys WHERE key IN (%s)" % ",".join("?" * len(chunk))
            for k, s in self.db.execute(q, chunk):
                out[k] = s
        return out
    def commit_session(self, sid: str, signature: str, revision: int, bundle_id: str, keys: Iterable[str]) -> None:
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                            (sid, signature, revision, bundle_id, now))
            self.db.executemany("INSERT OR IGNORE INTO seen_keys VALUES (?,?)", ((k, sid) for k in keys))
    def mark_seen(self, key: str, signature: str) -> None:
        prev = self.get(key)
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                            (key, signature, prev[1] if prev else 0, prev[2] if prev else None, now))
    def close(self) -> None:
        self.db.close()
from hours.grader.privacy import PLACEHOLDER_RE
class FinalScrubber(Scrubber):
    def _token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self.map:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.map[key] = "<%s_F%d>" % (kind, self.counts[kind])
        return self.map[key]
    def scrub_safe(self, text: str) -> str:
        if not text:
            return text
        parts, last = [], 0
        for m in PLACEHOLDER_RE.finditer(text):
            parts.append(self.scrub(text[last:m.start()]) or "")
            parts.append(m.group(0))
            last = m.end()
        parts.append(self.scrub(text[last:]) or "")
        return "".join(parts)
NO_SCRUB_KEYS = {"bundle_format", "bundle_id", "supersedes", "session_id", "unit_id", "id", "label", "level",
                 "surface", "origin", "source", "created_at_utc", "started_at", "ended_at", "project_key",
                 "trigger", "owner_sessions", "vendor_hash", "built_at_utc", "render_version", "version", "python", "os",
                 "compression", "entrypoint", "claude_code_versions", "tool_versions", "permission_modes"}
def scrub_tree(obj: Any, scrubber: FinalScrubber, key: Optional[str] = None) -> Any:
    if isinstance(obj, str):
        return obj if key in NO_SCRUB_KEYS else scrubber.scrub_safe(obj)
    if isinstance(obj, list):
        return [scrub_tree(x, scrubber, key) for x in obj]
    if isinstance(obj, dict):
        return {(scrubber.scrub_safe(k) if isinstance(k, str) else k): scrub_tree(v, scrubber, k)
                for k, v in obj.items()}
    return obj
def detect_tree(obj: Any, key: Optional[str] = None, det: Optional[FinalScrubber] = None) -> Dict[str, int]:
    det = det or FinalScrubber()
    if isinstance(obj, str):
        if key not in NO_SCRUB_KEYS:
            det.scrub_safe(obj)
    elif isinstance(obj, list):
        for x in obj:
            detect_tree(x, key, det)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                det.scrub_safe(k)
            detect_tree(v, k, det)
    return dict(det.counts)
def _iso(d: Optional[_dt.datetime]) -> Optional[str]:
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None
def _turn_keys(t: Any) -> List[str]:
    keys = []
    base = (t.id or "").split("#")[0]
    if base:
        keys.append("u:" + base)
    if t.role == "assistant" and t.message_id:
        keys.append("m:" + t.message_id)
    return keys
def _jsonable(o: Any) -> Any:
    if isinstance(o, (set, frozenset, tuple)):
        return sorted(o) if isinstance(o, (set, frozenset)) else list(o)
    if isinstance(o, _dt.datetime):
        return _iso(o)
    return str(o)
def parse_candidate(c: Candidate):
    if c.tool == "codex":
        return _codex.parse_rollout(c.path)
    if c.tool == "gemini_cli":
        return _gemini.parse_gemini_cli_file(c.path, subagent_paths=c.extra_paths)
    if c.tool == "antigravity":
        return _agy.parse_antigravity_conversation(c.path, profile=c.origin)
    if c.tool == "copilot_cli":
        return _copilot.parse_copilot_session(c.path)
    if c.tool == "cursor":
        return _cursor.parse_cursor_composer(c.path, c.ref) if c.ref else _cursor.parse_cursor_transcript(c.path)
    if c.tool == "opencode":
        return _opencode.parse_opencode_session(c.path, c.ref)
    if c.tool == "pi":
        return _pi.parse_pi_session(c.path)
    if c.tool == "hermes":
        return _hermes.parse_hermes_session(c.path, c.ref)
    if c.tool == "openclaw":
        return _openclaw.parse_openclaw_session(c.path, c.ref or os.path.basename(c.path)[:-6])
    if c.tool == "nanoclaw":
        return _nanoclaw.parse_nanoclaw_session(c.path)
    if c.tool in _more.TOOLS:
        return _more.parse(c.tool, c.session_id, c.path, c.ref)
    return parse_session_file(c.path)
def _project_key(c: Candidate, session: Any) -> Optional[str]:
    if c.tool == "claude_code":
        return hashlib.sha256(os.path.basename(os.path.dirname(c.path)).encode()).hexdigest()[:16]
    enc = (session.metadata or {}).get("encoded_project_dir")
    return hashlib.sha256(str(enc).encode()).hexdigest()[:16] if enc else None
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md")
PROJECT_INSTRUCTION_FILES = INSTRUCTION_FILES + (os.path.join(".github", "copilot-instructions.md"),)
CONFIG_NAME_CAP = 50
CONFIG_FILE_BYTES = 2 << 20
_CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+ -]{0,63}$")
_INSTALL_ID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}|[0-9a-f]{16,}", re.I)
_TOML_MCP_RE = re.compile(r"^\s*\[mcp_servers\.([A-Za-z0-9_.\"-]+)\]\s*$", re.M)
_CONFIG_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
def _config_name(n: Any) -> Optional[str]:
    s = str(n or "").strip()
    for ext in (".md", ".toml", ".json"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
    if not s or not _CONFIG_NAME_RE.match(s):
        return None
    return "dynamic" if _INSTALL_ID_RE.search(s) else s
def _names(items: Iterable[Any]) -> List[str]:
    out = sorted({x for x in (_config_name(i) for i in items) if x})
    return out[:CONFIG_NAME_CAP]
def _listdir(d: str) -> List[str]:
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []
def _json_keys(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.getsize(path) > CONFIG_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None
def _claude_dir_names(base: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sk = os.path.join(base, "skills")
    out["skills"] = _names(n for n in _listdir(sk) if os.path.isfile(os.path.join(sk, n, "SKILL.md")))
    cmd = os.path.join(base, "commands")
    cmds: List[str] = []
    for n in _listdir(cmd):
        full = os.path.join(cmd, n)
        if n.endswith(".md") and os.path.isfile(full):
            cmds.append(n)
        elif os.path.isdir(full):
            cmds += ["%s:%s" % (n, m) for m in _listdir(full) if m.endswith(".md")]
    out["commands"] = _names(cmds)
    ag = os.path.join(base, "agents")
    out["agents"] = _names(n for n in _listdir(ag) if n.endswith(".md"))
    hooks: List[str] = []
    deny = allow = 0
    sandbox = False
    for f in ("settings.json",):
        doc = _json_keys(os.path.join(base, f))
        if not doc:
            continue
        if isinstance(doc.get("hooks"), dict):
            hooks += [k for k, v in doc["hooks"].items() if v]
        perms = doc.get("permissions") if isinstance(doc.get("permissions"), dict) else {}
        deny += len(perms.get("deny") or []) if isinstance(perms.get("deny"), list) else 0
        allow += len(perms.get("allow") or []) if isinstance(perms.get("allow"), list) else 0
        sb = doc.get("sandbox")
        sandbox = sandbox or (isinstance(sb, dict) and sb.get("enabled") is True) or sb is True
    out["hooks"] = _names(hooks)
    out["permission_rules"] = {"deny": deny, "allow": allow}
    out["sandbox"] = bool(sandbox)
    return out
def _mcp_names(path: str) -> List[str]:
    doc = _json_keys(path)
    servers = (doc or {}).get("mcpServers")
    return _names(servers.keys()) if isinstance(servers, dict) else []
def _scope(tool: str, scope: str, base: str) -> Dict[str, Any]:
    if tool == "claude_code" and scope == "user":
        d = _claude_dir_names(base)
        d["instruction_files"] = [f for f in ("CLAUDE.md",) if os.path.isfile(os.path.join(base, f))]
        d["mcp_servers"] = _mcp_names(os.path.join(os.path.dirname(base), ".claude.json"))
        return d
    if scope == "project":
        d = _claude_dir_names(os.path.join(base, ".claude")) if tool == "claude_code" else {}
        d["instruction_files"] = [os.path.basename(f) if f.endswith(".md") and os.sep not in f else
                                  f.replace(os.sep, "/") for f in PROJECT_INSTRUCTION_FILES
                                  if os.path.isfile(os.path.join(base, f))]
        if tool == "claude_code":
            d["mcp_servers"] = _mcp_names(os.path.join(base, ".mcp.json"))
        return d
    if tool == "codex":
        d = {"instruction_files": [f for f in ("AGENTS.md",) if os.path.isfile(os.path.join(base, f))],
             "commands": _names(n for n in _listdir(os.path.join(base, "prompts")) if n.endswith(".md"))}
        try:
            with open(os.path.join(base, "config.toml"), "r", encoding="utf-8") as fh:
                txt = fh.read(CONFIG_FILE_BYTES)
            d["mcp_servers"] = _names(m.strip('"') for m in _TOML_MCP_RE.findall(txt))
        except OSError:
            pass
        return d
    if tool == "gemini_cli":
        return {"instruction_files": [f for f in ("GEMINI.md",) if os.path.isfile(os.path.join(base, f))],
                "commands": _names(n for n in _listdir(os.path.join(base, "commands")) if n.endswith(".toml")),
                "mcp_servers": _mcp_names(os.path.join(base, "settings.json"))}
    return {}
def config_inventory(tool: str, cwd: Optional[str]) -> Dict[str, Any]:
    home = os.path.expanduser("~")
    user_base = {"claude_code": os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"),
                 "codex": os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"),
                 "gemini_cli": os.path.join(home, ".gemini")}.get(tool)
    out: Dict[str, Any] = {"v": 1, "tool": tool}
    for scope, base in (("user", user_base), ("project", cwd)):
        if not base or not os.path.isdir(base) or (scope == "project" and os.path.realpath(base) == home):
            continue
        key = (tool + ":" + scope, os.path.realpath(base))
        if key not in _CONFIG_CACHE:
            try:
                _CONFIG_CACHE[key] = {k: v for k, v in _scope(tool, scope, base).items() if v}
            except Exception:
                log_exc("config_inventory")
                _CONFIG_CACHE[key] = {}
        if _CONFIG_CACHE[key]:
            out[scope] = _CONFIG_CACHE[key]
    return out
def build_bundle(c: Candidate, state: State, trigger: str) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    session = parse_candidate(c)
    dropped, owner_sids, claim = 0, set(), []
    if c.tool == "claude_code":
        session_id = c.session_id
        all_keys = sorted({k for t in session.turns for k in _turn_keys(t)})
        owners = state.owners(all_keys)
        kept = []
        for t in session.turns:
            foreign = [owners[k] for k in _turn_keys(t) if k in owners and owners[k] != session_id]
            if foreign:
                dropped += 1
                owner_sids.update(foreign)
            else:
                kept.append(t)
        session.turns = kept
        claim = sorted({k for t in kept for k in _turn_keys(t)})
    else:
        session_id = session.id
    units, _removed = _health.drop_session(session, surface=c.tool)
    signals = compute_signals(session)
    signals["skills_used"] = skill_counts(session)
    signals["signals_version"] = SIGNALS_VERSION
    signals["config_files"] = config_inventory(c.tool, session.cwd)
    prev = state.get(c.key)
    revision = (prev[1] + 1) if prev else 1
    render_counts: Dict[str, int] = {}
    unit_dicts = []
    for u in units:
        d = dataclasses.asdict(u)
        d["est_tokens"] = u.est_tokens
        unit_dicts.append(d)
    for u in units:
        render_counts["secrets_redacted"] = render_counts.get("secrets_redacted", 0) + int(
            u.facts.get("secrets_redacted") or 0)
    meta = session.metadata or {}
    sess_block: Dict[str, Any] = {
        "session_id": session_id,
        "revision": revision,
        "supersedes": prev[2] if prev else None,
        "source": c.tool,
        "origin": c.origin,
        "project_key": _project_key(c, session),
        "started_at": _iso(session.started_at),
        "ended_at": _iso(session.ended_at),
        "entrypoint": meta.get("entrypoint") if c.tool == "claude_code" else meta.get("entry_source"),
        "subagent_sessions": sum(1 for _ in session.iter_sessions()) - 1,
        "dedupe": {"dropped_turns": dropped, "owner_sessions": sorted(owner_sids)},
    }
    if c.tool == "claude_code":
        sess_block["claude_code_versions"] = list(meta.get("versions") or [])[:20]
    else:
        sess_block["tool_versions"] = [str(v)[:64] for v in (meta.get("versions") or [])][:20]
    bundle: Dict[str, Any] = {
        "bundle_format": BUNDLE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "bundle_id": str(uuid.uuid4()),
        "created_at_utc": _iso(_dt.datetime.now(_dt.timezone.utc)),
        "trigger": trigger,
        "plugin": {"name": PLUGIN_NAMES.get(RUNNING_TOOL[0], "hours"), "version": plugin_version()},
        "engine": {"render_version": getattr(_render, "RENDER_VERSION", None), **vendor_info()},
        "collector": {"python": platform.python_version(), "os": platform.system().lower()},
        "session": sess_block,
        "signals": signals,
        "units": unit_dicts,
    }
    bundle = json.loads(json.dumps(bundle, default=_jsonable))
    bundle, _ = _health.drop_bundle(bundle)
    final_counts: Dict[str, int] = {}
    residual: Dict[str, int] = {}
    for _ in range(3):
        fs = FinalScrubber()
        bundle = scrub_tree(bundle, fs)
        for k, v in fs.counts.items():
            final_counts[k] = final_counts.get(k, 0) + v
        residual = detect_tree(bundle)
        if not residual:
            break
    bundle["scrub"] = {"render_secret_placeholders": render_counts.get("secrets_redacted", 0),
                       "final_pass_counts": final_counts, "verify_residual_counts": residual}
    agy_file = (session.metadata or {}).get("transcript_file") if c.tool == "antigravity" else None
    bundle["attest"] = attest_for(c, agy_file)
    stats = {"units": len(bundle.get("units") or []), "dropped_turns": dropped, "final_pass": sum(final_counts.values()),
             "residual": sum(residual.values()), "revision": revision,
             "human_prompts": int(signals.get("human_prompts") or 0)}
    return bundle, claim, stats
def _agy_transcript(logs: str, used: Optional[str] = None) -> Optional[str]:
    if used in ("transcript_full.jsonl", "transcript.jsonl") and os.path.isfile(os.path.join(logs, used)):
        return os.path.join(logs, used)
    for p in _agy.transcript_paths(logs):
        try:
            if os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None
def attest_for(c: Candidate, agy_file: Optional[str] = None) -> Dict[str, Any]:
    if c.tool not in WITNESS_TOOLS:
        return {"v": 2, "source": c.tool, "unreadable": True}
    try:
        path = c.path
        if c.tool == "antigravity" and os.path.isdir(path):
            path = _agy_transcript(_agy.logs_dir_for(path), agy_file) or path
        att: Dict[str, Any] = {"v": 2, "source": c.tool, "fp": _views.file_blob(c.tool, path)}
        subs: List[str] = []
        if c.tool == "claude_code":
            sd = os.path.join(os.path.dirname(c.path), c.session_id, "subagents")
            for dirpath, _dirs, names in os.walk(sd):
                subs.extend(os.path.join(dirpath, n) for n in sorted(names) if n.endswith(".jsonl"))
        elif c.tool == "gemini_cli":
            subs = list(c.extra_paths or [])
        if subs:
            att["subs"] = [_views.file_blob(c.tool, sp) for sp in subs[:500]]
        return att
    except Exception:
        log_exc("attest")
        return {"v": 2, "source": c.tool, "unreadable": True}
def _witness_target(args: argparse.Namespace) -> Optional[Tuple[str, str]]:
    path = args.hook_transcript or ""
    if not path and args.tool == "copilot_cli" and args.hook_session:
        path = _copilot.events_path_for(args.hook_session) or ""
    if not path or not os.path.isfile(path) or not path.endswith(".jsonl"):
        return None
    tool = _sources.tool_for_path(path) or args.tool
    if tool == "antigravity":
        path = _agy_transcript(os.path.dirname(path)) or path
    return (path, tool) if tool in WITNESS_TOOLS else None
def _witness_sid(tool: str, path: str, first: Optional[Dict[str, Any]]) -> Optional[str]:
    parts = os.path.normpath(path).split(os.sep)
    sid: Optional[str] = None
    if tool == "claude_code":
        sid = os.path.basename(path)[:-len(".jsonl")]
    elif tool == "codex":
        sid = _codex.thread_id_from_name(path)
    elif tool == "copilot_cli":
        sid = parts[-2] if parts[-1] == "events.jsonl" else os.path.basename(path)[:-len(".jsonl")]
    elif tool == "antigravity" and "brain" in parts and parts.index("brain") + 1 < len(parts):
        sid = parts[parts.index("brain") + 1]
    elif tool == "gemini_cli" and isinstance(first, dict):
        sid = str(first.get("sessionId") or "")
    sid = (sid or "").lower()
    return sid if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", sid) else None
def _tail_hash(path: str, off: int) -> Optional[str]:
    if off <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, off - 64))
            return hashlib.sha256(fh.read(min(64, off))).hexdigest()[:16]
    except OSError:
        return None
def witness_once(ddir: str, server: "Server", path: str, tool: str) -> int:
    spath = os.path.join(ddir, WITNESS_FILE)
    try:
        with open(spath) as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    key = hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16]
    st = state.get(key) if isinstance(state.get(key), dict) else {}
    off, n = int(st.get("off") or 0), int(st.get("n") or 0)
    size = os.path.getsize(path)
    if size < off or (off and st.get("tail") != _tail_hash(path, off)):
        off, n = 0, 0
    with open(path, "rb") as fh:
        fh.seek(off)
        data = fh.read(min(size - off, WITNESS_READ_CAP))
    end = data.rfind(b"\n")
    if end < 0:
        return 0
    data = data[:end + 1]
    hashes: List[str] = []
    keys: List[str] = []
    last_t: Optional[float] = None
    first = st.get("first") if isinstance(st.get("first"), dict) else None
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict) or rec.get("type") == DEMO_LINE_TYPE:
            continue
        if first is None and tool == "gemini_cli":
            first = {"sessionId": rec.get("sessionId")}
        h = _rec_hash(tool, rec)
        if not h:
            continue
        hashes.append(h)
        keys.append(_rec_key(tool, rec))
        t = _auth_ts(rec.get("timestamp") or rec.get("created_at"))
        if t is not None and (last_t is None or t.t > last_t):
            last_t = t.t
    sid = st.get("sid") or _witness_sid(tool, path, first)
    if not sid:
        return 0
    sent = 0
    for i in range(0, len(hashes), WITNESS_CHUNK):
        chunk_h, chunk_k = hashes[i:i + WITNESS_CHUNK], keys[i:i + WITNESS_CHUNK]
        body = {"source": tool, "session_id": sid, "start_index": n + i, "record_count": n + i + len(chunk_h),
                "hashes": chunk_h, "keys": chunk_k,
                "last_record_at": (_dt.datetime.fromtimestamp(last_t, _dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ") if last_t is not None else None)}
        try:
            server.witness(body)
        except Exception:
            log_exc("witness")
            break
        sent += len(chunk_h)
    if sent == len(hashes):
        new_off = off + len(data)
        state[key] = {"off": new_off, "n": n + sent, "tail": _tail_hash(path, new_off), "sid": sid,
                      "first": first, "at": int(time.time())}
        if len(state) > WITNESS_KEEP:
            for k in sorted(state, key=lambda k: (state[k] or {}).get("at", 0))[:len(state) - WITNESS_KEEP]:
                state.pop(k, None)
        write_atomic(spath, json.dumps(state).encode("utf-8"))
    return sent
def witness_hook(ddir: str, args: argparse.Namespace) -> None:
    try:
        tgt = _witness_target(args)
        if tgt is None:
            return
        mode, server, _reason = decide_mode(ddir, args)
        if mode != "server" or server is None:
            return
        lockp = os.path.join(ddir, "witness.lock")
        fd = os.open(lockp, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as lk:
            try:
                fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            n = witness_once(ddir, server, tgt[0], tgt[1])
            log.info("witness %s: %d records reported", args.hook, n)
    except Exception:
        log_exc("witness")
def write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp-%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
class HttpStatus(Exception):
    def __init__(self, status: int, code: Optional[str] = None) -> None:
        super().__init__("HTTP %d" % status)
        self.status = status
        self.code = code
class TokenOriginError(Exception):
    pass
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpStatus(int(code))
_OPENER = urllib.request.build_opener(_NoRedirect())
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
def http_call(method: str, url: str, *, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
              timeout: float = REQUEST_TIMEOUT) -> Tuple[int, Any]:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            status = int(getattr(r, "status", 200))
            raw = r.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as e:
        code = None
        try:
            err = json.loads(e.read(4096).decode("utf-8"))
            if isinstance(err, dict) and isinstance(err.get("error"), str) and re.match(r"^[a-z_]{1,40}$",
                                                                                          err["error"]):
                code = err["error"]
        except Exception:
            pass
        try:
            e.close()
        except Exception:
            pass
        raise HttpStatus(int(e.code), code)
    if not 200 <= status < 300:
        raise HttpStatus(status)
    if len(raw) > MAX_REPLY_BYTES:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8")) if raw.strip() else None
    except Exception:
        return status, None
def _transient(exc: BaseException) -> bool:
    if isinstance(exc, HttpStatus):
        return exc.status in TRANSIENT_STATUS
    return isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError))
def with_retries(fn, retries: int = RETRIES):
    attempt = 0
    while True:
        try:
            return fn(attempt)
        except (HttpStatus, urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            if attempt >= retries or not _transient(e):
                raise
            time.sleep(retry_base_seconds() * (2 ** attempt))
            attempt += 1
def _json_body(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")
class Server:
    def __init__(self, dev: Dict[str, Any]) -> None:
        self.origin = str(dev["origin"])
        self._token = str(dev["device_token"])
        self.batch_url = str(dev.get("batch_url") or (self.origin + "/api/v1/batches"))
        self._first = True
    def url(self, path: str) -> str:
        return self.origin + path
    def _send(self, method: str, url: str, body: bytes, extra: Optional[Dict[str, str]] = None, *,
              retries: int = RETRIES, timeout: Optional[float] = None, gz: bool = False) -> Any:
        if origin_of(url) != self.origin:
            raise TokenOriginError()
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json",
                   "X-Hours-Plugin-Version": plugin_version(), "Accept": "application/json"}
        if gz:
            headers["Content-Encoding"] = "gzip"
            headers["X-Hours-Bundle-Format"] = BUNDLE_FORMAT
        headers.update(extra or {})
        first = self._first
        self._first = False
        def go(attempt: int) -> Any:
            t = timeout if timeout is not None else (FIRST_REQUEST_TIMEOUT if first and attempt == 0
                                                     else REQUEST_TIMEOUT)
            return http_call(method, url, body=body, headers=headers, timeout=t)[1]
        return with_retries(go, retries)
    def create_batch(self, sessions_total: int, raw_bytes_total: int) -> str:
        reply = self._send("POST", self.batch_url, _json_body({"sessions_total": int(sessions_total),
                                                               "raw_bytes_total": int(raw_bytes_total)}))
        bid = str((reply or {}).get("batch_id") or "") if isinstance(reply, dict) else ""
        if not BATCH_ID_RE.match(bid):
            raise HttpStatus(502)
        return bid
    def progress(self, batch_id: str, stage: str, sessions_done: int, raw_bytes_done: int,
                 bundle_bytes_total: int) -> None:
        self._send("POST", self.url("/api/v1/batches/%s/progress" % batch_id),
                   _json_body({"stage": stage, "sessions_done": int(sessions_done),
                               "raw_bytes_done": int(raw_bytes_done), "bundle_bytes_total": int(bundle_bytes_total)}),
                   retries=0, timeout=PROGRESS_TIMEOUT)
    def bundle(self, batch_id: str, gz_body: bytes) -> None:
        self._send("POST", self.url("/api/v1/bundles"), gz_body, {"X-Hours-Batch": batch_id}, gz=True)
    def complete(self, batch_id: str) -> None:
        self._send("POST", self.url("/api/v1/batches/%s/complete" % batch_id), _json_body({}))
    def witness(self, body: Dict[str, Any]) -> None:
        self._send("POST", self.url("/api/v1/witness"), _json_body(body), retries=1, timeout=PROGRESS_TIMEOUT)
class Progress:
    def __init__(self, server: Optional[Server], batch_id: Optional[str]) -> None:
        self.server, self.batch_id, self.last, self.warned = server, batch_id, 0.0, False
    def post(self, stage: str, sessions_done: int, raw_done: int, bundle_total: int, force: bool = False) -> None:
        if not self.server or not self.batch_id:
            return
        now = time.time()
        if not force and now - self.last < PROGRESS_MIN_INTERVAL:
            return
        self.last = now
        try:
            self.server.progress(self.batch_id, stage, sessions_done, raw_done, bundle_total)
        except Exception as e:
            if not self.warned:
                self.warned = True
                log.warning("progress post failed (%s); continuing", type(e).__name__)
def mark_revoked(ddir: str) -> None:
    dev = load_device(ddir)
    if dev and not dev.get("revoked_at"):
        dev["revoked_at"] = _iso(_dt.datetime.now(_dt.timezone.utc))
        save_device(ddir, dev)
    log.error("the site no longer accepts this device's token (removed on the website?); run /hours:connect again")
def _meta_path(outbox: str, name: str) -> str:
    return os.path.join(outbox, name[:-5] + ".meta")
def read_meta(outbox: str, name: str) -> Dict[str, int]:
    try:
        with open(_meta_path(outbox, name), "r") as fh:
            m = json.load(fh)
        return {"raw_bytes": int(m.get("raw_bytes") or 0), "gz_bytes": int(m.get("gz_bytes") or 0)}
    except Exception:
        return {"raw_bytes": 0, "gz_bytes": 0}
def outbox_files(outbox: str) -> List[str]:
    try:
        return sorted(f for f in os.listdir(outbox) if f.endswith(".json"))
    except OSError:
        return []
def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
def deliver(ddir: str, inbox_override: Optional[str]) -> Dict[str, int]:
    outbox = os.path.join(ddir, "outbox")
    os.makedirs(outbox, mode=0o700, exist_ok=True)
    res = {"delivered": 0, "failed": 0}
    inbox = dev_inbox(inbox_override)
    files = outbox_files(outbox)
    if files:
        os.makedirs(inbox, mode=0o700, exist_ok=True)
    for f in files:
        try:
            shutil.move(os.path.join(outbox, f), os.path.join(inbox, f))
            _remove_quiet(_meta_path(outbox, f))
            res["delivered"] += 1
        except Exception:
            res["failed"] += 1
            log_exc("dev-inbox move")
    return res
def skill_counts(session) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for t in session.iter_turns(recursive=True):
        names = []
        if getattr(t, "skill", None):
            names.append(t.skill)
        if t.kind == "tool_call" and t.tool and t.tool.name == "Skill" and t.tool.input_summary:
            names.append(t.tool.input_summary)
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    return counts
PERMANENT_REJECT = {400, 413, 415, 422}
def deliver_server(ddir: str, server: Server, batch_id: str, prog: Progress, sessions_done: int,
                   raw_done: int) -> Dict[str, Any]:
    outbox = os.path.join(ddir, "outbox")
    res: Dict[str, Any] = {"delivered": 0, "failed": 0, "rejected": 0, "not_accepted": 0, "demo_only": 0,
                           "bundle_bytes_sent": 0, "bundle_bytes_total": 0, "stopped": None}
    files = outbox_files(outbox)
    res["bundle_bytes_total"] = sum(read_meta(outbox, f)["gz_bytes"] for f in files)
    prog.post("upload", sessions_done, raw_done, res["bundle_bytes_total"], force=True)
    for i, f in enumerate(files):
        p = os.path.join(outbox, f)
        try:
            with open(p, "rb") as fh:
                body = gzip.compress(fh.read(), compresslevel=6, mtime=0)
        except OSError:
            res["failed"] += 1
            log_exc("outbox read")
            continue
        try:
            server.bundle(batch_id, body)
            ok = True
        except HttpStatus as e:
            ok = e.status == 409
            if not ok:
                if e.status == 401:
                    mark_revoked(ddir)
                    res["stopped"] = "token_refused"
                elif e.status in PERMANENT_REJECT:
                    rdir = os.path.join(ddir, "rejected")
                    os.makedirs(rdir, mode=0o700, exist_ok=True)
                    shutil.move(p, os.path.join(rdir, f))
                    _remove_quiet(_meta_path(outbox, f))
                    res["rejected"] += 1
                    if e.code == "not_accepted":
                        res["not_accepted"] += 1
                    elif e.code == "demo_only":
                        res["demo_only"] += 1
                    log.error("bundle %s refused by the server (HTTP %d); kept in rejected/", f[:8], e.status)
                    continue
                elif 300 <= e.status < 400:
                    res["stopped"] = "redirect_refused"
                    log.error("upload refused: the site answered with a redirect; nothing re-sent")
                else:
                    res["stopped"] = "http_%d" % e.status
                    log.error("upload failed: HTTP %d; bundles kept in the outbox", e.status)
                res["failed"] += len(files) - i
                break
        except TokenOriginError:
            res["stopped"] = "origin_mismatch"
            res["failed"] += len(files) - i
            log.error("upload refused: target is not the paired site")
            break
        except Exception as e:
            res["stopped"] = type(e).__name__
            res["failed"] += len(files) - i
            log.error("upload failed: %s; bundles kept in the outbox", type(e).__name__)
            break
        if ok:
            _remove_quiet(p)
            _remove_quiet(_meta_path(outbox, f))
            res["delivered"] += 1
            res["bundle_bytes_sent"] += len(body)
            prog.post("upload", sessions_done, raw_done, res["bundle_bytes_total"])
    return res
def hook_key(cands: Dict[str, Candidate], tool: str, hook_session: Optional[str],
             hook_transcript: Optional[str]) -> Tuple[Optional[str], str, Optional[str]]:
    if hook_transcript:
        where = _sources.tool_for_path(hook_transcript)
        if where:
            tool = where
        elif tool != "claude_code":
            hook_transcript = None
    if hook_transcript:
        real = os.path.realpath(hook_transcript)
        for k, c in cands.items():
            if c.tool != tool:
                continue
            cp = os.path.realpath(c.path)
            if cp == real or (c.tool == "antigravity" and real.startswith(cp + os.sep)):
                return k, tool, None
    if hook_session:
        k = hook_session if tool == "claude_code" else "%s:%s" % (tool, hook_session.lower())
        if k in cands:
            return k, tool, None
        if tool == "copilot_cli" and not hook_transcript:
            hook_transcript = _copilot.events_path_for(hook_session)
        if hook_transcript and os.path.isfile(hook_transcript):
            return k, tool, hook_transcript
    return None, tool, None
def select(cands: Dict[str, Candidate], state: State, *, sessions: Optional[List[str]], hook_event: Optional[str],
           hook_session: Optional[str], hook_transcript: Optional[str],
           hook_tool: Optional[str] = None) -> List[Candidate]:
    now = time.time()
    picked: List[Candidate] = []
    key, tool, add_path = hook_key(cands, hook_tool or "claude_code", hook_session, hook_transcript)
    if key and add_path and key not in cands:
        st = os.stat(add_path)
        real = os.path.realpath(add_path)
        sid = key.split(":", 1)[-1]
        if tool == "claude_code":
            cands[key] = Candidate(sid, real, "claude_code", "%d:%d:0:0" % (st.st_size, int(st.st_mtime)),
                                   st.st_mtime, _birth(st), st.st_size, st.st_size)
        elif tool == "antigravity":
            logs = _agy.logs_dir_for(real)
            cands[key] = Candidate(sid, logs, "antigravity", "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime,
                                   _birth(st), st.st_size, st.st_size, tool=tool, key=key)
        else:
            cands[key] = Candidate(sid, real, tool, "%d:%d" % (st.st_size, int(st.st_mtime)), st.st_mtime,
                                   _birth(st), st.st_size, st.st_size, tool=tool, key=key,
                                   compressed=real.endswith(".zst"))
    for k, c in cands.items():
        if sessions is not None:
            if any(k.startswith(s) or (c.session_id and c.session_id.startswith(s)) for s in sessions):
                picked.append(c)
            continue
        if hook_event == "SessionStart" and k == key:
            continue
        prev = state.get(k)
        if prev and (prev[0] == c.signature or (c.compressed and prev[1] > 0)):
            continue
        ending = hook_event in ("SessionEnd", "Stop") and k == key
        if hook_event and not ending and now - c.newest_mtime < IDLE_MINUTES * 60:
            continue
        picked.append(c)
    picked.sort(key=lambda c: (c.birth, c.key))
    return picked
def run_once(ddir: str, args: argparse.Namespace, trigger: str, server: Optional[Server] = None) -> Dict[str, Any]:
    state = State(os.path.join(ddir, "state.sqlite3"))
    outbox = os.path.join(ddir, "outbox")
    os.makedirs(outbox, mode=0o700, exist_ok=True)
    summary: Dict[str, Any] = {"trigger": trigger, "candidates": 0, "packaged": 0, "errors": 0, "quarantined": 0,
                               "units": 0, "dropped_turns": 0, "final_pass_hits": 0, "residual_hits": 0,
                               "skipped_empty": 0, "skipped_compressed": 0, "by_tool": {}}
    try:
        skipped: Dict[str, int] = {}
        cands = discover_all(args.since, skipped)
        summary["skipped_compressed"] = skipped.get("codex_compressed", 0)
        picked = select(cands, state, sessions=args.sessions, hook_event=args.hook,
                        hook_session=args.hook_session, hook_transcript=args.hook_transcript,
                        hook_tool=args.tool)
        summary["candidates"] = len(picked)
        leftovers = outbox_files(outbox)
        left_raw = sum(read_meta(outbox, f)["raw_bytes"] for f in leftovers)
        sessions_total = len(picked) + len(leftovers)
        raw_total = sum(c.raw_bytes for c in picked) + left_raw
        summary["sessions_total"] = sessions_total
        summary["raw_bytes_total"] = raw_total
        batch_id: Optional[str] = None
        if server is not None:
            if not sessions_total:
                summary.update({"delivered": 0, "failed": 0})
                return summary
            try:
                batch_id = server.create_batch(sessions_total, raw_total)
                summary["batch_id"] = batch_id
            except HttpStatus as e:
                summary["batch_error"] = "http_%d" % e.status
                if e.status == 401:
                    mark_revoked(ddir)
                else:
                    log.error("could not open a batch: HTTP %d; cleaning anyway, sending next time", e.status)
            except Exception as e:
                summary["batch_error"] = type(e).__name__
                log.error("could not open a batch: %s; cleaning anyway, sending next time", type(e).__name__)
        prog = Progress(server, batch_id)
        done, raw_done, gz_total = len(leftovers), left_raw, sum(read_meta(outbox, f)["gz_bytes"] for f in leftovers)
        prog.post("clean", done, raw_done, gz_total, force=True)
        for c in picked:
            t0 = time.time()
            try:
                try:
                    bundle, claim, st = build_bundle(c, state, trigger)
                except ZstdUnavailable:
                    summary["skipped_compressed"] += 1
                    continue
                except Exception:
                    summary["errors"] += 1
                    log_exc("session %s" % c.label)
                    continue
                if c.tool != "claude_code" and (not st["units"] or not st["human_prompts"]):
                    state.mark_seen(c.key, c.signature)
                    summary["skipped_empty"] += 1
                    continue
                sid = bundle["session"]["session_id"]
                if st["residual"]:
                    qdir = os.path.join(ddir, "quarantine")
                    os.makedirs(qdir, mode=0o700, exist_ok=True)
                    write_atomic(os.path.join(qdir, "%s.r%d.json" % (sid, st["revision"])),
                                 json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
                    summary["quarantined"] += 1
                    log.error("session %s quarantined: %d residual scrubber hits", c.label, st["residual"])
                    continue
                name = "%s.r%d.%s.json" % (sid, st["revision"], bundle["bundle_id"][:8])
                data = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
                gz_bytes = len(gzip.compress(data, compresslevel=6, mtime=0))
                write_atomic(_meta_path(outbox, name), _json_body({"raw_bytes": c.raw_bytes, "gz_bytes": gz_bytes}))
                write_atomic(os.path.join(outbox, name), data)
                gz_total += gz_bytes
                state.commit_session(c.key, c.signature, st["revision"], bundle["bundle_id"], claim)
                summary["packaged"] += 1
                summary["by_tool"][c.tool] = summary["by_tool"].get(c.tool, 0) + 1
                summary["units"] += st["units"]
                summary["dropped_turns"] += st["dropped_turns"]
                summary["final_pass_hits"] += st["final_pass"]
                log.info("session %s r%d: %d units, %d copied turns dropped, %d final-pass hits, %.1fs",
                         c.label, st["revision"], st["units"], st["dropped_turns"], st["final_pass"],
                         time.time() - t0)
            finally:
                done += 1
                raw_done += c.raw_bytes
                prog.post("clean", done, raw_done, gz_total)
        if picked:
            prog.post("clean", done, raw_done, gz_total, force=True)
        if server is None:
            summary.update(deliver(ddir, args.inbox))
        elif batch_id is None:
            summary.update({"delivered": 0, "failed": len(outbox_files(outbox))})
        else:
            res = deliver_server(ddir, server, batch_id, prog, done, raw_done)
            summary.update(res)
            if res["delivered"] > 0 or res["failed"] == 0:
                try:
                    server.complete(batch_id)
                    summary["completed"] = True
                except Exception as e:
                    summary["completed"] = False
                    log.error("batch complete failed: %s", type(e).__name__ if not isinstance(e, HttpStatus)
                              else "HTTP %d" % e.status)
    finally:
        state.close()
    return summary
def acquire_lock(ddir: str) -> Optional[Any]:
    fh = open(os.path.join(ddir, "collector.lock"), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError as e:
        fh.close()
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
def opt_in(ddir: str) -> None:
    path = os.path.join(ddir, "dev_opt_in.json")
    rec = {"opted_in_at_utc": _iso(_dt.datetime.now(_dt.timezone.utc)), "plugin_version": plugin_version(),
           "scope": "dev inbox only; Claude Code transcripts on this computer, scrubbed locally; nothing is sent"}
    write_atomic(path, (json.dumps(rec, indent=1) + "\n").encode())
def _say(lines: List[str], result: Dict[str, Any]) -> None:
    for line in lines:
        print(line)
    print(json.dumps(result, sort_keys=True))
def _clean_label(v: Any) -> str:
    s = "".join(ch for ch in str(v or "") if ch.isprintable())
    return s[:120] or "your Hours account"
def _range(cs: List[Candidate]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"sessions": len(cs), "raw_bytes": sum(c.raw_bytes for c in cs),
                           "first_date_pt": None, "last_date_pt": None}
    if cs:
        out["first_date_pt"] = pt_date(min(min(c.birth, c.newest_mtime) for c in cs))
        out["last_date_pt"] = pt_date(max(c.newest_mtime for c in cs))
    return out
def count_found(ddir: str) -> Dict[str, Any]:
    state = State(os.path.join(ddir, "state.sqlite3"))
    skipped: Dict[str, int] = {}
    try:
        picked = select(discover_all(None, skipped), state, sessions=None, hook_event=None, hook_session=None,
                        hook_transcript=None)
    finally:
        state.close()
    total = _range(picked)
    out: Dict[str, Any] = {"sessions_found": total["sessions"], "raw_bytes": total["raw_bytes"],
                           "first_date_pt": total["first_date_pt"], "last_date_pt": total["last_date_pt"],
                           "by_tool": {}, "codex_compressed_skipped": skipped.get("codex_compressed", 0)}
    for tool in TOOLS:
        cs = [c for c in picked if c.tool == tool]
        if cs:
            out["by_tool"][tool] = _range(cs)
    return out
def found_lines(found: Dict[str, Any]) -> List[str]:
    lines = []
    for tool in TOOLS:
        r = found["by_tool"].get(tool)
        if not r:
            continue
        lines.append("Found %d %s session%s from %s to %s (Pacific Time), %s on disk."
                     % (r["sessions"], TOOL_LABELS[tool], "" if r["sessions"] == 1 else "s", r["first_date_pt"],
                        r["last_date_pt"], human_bytes(r["raw_bytes"])))
    n = found.get("codex_compressed_skipped") or 0
    if n:
        lines.append("%d older Codex session%s %s compressed, and this computer's Python cannot open %s, so %s "
                     "skipped." % (n, "" if n == 1 else "s", "is" if n == 1 else "are", "it" if n == 1 else "them",
                                   "it is" if n == 1 else "they are"))
    return lines
def cmd_pair(ddir: str, args: argparse.Namespace, started_at: Optional[str] = None) -> int:
    started_at = started_at or _iso(_dt.datetime.now(_dt.timezone.utc))
    code = (args.pair or "").strip()
    site_arg = (args.endpoint or endpoint() or "").strip()
    site = normalize_site(site_arg)
    if not CODE_RE.match(code):
        _say(["Hours: that pairing code does not look right. Copy the whole line from the Hours start page."],
             {"paired": False, "error": "bad_code"})
        return 2
    if not site:
        _say(["Hours: that site address is not allowed. It must start with https:// (or http://localhost for "
              "testing) and have no path. Copy the whole line from the Hours start page."],
             {"paired": False, "error": "bad_site"})
        return 2
    deadline = time.time() + wake_seconds()
    awake = False
    while True:
        try:
            http_call("GET", site + "/api/v1/ping", timeout=max(1.0, min(15.0, deadline - time.time())))
            awake = True
            break
        except Exception:
            if time.time() >= deadline:
                break
            time.sleep(min(3.0, max(0.05, retry_base_seconds())))
    if not awake:
        _say(["Hours: the site at %s did not answer for %d seconds. Check your connection and run the same line "
              "again." % (site, int(wake_seconds()))], {"paired": False, "error": "site_unreachable"})
        return 3
    body = _json_body({"code": code, "plugin_version": plugin_version(), "os": os_label(), "started_at": started_at})
    try:
        _st, reply = http_call("POST", site + "/api/v1/devices/pair", body=body, timeout=FIRST_REQUEST_TIMEOUT,
                               headers={"Content-Type": "application/json", "Accept": "application/json",
                                        "X-Hours-Plugin-Version": plugin_version()})
    except HttpStatus as e:
        msg = {429: "Too many tries. Wait a few minutes, then get a new code on the Hours start page.",
               403: "The site needs you to accept its notice first. Open %s/start in your browser." % site}.get(
            e.status, "The code was not accepted. Codes work once. Get a new code at "
                      "%s/start and paste the new line." % site)
        if 300 <= e.status < 400:
            msg = "The site answered with a redirect, which Hours refuses. Check the address on the start page."
        _say(["Hours: " + msg], {"paired": False, "error": "http_%d" % e.status})
        return 4
    except Exception as e:
        _say(["Hours: pairing failed (%s). Run the same line again." % type(e).__name__],
             {"paired": False, "error": type(e).__name__})
        return 4
    reply = reply if isinstance(reply, dict) else {}
    token = str(reply.get("device_token") or "")
    consent = reply.get("collection_consent")
    batch_url = str(reply.get("batch_url") or "")
    if batch_url.startswith("/") and not batch_url.startswith("//"):
        batch_url = site + batch_url
    problems = []
    if not TOKEN_RE.match(token):
        problems.append("device_token")
    if not (isinstance(consent, dict) and consent.get("version") and consent.get("accepted_at")):
        problems.append("collection_consent")
    if origin_of(batch_url) != site or urllib.parse.urlsplit(batch_url).query \
            or urllib.parse.urlsplit(batch_url).fragment:
        problems.append("batch_url")
    if problems:
        log.error("pair reply refused: %s", ",".join(problems))
        _say(["Hours: the site's reply was not usable (%s), so nothing was saved and nothing will be sent."
              % ", ".join(problems)], {"paired": False, "error": "bad_reply", "fields": problems})
        return 5
    label = _clean_label(reply.get("account_label"))
    now = _iso(_dt.datetime.now(_dt.timezone.utc))
    prev = load_device(ddir)
    if prev and (prev.get("origin") != site or prev.get("account_label") != label):
        stamp = now.replace(":", "").replace("-", "")
        for name in ("outbox", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
            src = os.path.join(ddir, name)
            if os.path.exists(src):
                os.rename(src, os.path.join(ddir, "%s.previous-%s" % (name, stamp)))
        log.info("re-paired with a different site or account; earlier outbox and state set aside")
    save_device(ddir, {"origin": site, "device_token": token, "account_label": label, "batch_url": batch_url,
                       "paired_at": now, "started_at": started_at, "confirmed_at": None, "revoked_at": None,
                       "plugin_version": plugin_version()})
    write_atomic(os.path.join(ddir, "opt_in.json"), (json.dumps({
        "source": "server", "origin": site, "document": "collection",
        "version": str(consent["version"])[:64], "accepted_at": str(consent["accepted_at"])[:64],
        "recorded_at_utc": now, "plugin_version": plugin_version()}, indent=1) + "\n").encode())
    log.info("paired with %s (plugin %s)", site, plugin_version())
    try:
        write_home_pointer(ddir)
    except OSError:
        log.warning("could not record the shared data folder; other tools' plugins pair on their own")
    found = count_found(ddir)
    lines = ["Hours: connected to %s as %s." % (site, label)]
    if found["sessions_found"]:
        lines.extend(found_lines(found))
        lines.append("Nothing has been sent yet. They are cleaned on this computer first; only the cleaned copy "
                     "is sent.")
    else:
        lines.extend(found_lines(found))
        lines.append("Found no Claude Code, Codex, Gemini CLI, Antigravity or Copilot CLI sessions on this "
                     "computer. Some tools delete old sessions after 30 days, so new sessions will be sent as you "
                     "work.")
    lines.append("Manage your devices at %s/devices" % site)
    _say(lines, {"paired": True, "site": site, "account_label": label, "started_at": started_at, **found})
    return 0
def code_digest(code: str) -> str:
    return hashlib.sha256(("hours-pair-code\0" + code).encode("utf-8")).hexdigest()
def pending_pair(now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    code = (os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_CODE") or "").strip()
    site = normalize_site((os.environ.get("CLAUDE_PLUGIN_OPTION_ENDPOINT") or "").strip())
    if not CODE_RE.match(code) or not site:
        return None
    now = time.time() if now is None else now
    started_at = None
    raw = (os.environ.get("CLAUDE_PLUGIN_OPTION_CLOCK_START") or "").strip()
    if CLOCK_START_RE.match(raw) and abs(now - int(raw)) <= CLOCK_START_SKEW:
        started_at = _iso(_dt.datetime.fromtimestamp(int(raw), _dt.timezone.utc))
    return {"code": code, "site": site, "started_at": started_at, "digest": code_digest(code)}
def tried_codes(ddir: str) -> List[str]:
    try:
        with open(os.path.join(ddir, CODES_TRIED_FILE), "r") as fh:
            rec = json.load(fh)
        return [str(x) for x in rec.get("sha256", []) if isinstance(x, str)]
    except Exception:
        return []
def remember_code(ddir: str, digest: str) -> None:
    seen = [d for d in tried_codes(ddir) if d != digest] + [digest]
    write_atomic(os.path.join(ddir, CODES_TRIED_FILE),
                 (json.dumps({"sha256": seen[-CODES_TRIED_KEEP:]}, indent=1) + "\n").encode())
def auto_pair(ddir: str) -> bool:
    p = pending_pair()
    if p is None:
        return False
    lock = acquire_lock(ddir)
    deadline = time.time() + AUTO_PAIR_LOCK_WAIT
    while lock is None and time.time() < deadline:
        time.sleep(1)
        lock = acquire_lock(ddir)
    if lock is None:
        log.info("install-line pairing skipped: another collector held the lock")
        return False
    try:
        dev = load_device(ddir)
        if p["digest"] in tried_codes(ddir) or (dev and dev.get("pair_code_sha256") == p["digest"]):
            return False
        ns = argparse.Namespace(pair=p["code"], endpoint=p["site"], yes=True)
        rc = cmd_pair(ddir, ns, started_at=p["started_at"])
        if rc == 3:
            log.info("install-line pairing: the site did not answer; trying again at the next session start")
            return False
        remember_code(ddir, p["digest"])
        if rc != 0:
            log.info("install-line pairing refused (exit %d); this code is not tried again", rc)
            return False
        dev = load_device(ddir)
        if dev:
            dev["pair_code_sha256"] = p["digest"]
            dev["paired_by"] = "install_line"
            save_device(ddir, dev)
        log.info("paired from the install line; sending the first pass now")
        return True
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass
def confirm_device(ddir: str) -> Optional[Dict[str, Any]]:
    dev = load_device(ddir)
    if not dev or dev.get("revoked_at"):
        return None
    if not dev.get("confirmed_at"):
        dev["confirmed_at"] = _iso(_dt.datetime.now(_dt.timezone.utc))
        save_device(ddir, dev)
    return dev
def decide_mode(ddir: str, args: argparse.Namespace) -> Tuple[Optional[str], Optional[Server], str]:
    dev = load_device(ddir)
    if args.inbox:
        if has_dev_opt_in(ddir) or (dev and has_server_opt_in(ddir, dev["origin"])):
            return "dev", None, ""
        return None, None, "not opted in; nothing read"
    if dev:
        if dev.get("revoked_at"):
            return None, None, "this device was removed or its token refused; run /hours:connect again"
        if not dev.get("confirmed_at"):
            return None, None, "paired but the first upload is not confirmed yet; nothing read"
        if not has_server_opt_in(ddir, dev["origin"]):
            return None, None, "no collection consent from the server; nothing read"
        override = endpoint()
        if override and origin_of(override) != dev["origin"]:
            return None, None, "endpoint override differs from the paired site; nothing sent"
        return "server", Server(dev), ""
    if has_dev_opt_in(ddir):
        return "dev", None, ""
    return None, None, "not paired; nothing read and nothing sent"
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Hours transcript collector")
    ap.add_argument("--once", action="store_true", help="run one collection pass now and print a count summary")
    ap.add_argument("--sessions", nargs="+", help="only these session ids (prefixes ok); always repackaged")
    ap.add_argument("--inbox", help="dev inbox directory (forces dev mode, no network)")
    ap.add_argument("--since", type=float, help="only sessions changed in the last N days")
    ap.add_argument("--data-dir", help="state/log/outbox directory (default CLAUDE_PLUGIN_DATA or ~/.hours)")
    ap.add_argument("--opt-in", action="store_true", help="dev only: allow --inbox runs (never enables sending)")
    ap.add_argument("--pair", metavar="CODE", help="pairing code from the Hours start page (HRS-XXXX-XXXX)")
    ap.add_argument("--endpoint", metavar="URL", help="the Hours site address, for example https://hours.example")
    ap.add_argument("--yes", action="store_true", help="with --pair: confirm the first upload and send at once")
    ap.add_argument("--confirm", action="store_true", help="confirm the first upload, then run one full pass")
    ap.add_argument("--hook", choices=["SessionStart", "SessionEnd", "Stop", "unknown"])
    ap.add_argument("--tool", choices=list(TOOLS), default="claude_code",
                    help="which tool's plugin started this run (its hook session and its data folder)")
    ap.add_argument("--hook-session")
    ap.add_argument("--hook-transcript")
    ap.add_argument("--hook-detail")
    return ap.parse_args(argv)
def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many
def _report(summary: Dict[str, Any], site: Optional[str]) -> List[str]:
    lines = []
    sent = int(summary.get("delivered") or 0)
    if summary.get("batch_error"):
        lines.append("Hours: the site could not start an upload (%s). Your sessions are cleaned and kept on this "
                     "computer; they are sent when your next coding session starts or ends." % summary["batch_error"])
    elif not summary.get("sessions_total"):
        lines.append("Hours: nothing new to send.")
    else:
        if sent:
            lines.append("Hours: sent %d cleaned session%s (%s on disk, sent as %s)."
                         % (sent, _plural(sent, "", "s"), human_bytes(summary.get("raw_bytes_total") or 0),
                            human_bytes(summary.get("bundle_bytes_sent") or 0)))
        else:
            lines.append("Hours: no sessions were added this time.")
        failed = int(summary.get("failed") or 0)
        if failed:
            lines.append("%d %s could not be sent yet; %s kept on this computer and retried when your next coding "
                         "session starts or ends." % (failed, _plural(failed, "session", "sessions"),
                                                      _plural(failed, "it is", "they are")))
        refused = int(summary.get("not_accepted") or 0)
        if refused:
            what = "this session" if refused == 1 else "%d of these sessions" % refused
            lines.append("Unfortunately we can't accept %s. If you think this is a mistake, ask for help%s."
                         % (what, (": %s/help" % site) if site else " on the Hours site"))
        demo = int(summary.get("demo_only") or 0)
        if demo > 0:
            lines.append("%d %s not sent. This test site accepts only its own demo transcripts, so sessions from "
                         "your real work are refused here." % (demo, _plural(demo, "session was", "sessions were")))
        other = int(summary.get("rejected") or 0) - refused - demo
        if other > 0:
            lines.append("%d %s refused by the site and not sent."
                         % (other, _plural(other, "session was", "sessions were")))
        if sent:
            lines.append("Your score is being prepared. Follow it on the Hours page in your browser.")
    if site:
        lines.append("Manage your devices at %s/devices" % site)
    return lines
def stop_throttled(ddir: str) -> bool:
    path = os.path.join(ddir, "last_stop_sweep")
    try:
        if time.time() - os.path.getmtime(path) < STOP_THROTTLE_SECONDS:
            return True
    except OSError:
        pass
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass
    return False
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    os.umask(0o077)
    RUNNING_TOOL[0] = args.tool
    ddir = data_dir(args.data_dir, args.tool)
    setup_logging(ddir)
    if args.hook in ("SessionStart", "SessionEnd", "Stop"):
        witness_hook(ddir, args)
    if args.hook == "Stop" and args.tool not in STOP_SWEEP_TOOLS:
        return 0
    if args.hook == "Stop" and stop_throttled(ddir):
        log.info("Stop sweep skipped: the last one was under %d minutes ago", STOP_THROTTLE_SECONDS // 60)
        return 0
    if args.hook == "SessionStart" and args.tool == "claude_code" and args.pair is None and auto_pair(ddir):
        args.hook = None
        args.confirm = True
    if args.opt_in:
        opt_in(ddir)
        log.info("dev opt-in recorded")
        print("Hours: dev-inbox consent recorded in %s (nothing is ever sent in this mode)"
              % os.path.join(ddir, "dev_opt_in.json"))
        return 0
    if args.pair is not None:
        rc = cmd_pair(ddir, args)
        if rc or not args.yes:
            return rc
        args.confirm = True
    talk = False
    if args.confirm:
        if not confirm_device(ddir):
            _say(["Hours: this computer is not paired. Run the line from the Hours start page."],
                 {"error": "not_paired"})
            return 2
        args.once = True
        talk = True
    mode, server, reason = decide_mode(ddir, args)
    if mode is None:
        log.info("%s (%s)", reason, args.hook or "manual")
        if args.once:
            print(json.dumps({"error": reason}))
        return 0
    if args.hook:
        try:
            os.nice(10)
        except OSError:
            pass
    trigger = args.hook or "manual"
    lock = acquire_lock(ddir)
    if lock is None and (args.hook or talk):
        log.info("another collector is running; waiting for it")
        deadline = time.time() + LOCK_WAIT_SECONDS
        while lock is None and time.time() < deadline:
            time.sleep(2)
            lock = acquire_lock(ddir)
    if lock is None:
        log.info("another collector is running; skipped")
        if args.once:
            print(json.dumps({"error": "another collector run is in progress"}))
        return 0
    try:
        t0 = time.time()
        summary = run_once(ddir, args, trigger, server)
        summary["seconds"] = round(time.time() - t0, 2)
        log.info("run %s: %s", trigger, json.dumps(summary, sort_keys=True))
        if talk:
            for line in _report(summary, server.origin if server else None):
                print(line)
        if args.once:
            print(json.dumps(summary, sort_keys=True))
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass
    return 0
if __name__ == "__main__":
    _hook = "--hook" in sys.argv
    try:
        _rc = main()
    except Exception:
        _rc = 1
        try:
            log_exc("collector")
        except Exception:
            pass
    sys.exit(0 if _hook else _rc)
