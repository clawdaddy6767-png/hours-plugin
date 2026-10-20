from __future__ import annotations
import json
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import IO, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union
from ..model import Attachment, Session, Turn, parse_ts
from ._common import bump, html_to_text, set_text, stable_uuid
SOURCE = "gemini_app"
PROMPT_PREFIXES = ("Prompted ", "送信したメッセージ: ", "送信したメッセージ:", "Prompt: ")
SKIP_PREFIXES = ("Cleared ", "Created Gemini Canvas titled ", "Added chat from link: ", "Deleted ", "Used Gemini Apps",
                 "Visited ", "Shared ")
BRANCH_PREFIX = "Branched "
GAP = timedelta(minutes=30)
CONV_LINK_RE = re.compile(r"gemini\.google\.com/(?:u/\d+/)?app/(?:c/)?([A-Za-z0-9_-]{6,})")
_TZ = {"PST": -8, "PDT": -7, "MST": -7, "MDT": -6, "CST": -6, "CDT": -5, "EST": -5, "EDT": -4, "UTC": 0, "GMT": 0,
       "AKST": -9, "AKDT": -8, "HST": -10}
_DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2}\s?[AP]M)\s*([A-Z]{2,5})?")
@dataclass
class TakeoutBundle:
    sessions: List[Session] = field(default_factory=list)
    files_seen: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    records: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    gems_only: bool = False
