from __future__ import annotations
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$")
@dataclass(frozen=True)
class Stamp:
    t: float
    frac_digits: int
    zone: str
    ms: int
def parse_ts(v: Any) -> Optional[Stamp]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if not math.isfinite(x) or x <= 0:
            return None
        if x > 1e11:
            x /= 1000.0
        frac = x - math.floor(x)
        return Stamp(x, 3 if frac else 0, "epoch", int(round(frac * 1000)) % 1000 if frac else -1)
    if not isinstance(v, str) or len(v) > 40:
        return None
    m = _TS_RE.match(v.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, zone = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss), tzinfo=timezone.utc)
    except ValueError:
        return None
    t = dt.timestamp()
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        z = zone[1:].replace(":", "")
        t -= sign * (int(z[:2]) * 3600 + int(z[2:]) * 60)
    ms = -1
    if frac:
        t += float("0." + frac)
        ms = int((frac + "000")[:3])
    return Stamp(t, len(frac or ""), "Z" if zone == "Z" else ("offset" if zone else "none"), ms)
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
def short_hash(obj: Any, n: int = 16) -> str:
    data = obj.encode("utf-8", "surrogatepass") if isinstance(obj, str) else canonical(obj)
    return hashlib.sha256(data).hexdigest()[:n]
def text_hash(text: Any, n: int = 12) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split()).lower()
    if not t:
        return None
    return hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest()[:n]
