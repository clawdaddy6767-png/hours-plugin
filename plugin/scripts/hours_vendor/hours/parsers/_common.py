from __future__ import annotations
import html
import importlib
import io
import json
import os
import re
import uuid
from html.parser import HTMLParser
from typing import IO, Any, Dict, Iterator, List, Optional, Tuple
from ..model import ToolInfo, Turn, categorize_tool
HOURS_NS = uuid.UUID("6f1f5d0e-8a43-4c1e-9a52-2f0b5c8d7e31")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
UUID_IN_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DEFAULT_TOOL_RESULT_CHARS = 4000
class ZstdUnavailable(Exception):
    pass
def stable_uuid(*parts: Any) -> str:
    return str(uuid.uuid5(HOURS_NS, "\x1f".join(str(p) for p in parts)))
def as_uuid(value: Any, *fallback_parts: Any) -> str:
    s = str(value or "").strip()
    if UUID_RE.match(s):
        return s.lower()
    return stable_uuid(*(fallback_parts or (s,)))
_ZSTD: Dict[str, Any] = {}
def zstd_decoder() -> Optional[str]:
    if "name" in _ZSTD:
        return _ZSTD["name"]
    name = "pure"
    for mod in ("zstandard", "compression.zstd"):
        try:
            _ZSTD["mod"] = importlib.import_module(mod)
            name = mod
            break
        except Exception:
            continue
    _ZSTD["name"] = name
    return name
def zstd_available() -> bool:
    return zstd_decoder() is not None
def open_binary(path: str) -> IO[bytes]:
    if not path.endswith(".zst"):
        return open(path, "rb")
    dec = zstd_decoder()
    if dec is None:
        raise ZstdUnavailable(path)
    if dec == "pure":
        return io.BytesIO(_pure_decompress(path))
    mod = _ZSTD["mod"]
    if dec == "zstandard":
        fh = open(path, "rb")
        try:
            reader = mod.ZstdDecompressor().stream_reader(fh, closefd=True)
        except TypeError:
            reader = mod.ZstdDecompressor().stream_reader(fh)
        return io.BufferedReader(reader)
    return mod.open(path, "rb")
_PURE_LAST: Dict[str, Any] = {}
def _pure_decompress(path: str) -> bytes:
    from . import _zstd
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if _PURE_LAST.get("key") == key:
        return _PURE_LAST["data"]
    with open(path, "rb") as fh:
        data = _zstd.decompress(fh.read())
    _PURE_LAST.clear()
    _PURE_LAST.update(key=key, data=data)
    return data
def iter_jsonl(path: str, warnings: Optional[List[str]] = None, label: Optional[str] = None
               ) -> Iterator[Dict[str, Any]]:
    bad = 0
    cut = False
    with open_binary(path) as fh:
        lines = _lines(fh)
        while True:
            try:
                raw = next(lines)
            except StopIteration:
                break
            except Exception:
                cut = True
                break
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
    if warnings is not None:
        if bad:
            warnings.append("%s: %d malformed line(s) skipped" % (label or os.path.basename(path), bad))
        if cut:
            warnings.append("%s: file ended early; read up to the damage" % (label or os.path.basename(path)))
def _lines(fh: IO[bytes], chunk: int = 1 << 20) -> Iterator[bytes]:
    if hasattr(fh, "readline"):
        while True:
            line = fh.readline()
            if not line:
                return
            yield line
    buf = b""
    while True:
        data = fh.read(chunk)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf
def first_jsonl_record(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open_binary(path) as fh:
            for raw in _lines(fh):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    return None
                return rec if isinstance(rec, dict) else None
    except ZstdUnavailable:
        raise
    except OSError:
        return None
    return None
def set_text(turn: Turn, text: Optional[str], *, keep_text: bool = True, limit: Optional[int] = None) -> None:
    text = text or ""
    turn.char_count = len(text)
    if not keep_text:
        turn.text = None
    elif limit is not None and len(text) > limit:
        turn.text = text[:limit]
    else:
        turn.text = text
def set_command(turn: Turn, typed: str, *, keep_text: bool = True) -> None:
    s = (typed or "").strip()
    name, _sp, args = s.partition(" ")
    turn.extra["command"] = name
    set_text(turn, args.strip(), keep_text=keep_text)
    turn.char_count = len(s)
_DATA_URI_RE = re.compile(r"data:(image|audio|video|application)/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{16,}")
def strip_data_uris(text: str) -> Tuple[str, int]:
    n = 0
    def rep(m: "re.Match") -> str:
        nonlocal n
        n += 1
        return "[%s removed]" % m.group(1)
    return _DATA_URI_RE.sub(rep, text or ""), n
class _TextHTML(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
             "blockquote", "section", "article", "hr"}
    SKIP = {"script", "style", "head", "title", "noscript"}
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0
    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "img":
            self.out.append(" [image] ")
    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")
    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)
