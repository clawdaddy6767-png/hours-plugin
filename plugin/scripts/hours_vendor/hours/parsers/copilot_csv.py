from __future__ import annotations
import csv
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, Dict, Iterator, List, Optional, Sequence, Union
from ..model import Attachment, Session, Turn, parse_ts
from ._common import bump, set_text, stable_uuid, strip_data_uris
SOURCE = "copilot_consumer"
LAYOUTS = {
    "A": {"conversation": "conversation", "time": "time", "author": "author", "text": "message"},
    "B": {"conversation": "chatname", "time": "createdat", "author": "author", "text": "messagecontent"},
    "C": {"date": "date", "time": "time", "author": "type", "text": "content", "app": "source"},
    "D": {"time": "timestamp", "conversation": "clientapp", "text": "prompt"},
}
REQUIRED = {k: set(v.values()) - {"app"} for k, v in LAYOUTS.items()}
HUMAN = {"human", "user", "you", "prompt", "me"}
AI = {"ai", "copilot", "assistant", "bot", "response", "microsoft copilot"}
GAP = timedelta(hours=3)
FIELD_LIMIT = 64 * 1024 * 1024
TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%b %d, %Y %I:%M:%S %p", "%b %d, %Y, %I:%M:%S %p")
class UnknownLayout(ValueError):
    pass
@dataclass
class CsvBundle:
    sessions: List[Session] = field(default_factory=list)
    layout: Optional[str] = None
    rows: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    images_removed: int = 0
    warnings: List[str] = field(default_factory=list)
def _norm(h: str) -> str:
    return re.sub(r"[^a-z]", "", (h or "").lower())
def detect_layout(header: Sequence[str]) -> Optional[str]:
    cols = {_norm(h) for h in header}
    for name in ("B", "A", "C", "D"):
        if REQUIRED[name] <= cols:
            return name
    return None
def looks_like_copilot_csv(first_line: str) -> bool:
    try:
        header = next(csv.reader([first_line.lstrip("﻿")]))
    except (csv.Error, StopIteration):
        return False
    return detect_layout(header) is not None
def parse_time(value: str) -> Optional[datetime]:
    v = (value or "").strip().replace(" ", " ")
    if not v:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", v):
        d = parse_ts(v)
        if d is not None:
            return d
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
@dataclass
class _Row:
    ts: Optional[datetime]
    who: str
    text: str
    conv: str
    images: int = 0
    raw_author: str = ""
def _rows(fh: IO[str], bundle: CsvBundle) -> Iterator[_Row]:
    csv.field_size_limit(FIELD_LIMIT)
    reader = csv.reader(fh)
    try:
        header = next(reader)
    except StopIteration:
        raise UnknownLayout("empty file")
    if header:
        header[0] = header[0].lstrip("﻿")
    layout = detect_layout(header)
    if layout is None:
        raise UnknownLayout("header")
    bundle.layout = layout
    idx = {_norm(h): i for i, h in enumerate(header)}
    cols = LAYOUTS[layout]
    def col(row: List[str], key: str) -> str:
        name = cols.get(key)
        i = idx.get(name) if name else None
        return row[i] if i is not None and i < len(row) else ""
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        bundle.rows += 1
        text, n_img = strip_data_uris(col(row, "text"))
        bundle.images_removed += n_img
        if layout == "C":
            ts = parse_time((col(row, "date") + " " + col(row, "time")).strip())
        else:
            ts = parse_time(col(row, "time"))
        if layout == "D":
            who, raw = "user", "prompt"
        else:
            raw = col(row, "author").strip()
            low = raw.lower()
            who = "user" if low in HUMAN else ("assistant" if low in AI else "other")
        if ts is None:
            bump(bundle.skipped, "bad_time")
        yield _Row(ts, who, text, col(row, "conversation").strip() if layout != "C" else "", n_img, raw)
def rows_to_sessions(rows: List[_Row], layout: str, *, keep_text: bool = True) -> List[Session]:
    groups: Dict[str, List[_Row]] = {}
    for r in rows:
        groups.setdefault(r.conv, []).append(r)
    out: List[Session] = []
    for conv, rs in groups.items():
        rs = [r for _i, r in sorted(enumerate(rs), key=lambda x: (x[1].ts is None,
                                                                  x[1].ts.timestamp() if x[1].ts else 0.0, x[0]))]
        chunks: List[List[_Row]] = []
        for r in rs:
            if chunks and chunks[-1] and (r.ts is None or chunks[-1][-1].ts is None or r.ts - chunks[-1][-1].ts <= GAP):
                chunks[-1].append(r)
            else:
                chunks.append([r])
        for ch in chunks:
            first = ch[0]
            sid = stable_uuid("copilot_consumer", layout, conv, first.ts.isoformat() if first.ts else "",
                              first.text[:200])
            s = Session(id=sid, source=SOURCE)
            unknown_authors: Dict[str, int] = {}
            for i, r in enumerate(ch):
                if r.who == "user":
                    t = Turn(id="%s#%d" % (sid, i), role="user", kind="prompt", timestamp=r.ts)
                    set_text(t, r.text, keep_text=keep_text)
                elif r.who == "assistant":
                    t = Turn(id="%s#%d" % (sid, i), role="assistant", kind="response", timestamp=r.ts)
                    set_text(t, r.text, keep_text=keep_text)
                else:
                    t = Turn(id="%s#%d" % (sid, i), role="system", kind="meta", timestamp=r.ts)
                    t.char_count = len(r.text)
                    bump(unknown_authors, r.raw_author[:40])
                t.attachments = [Attachment(kind="image") for _ in range(r.images)]
                s.turns.append(t)
            stamps = [r.ts for r in ch if r.ts]
            s.started_at = min(stamps) if stamps else None
            s.ended_at = max(stamps) if stamps else None
            s.metadata.update({"partial_source": True, "layout": layout, "tz_unknown": True,
                               "grouping": "title_and_gap" if layout != "C" else "time_gap",
                               "prompt_only": layout == "D", "rows": len(ch)})
            if unknown_authors:
                s.metadata["unknown_authors"] = unknown_authors
            out.append(s)
    out.sort(key=lambda s: (s.started_at is None, s.started_at.timestamp() if s.started_at else 0.0))
    return out
def parse_copilot_csv_text(fh: IO[str], *, keep_text: bool = True) -> CsvBundle:
    bundle = CsvBundle()
    rows = list(_rows(fh, bundle))
    bundle.sessions = rows_to_sessions(rows, bundle.layout or "?", keep_text=keep_text)
    return bundle
def parse_copilot_csv(paths: Union[str, Sequence[str]], *, keep_text: bool = True) -> CsvBundle:
    if isinstance(paths, (str, os.PathLike)):
        paths = [str(paths)]
    total = CsvBundle()
    for p in paths:
        with open(str(p), "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            b = parse_copilot_csv_text(fh, keep_text=keep_text)
        total.sessions.extend(b.sessions)
        total.layout = total.layout or b.layout
        total.rows += b.rows
        total.images_removed += b.images_removed
        for k, v in b.skipped.items():
            bump(total.skipped, k, v)
    return total
def parse_copilot_csv_bytes(data: bytes, *, keep_text: bool = True) -> CsvBundle:
    return parse_copilot_csv_text(io.StringIO(data.decode("utf-8-sig", "replace"), newline=""), keep_text=keep_text)
__all__ = ["parse_copilot_csv", "parse_copilot_csv_text", "parse_copilot_csv_bytes", "detect_layout",
           "looks_like_copilot_csv", "parse_time", "UnknownLayout", "CsvBundle", "SOURCE"]