def is_gemini_activity(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    base = parts[-1].lower()
    if base not in ("myactivity.json", "myactivity.html"):
        return False
    return any("gemini" in p.lower() for p in parts[:-1])
def is_gems_file(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return any(p.lower() == "gemini" for p in parts[:-1]) and "myactivity" not in parts[-1].lower()
@dataclass
class _Rec:
    ts: Optional[datetime]
    prompt: str
    reply: Optional[str]
    conv: Optional[str]
    attachments: int = 0
    branched: bool = False
    tz_unknown: bool = False
def _strip_prefix(title: str) -> Tuple[Optional[str], str]:
    for p in PROMPT_PREFIXES:
        if title.startswith(p):
            return "prompt", title[len(p):]
    if title.startswith(BRANCH_PREFIX):
        return "branched", title[len(BRANCH_PREFIX):]
    for p in SKIP_PREFIXES:
        if title.startswith(p):
            return None, ""
    return "unknown", title
def _conv_key(rec: Dict[str, Any]) -> Optional[str]:
    urls = [rec.get("titleUrl")] + [d.get("url") for d in rec.get("details") or [] if isinstance(d, dict)]
    for u in urls:
        if isinstance(u, str):
            m = CONV_LINK_RE.search(u)
            if m:
                return m.group(1)
    return None
def _maybe_json_text(v: Any) -> str:
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("{", "["):
            try:
                return _maybe_json_text(json.loads(s))
            except ValueError:
                return v
        return v
    if isinstance(v, dict):
        for k in ("text", "content", "prompt", "query", "response", "message"):
            if k in v:
                return _maybe_json_text(v[k])
        return ""
    if isinstance(v, list):
        return "\n".join(_maybe_json_text(x) for x in v)
    return ""
def record_from_json(rec: Dict[str, Any]) -> Tuple[Optional[_Rec], str]:
    header = str(rec.get("header") or "")
    products = rec.get("products") if isinstance(rec.get("products"), list) else []
    if "gemini" not in header.lower() and not any("gemini" in str(p).lower() for p in products):
        return None, "not_gemini"
    ts = parse_ts(rec.get("time"))
    atts = sum(len(rec.get(k) or []) for k in ("attachedFiles", "imageFile", "audioFiles")
               if isinstance(rec.get(k), list))
    reply = None
    items = rec.get("safeHtmlItem")
    if isinstance(items, list) and items:
        reply = "\n\n".join(html_to_text(x.get("html")) for x in items if isinstance(x, dict)) or None
    kind, prompt = _strip_prefix(str(rec.get("title") or ""))
    for d in rec.get("details") or []:
        if not isinstance(d, dict):
            continue
        nm = str(d.get("name") or "")
        val = d.get("value") if d.get("value") is not None else d.get("text")
        if nm.lower().startswith("request") and (kind != "prompt" or not prompt):
            prompt = _maybe_json_text(val) or nm.split(":", 1)[-1].strip()
            kind = "prompt"
        elif nm.lower().startswith("response") and reply is None:
            reply = _maybe_json_text(val) or None
    for ui in rec.get("userInteractions") or []:
        u = ui.get("userInteraction") if isinstance(ui, dict) and isinstance(ui.get("userInteraction"), dict) else ui
        if not isinstance(u, dict):
            continue
        if (kind != "prompt" or not prompt) and u.get("request") is not None:
            prompt, kind = _maybe_json_text(u["request"]), "prompt"
        if reply is None and u.get("response") is not None:
            reply = _maybe_json_text(u["response"]) or None
    if kind is None:
        return None, "non_chat"
    if kind == "unknown":
        if reply is None:
            return None, "non_chat"
        kind = "prompt"
    if not prompt.strip():
        return None, "no_prompt"
    return _Rec(ts, prompt, reply, _conv_key(rec), atts, branched=(kind == "branched")), "prompt"
class _CellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Dict[str, Any]] = []
        self.depth = 0
        self.cur: Optional[Dict[str, Any]] = None
        self.outer_depth: Optional[int] = None
        self.cell: Optional[str] = None
        self.cell_depth: Optional[int] = None
        self.buf: List[str] = []
    def handle_starttag(self, tag: str, attrs: Any) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "div":
            self.depth += 1
            if "outer-cell" in cls and self.cur is None:
                self.cur = {"header": "", "contents": [], "links": []}
                self.outer_depth = self.depth
            elif self.cur is not None and self.cell is None and ("header-cell" in cls or "content-cell" in cls):
                self.cell = "header" if "header-cell" in cls else "content"
                self.cell_depth = self.depth
                self.buf = []
        elif tag == "a" and self.cur is not None:
            href = dict(attrs).get("href")
            if href:
                self.cur["links"].append(href)
        if self.cell is not None and tag in ("br", "p"):
            self.buf.append("\n")
    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if self.cell is not None and tag == "br":
            self.buf.append("\n")
    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            if self.cell is not None and tag == "p":
                self.buf.append("\n")
            return
        if self.cell is not None and self.depth == self.cell_depth and self.cur is not None:
            text = "".join(self.buf).replace("\xa0", " ").strip()
            if self.cell == "header":
                self.cur["header"] = (self.cur["header"] + " " + text).strip()
            else:
                self.cur["contents"].append(text)
            self.cell = None
            self.cell_depth = None
        if self.cur is not None and self.depth == self.outer_depth:
            self.blocks.append(self.cur)
            self.cur = None
            self.outer_depth = None
        self.depth -= 1
    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.buf.append(data)
def _parse_html_time(s: str) -> Tuple[Optional[datetime], bool]:
    m = _DATE_RE.search(s)
    if not m:
        return None, True
    raw = re.sub(r"\s+", " ", m.group(1).replace(".", "").replace(" ", " ")).strip()
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%B %d, %Y, %I:%M:%S %p"):
        try:
            d = datetime.strptime(raw, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        return None, True
    tz = m.group(2)
    if tz in _TZ:
        return d.replace(tzinfo=timezone(timedelta(hours=_TZ[tz]))).astimezone(timezone.utc), False
    return d.replace(tzinfo=timezone.utc), True
def records_from_html(text: str) -> Iterator[Tuple[Optional[_Rec], str]]:
    p = _CellParser()
    p.feed(text)
    p.close()
    for b in p.blocks:
        header = b["header"]
        if "gemini" not in header.lower():
            yield None, "not_gemini"
            continue
        body = b["contents"][0] if b["contents"] else ""
        m = _DATE_RE.search(body)
        if not m:
            yield None, "no_time"
            continue
        title = body[:m.start()].strip()
        reply = body[m.end():].strip() or None
        ts, tz_unknown = _parse_html_time(body)
        kind, prompt = _strip_prefix(title)
        if kind is None:
            yield None, "non_chat"
            continue
        if kind == "unknown" and reply is None:
            yield None, "non_chat"
            continue
        if not prompt.strip():
            yield None, "no_prompt"
            continue
        conv = None
        for href in b["links"]:
            mm = CONV_LINK_RE.search(href)
            if mm:
                conv = mm.group(1)
                break
        yield _Rec(ts, prompt.strip(), reply, conv, branched=(kind == "branched"), tz_unknown=tz_unknown), "prompt"
def records_to_sessions(recs: List[_Rec], *, keep_text: bool = True) -> List[Session]:
    recs = sorted(recs, key=lambda r: (r.ts is None, r.ts.timestamp() if r.ts else 0.0))
    groups: List[Tuple[str, List[_Rec], bool]] = []
    by_conv: Dict[str, List[_Rec]] = {}
    loose: List[List[_Rec]] = []
    for r in recs:
        if r.conv:
            by_conv.setdefault(r.conv, []).append(r)
            continue
        if loose and loose[-1] and r.ts and loose[-1][-1].ts and r.ts - loose[-1][-1].ts <= GAP:
            loose[-1].append(r)
        else:
            loose.append([r])
    for conv, rs in by_conv.items():
        groups.append((stable_uuid("gemini_app", "conv", conv), rs, False))
    for rs in loose:
        first = rs[0]
        groups.append((stable_uuid("gemini_app", "gap", first.ts.isoformat() if first.ts else "", first.prompt[:200]),
                       rs, True))
    out = []
    for sid, rs, approx in groups:
        s = Session(id=sid, source=SOURCE)
        no_reply = 0
        tz_unknown = False
        for i, r in enumerate(rs):
            t = Turn(id="%s#u%d" % (sid, i), role="user", kind="prompt", timestamp=r.ts)
            set_text(t, r.prompt, keep_text=keep_text)
            t.attachments = [Attachment(kind="file") for _ in range(r.attachments)]
            if r.branched:
                t.extra["branched"] = True
            s.turns.append(t)
            tz_unknown = tz_unknown or r.tz_unknown
            if r.reply:
                a = Turn(id="%s#a%d" % (sid, i), role="assistant", kind="response", timestamp=r.ts)
                set_text(a, r.reply, keep_text=keep_text)
                s.turns.append(a)
            else:
                no_reply += 1
        stamps = [r.ts for r in rs if r.ts]
        s.started_at = min(stamps) if stamps else None
        s.ended_at = max(stamps) if stamps else None
        s.metadata.update({"partial_source": True, "grouping": "time_gap" if approx else "conversation_link",
                           "records": len(rs), "records_without_reply": no_reply,
                           "timestamps": "prompt_time_only", "tz_unknown": tz_unknown})
        out.append(s)
    out.sort(key=lambda s: (s.started_at is None, s.started_at.timestamp() if s.started_at else 0.0))
    return out
Opener = Callable[[], IO[bytes]]
def _members(path: str, warnings: List[str]) -> Iterator[Tuple[str, Opener]]:
    if os.path.isdir(path):
        for dirpath, _dirs, files in os.walk(path):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, path), (lambda p=full: open(p, "rb"))
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    yield name, (lambda z=zf, n=name: z.open(n))
        return
    if os.path.isfile(path) and tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            for m in tf.getmembers():
                if m.isfile():
                    yield m.name, (lambda t=tf, mm=m: t.extractfile(mm))
        return
    if os.path.isfile(path):
        ext = ".html" if path.lower().endswith((".html", ".htm")) else ".json"
        yield "Gemini Apps/MyActivity" + ext, (lambda p=path: open(p, "rb"))
        return
    warnings.append("input not found")
def parse_activity_file(name: str, data: bytes, bundle: TakeoutBundle) -> List[_Rec]:
    recs: List[_Rec] = []
    text = data.decode("utf-8-sig", "replace")
    if name.lower().endswith(".json"):
        try:
            arr = json.loads(text)
        except ValueError:
            bundle.warnings.append("MyActivity.json: invalid JSON")
            return recs
        items = arr if isinstance(arr, list) else (arr.get("items") if isinstance(arr, dict) else None) or []
        for rec in items:
            if not isinstance(rec, dict):
                continue
            bundle.records += 1
            r, why = record_from_json(rec)
            if r is None:
                bump(bundle.skipped, why)
            else:
                recs.append(r)
    else:
        for r, why in records_from_html(text):
            bundle.records += 1
            if r is None:
                bump(bundle.skipped, why)
            else:
                recs.append(r)
    return recs
def parse_takeout(paths: Union[str, Sequence[str]], *, keep_text: bool = True) -> TakeoutBundle:
    if isinstance(paths, (str, os.PathLike)):
        paths = [str(paths)]
    bundle = TakeoutBundle()
    found: Dict[str, Tuple[str, bytes]] = {}
    gems = False
    for p in paths:
        for name, opener in _members(str(p), bundle.warnings):
            bundle.files_seen.append(name)
            if is_gemini_activity(name):
                folder = os.path.dirname(name.replace("\\", "/"))
                prev = found.get(folder)
                if prev is None or (name.lower().endswith(".json") and not prev[0].lower().endswith(".json")):
                    fh = opener()
                    try:
                        found[folder] = (name, fh.read())
                    finally:
                        fh.close()
            elif is_gems_file(name):
                gems = True
    recs: List[_Rec] = []
    for _folder, (name, data) in sorted(found.items()):
        recs.extend(parse_activity_file(name, data, bundle))
    bundle.gems_only = gems and not found
    bundle.sessions = records_to_sessions(recs, keep_text=keep_text)
    return bundle
__all__ = ["parse_takeout", "parse_activity_file", "records_to_sessions", "record_from_json", "records_from_html",
           "is_gemini_activity", "is_gems_file", "TakeoutBundle", "SOURCE"]