def html_to_text(fragment: Optional[str]) -> str:
    if not fragment:
        return ""
    fragment, _n = strip_data_uris(fragment)
    p = _TextHTML()
    try:
        p.feed(fragment)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html.unescape(fragment)).strip()
    text = "".join(p.out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
VENDOR_TOOL_CATEGORIES = {
    "shell": {"shell", "local_shell", "exec_command", "write_stdin", "run_shell_command", "run_command",
              "send_command_input", "command_status", "bash", "read_bash", "write_bash", "stop_bash", "powershell",
              "container.exec", "unified_exec"},
    "file_read": {"read_file", "read_many_files", "list_directory", "view_file", "list_dir", "view_code_item",
                  "view_file_outline", "view", "view_image", "ls", "find_by_name", "glob"},
    "file_edit": {"apply_patch", "write_file", "replace", "edit_file", "write_to_file", "replace_file_content",
                  "multi_replace_file_content", "code_action", "edit", "create", "str_replace_editor",
                  "canmore.create_textdoc", "canmore.update_textdoc", "canmore.comment_textdoc", "delete_file"},
    "search": {"grep_search", "search_file_content", "grep", "codebase_search", "search", "tool_search",
               "file_search", "msearch"},
    "web": {"web_search", "google_web_search", "web_fetch", "read_url_content", "search_web", "browser",
            "web.run", "web", "fetch", "open_browser_url", "browser_subagent", "read_browser_page"},
    "agent": {"task", "invoke_subagent", "manage_subagents", "spawn_agent", "send_input", "subagent"},
    "plan": {"update_plan", "write_todos", "update_todo", "todo_write", "plan"},
    "skill": {"skill", "activate_skill"},
}
_VENDOR_INDEX = {n: cat for cat, names in VENDOR_TOOL_CATEGORIES.items() for n in names}
def tool_category(name: Optional[str], mcp_server: Optional[str] = None) -> str:
    if mcp_server:
        return "mcp"
    n = (name or "").strip().lower()
    if not n:
        return "other"
    if n in _VENDOR_INDEX:
        return _VENDOR_INDEX[n]
    if n.startswith("mcp__") or n.startswith("mcp_"):
        return "mcp"
    cat = categorize_tool(name)
    return cat
def mcp_server_of(name: Optional[str]) -> Optional[str]:
    n = name or ""
    if n.startswith("mcp__"):
        parts = n.split("__")
        return parts[1] if len(parts) > 2 and parts[1] else None
    return None
def parse_args(args: Any) -> Any:
    if isinstance(args, str):
        s = args.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except ValueError:
                return args
    return args
_SUMMARY_KEYS = ("command", "cmd", "CommandLine", "commandLine", "command_line", "file_path", "filePath", "path",
                 "absolute_path", "AbsolutePath", "TargetFile", "targetFile", "DirectoryPath", "dir_path",
                 "pattern", "Pattern", "query", "Query", "url", "Url", "prompt", "description", "Description",
                 "skill", "name", "title", "plan", "explanation")
def input_summary(args: Any, limit: int = 300) -> Optional[str]:
    args = parse_args(args)
    if isinstance(args, str):
        return args[:limit] or None
    if isinstance(args, list):
        if args and all(isinstance(x, str) for x in args):
            return " ".join(args)[:limit]
        return ("items=%d" % len(args))[:limit]
    if not isinstance(args, dict):
        return None
    for k in _SUMMARY_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v[:limit]
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v)[:limit]
    return ("keys=" + ",".join(sorted(str(k) for k in args.keys())))[:limit]
BACKGROUND_KEYS = ("run_in_background", "is_background", "background")
def background_requested(args: Any) -> bool:
    a = parse_args(args)
    return isinstance(a, dict) and any(a.get(k) is True for k in BACKGROUND_KEYS)
def make_tool(name: Optional[str], *, call_id: Optional[str] = None, args: Any = None,
              mcp_server: Optional[str] = None) -> ToolInfo:
    nm = str(name or "unknown")
    server = mcp_server or mcp_server_of(nm)
    return ToolInfo(name=nm, id=call_id, input_summary=input_summary(args) if args is not None else None,
                    mcp_server=server, category=tool_category(nm, server))
def json_size(obj: Any) -> int:
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj))
    except (TypeError, ValueError):
        return 0
def encode_project_dir(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
def bump(counter: Dict[str, int], key: Any, n: int = 1) -> None:
    k = str(key)
    counter[k] = counter.get(k, 0) + n
