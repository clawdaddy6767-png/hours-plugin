from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from hours.model import Session, Turn
from . import detectors
from .privacy import PLACEHOLDER_RE, Scrubber, cut_head, cut_tail, seeded_scrubber
RENDER_VERSION = "r1.3"
REMOVED_LINE = "[content removed]"
CHARS_PER_TOKEN = 3.5
TARGET_TOKENS = 20_000
MAX_TOKENS = 40_000
WINDOW_MAX_TOKENS = 40_000
IDLE_SPLIT = timedelta(minutes=30)
VERIFY_RE = detectors.VERIFY_RE
RISKY_RE = re.compile(r"(rm -rf|git push (-f|--force)|git reset --hard|\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\b|"
                      r"terraform destroy|--dangerously|chmod -R 777)", re.I)
try:
    from zoneinfo import ZoneInfo
    PACIFIC: Any = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC = timezone(timedelta(hours=-8))
@dataclass
class RTurn:
    id: str
    label: str
    text: str
    human_index: int
    ts: Optional[str] = None
    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label, "text": self.text, "human_index": self.human_index,
                "ts": self.ts}
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RTurn":
        return cls(d["id"], d["label"], d["text"], int(d.get("human_index") or 0), d.get("ts"))
@dataclass
class Episode:
    index: int
    turns: List[Turn]
    human_count: int = 0
@dataclass
class Unit:
    unit_id: str
    session_id: str
    surface: str
    episode_index: int
    part: int
    rturns: List[RTurn]
    level: str
    facts: Dict[str, object]
    elided: Dict[str, int] = field(default_factory=dict)
    kind: str = "segment"
    metrics: Dict[str, Any] = field(default_factory=dict)
    started: Optional[str] = None
    ended: Optional[str] = None
    project_key: Optional[str] = None
    render_version: str = RENDER_VERSION
    @property
    def text(self) -> str:
        return "\n".join(f"[{r.id} {r.label}] {r.text}" for r in self.rturns)
    @property
    def est_tokens(self) -> int:
        return int(len(self.text) / CHARS_PER_TOKEN)
    def turn_text(self) -> Dict[str, str]:
        return {r.id: r.text for r in self.rturns}
    @property
    def human_turns(self) -> int:
        return sum(1 for r in self.rturns if r.label == "USER")
    @property
    def ref(self) -> str:
        sid = self.session_id[:8]
        if self.kind == "window":
            return f"S{sid}:W{self.episode_index + 1}"
        return f"S{sid}:U{self.episode_index + 1}" + (f"p{self.part + 1}" if self.part else "")
    def content_hash(self) -> str:
        return hashlib.sha256((self.render_version + "\n" + self.text).encode("utf-8")).hexdigest()[:16]
    def to_dict(self) -> Dict[str, Any]:
        return {"unit_id": self.unit_id, "session_id": self.session_id, "surface": self.surface,
                "episode_index": self.episode_index, "part": self.part, "kind": self.kind, "level": self.level,
                "rturns": [r.to_dict() for r in self.rturns], "facts": self.facts, "elided": self.elided,
                "metrics": self.metrics, "started": self.started, "ended": self.ended,
                "project_key": self.project_key, "render_version": self.render_version}
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Unit":
        return cls(unit_id=d["unit_id"], session_id=d["session_id"], surface=d.get("surface") or "claude_code",
                   episode_index=int(d.get("episode_index") or 0), part=int(d.get("part") or 0),
                   rturns=[RTurn.from_dict(r) for r in d.get("rturns") or []], level=d.get("level") or "L0",
                   facts=d.get("facts") or {}, elided=d.get("elided") or {}, kind=d.get("kind") or "segment",
                   metrics=d.get("metrics") or {}, started=d.get("started"), ended=d.get("ended"),
                   project_key=d.get("project_key"), render_version=d.get("render_version") or "unknown")
def _is_clear(t: Turn) -> bool:
    return t.kind == "command" and (t.extra.get("command") or t.text or "").strip().startswith("/clear")
def split_episodes(session: Session) -> List[Episode]:
    main = [t for t in session.turns if not t.is_sidechain and t.on_main_branch]
    episodes: List[Episode] = []
    cur: List[Turn] = []
    last_ts = None
    saw_boundary = False
    for t in main:
        if t.kind == "compaction" or _is_clear(t):
            saw_boundary = True
        if t.is_human_input:
            gap = (t.timestamp - last_ts) if (t.timestamp and last_ts) else timedelta(0)
            if cur and any(x.is_human_input for x in cur) and (saw_boundary or gap > IDLE_SPLIT):
                episodes.append(Episode(len(episodes), cur))
                cur = []
            saw_boundary = False
        cur.append(t)
        if t.timestamp:
            last_ts = t.timestamp
    if cur:
        episodes.append(Episode(len(episodes), cur))
    for e in episodes:
        e.human_count = sum(1 for t in e.turns if t.is_human_input)
    merged: List[Episode] = []
    for e in episodes:
        if merged and e.human_count < 2:
            merged[-1].turns.extend(e.turns)
            merged[-1].human_count += e.human_count
        else:
            merged.append(e)
    for i, e in enumerate(merged):
        e.index = i
    return merged
CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
MD_CHARS = re.compile(r"[#*_`>|]+")
FINAL_PASSES = 5
PROSE_LABELS = ("USER", "A")
ATTACHMENT_KINDS = ("file", "image", "pasted_text", "voice")
def final_scrub(text: Optional[str], scrubber: Scrubber, prose: bool = False) -> Optional[str]:
    if not text:
        return text
    for _ in range(FINAL_PASSES):
        new = scrubber.scrub_safe(text) or ""
        if new == text:
            break
        text = new
    if prose:
        text = scrubber.scrub_people(text) or ""
    return text
def _final_tree(obj: Any, scrubber: Scrubber) -> Any:
    if isinstance(obj, str):
        return final_scrub(obj, scrubber)
    if isinstance(obj, list):
        return [_final_tree(x, scrubber) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_final_tree(x, scrubber) for x in obj)
    if isinstance(obj, dict):
        return {(final_scrub(k, scrubber) if isinstance(k, str) else k): _final_tree(v, scrubber)
                for k, v in obj.items()}
    return obj
def _strip_md(s: str) -> str:
    s = CODE_BLOCK.sub(lambda m: f"[code: {m.group(1) or 'text'}, {m.group(2).count(chr(10)) + 1} lines]", s)
    parts: List[str] = []
    last = 0
    for m in PLACEHOLDER_RE.finditer(s):
        parts.append(MD_CHARS.sub(" ", s[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(MD_CHARS.sub(" ", s[last:]))
    return re.sub(r"\s+", " ", "".join(parts)).strip()
def gist(text: Optional[str], level: str, final: bool = False) -> str:
    if not text:
        return ""
    s = _strip_md(text)
    sents = [x for x in SENT_SPLIT.split(s) if x]
    if not sents:
        return ""
    questions = [x for x in sents if x.endswith("?")]
    if level == "L0":
        keep = sents[:2] + [q for q in questions if q not in sents[:2]][:2]
        if len(sents) > 2 and sents[-1] not in keep:
            keep.append(sents[-1])
        cap = 900 if final else 400
    else:
        keep = sents[:1] + [q for q in questions if q != sents[0]][:1]
        cap = 300 if final else 160
    out = " ".join(keep)
    return out if len(out) <= cap else cut_head(out, cap) + "…"
def human_text(text: str, level: str) -> str:
    limit = 5200 if level == "L0" else 2100
    if len(text) <= limit:
        return text
    head, tail = (3500, 700) if level == "L0" else (1400, 350)
    return (f"{cut_head(text, head)} [… elided ~{int((len(text) - head - tail) / CHARS_PER_TOKEN)} tok of pasted "
            f"content …] {cut_tail(text, tail)}")
def _first_line(s: Optional[str], n: Optional[int] = 140) -> str:
    if not s:
        return ""
    for line in s.splitlines():
        line = line.strip()
        if line:
            return line[:n] if n else line
    return ""
def _subagent_summary(sub: Session, scrubber: Scrubber) -> str:
    finals = [t for t in sub.turns if t.role == "assistant" and t.kind == "response" and t.text]
    ret = gist(scrubber.scrub_prose(finals[-1].text), "L0", final=True) if finals else ""
    agent_type = sub.metadata.get("agent_type") or sub.metadata.get("agentType") or "subagent"
    errored = any(t.tool and t.tool.is_error for t in sub.iter_turns(recursive=True))
    return f"{agent_type}{', some tool errors' if errored else ''}; returned: \"{cut_head(ret or '', 600)}\""
def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None
def render_session(session: Session, scrubber: Scrubber, level: str = "L0", id_map: Optional[Dict[str, str]] = None,
                   secret_by_turn: Optional[Dict[str, Dict[str, int]]] = None) -> List[RTurn]:
    subs = {s.agent_id: s for s in session.subagents if s.agent_id}
    results: Dict[str, Turn] = {}
    for t in session.turns:
        if t.kind == "tool_result" and t.tool and t.tool.id:
            results[t.tool.id] = t
    ids: Dict[str, str] = id_map if id_map is not None else {}
    secrets: Dict[str, Dict[str, int]] = secret_by_turn if secret_by_turn is not None else {}
    call_rid: Dict[str, str] = {}
    out: List[RTurn] = []
    u = 0
    tool_k = 0
    e = 0
    last_model = None
    pending_assistant: List[str] = []
    pending_ids: List[str] = []
    pending_ts: Optional[str] = None
    agent_batch: Optional[Tuple[str, RTurn]] = None
    def flush():
        nonlocal pending_assistant, pending_ids, pending_ts
        if pending_assistant:
            out.append(RTurn(f"A{u}", "A", " ".join(pending_assistant), u, pending_ts))
        pending_assistant, pending_ids, pending_ts = [], [], None
    def note_secrets(rid: str, before: Dict[str, int]) -> None:
        after = scrubber.snapshot()
        delta = {k: after[k] - before.get(k, 0) for k in after if after[k] - before.get(k, 0) > 0}
        if delta:
            agg = secrets.setdefault(rid, {})
            for k, v in delta.items():
                agg[k] = agg.get(k, 0) + v
    for t in session.turns:
        if t.is_sidechain or not t.on_main_branch:
            continue
        if t.is_human_input:
            flush()
            agent_batch = None
            u += 1
            tool_k = 0
            rid = f"U{u}"
            before = scrubber.snapshot()
            if t.kind == "command":
                name = (t.extra.get("command") or "").strip()
                args = scrubber.scrub_prose((t.text or "").strip()) or ""
                body = (name + (" " + args if args else "")).strip() or args
            elif t.kind == "shell":
                body = "! " + (scrubber.scrub((t.text or "").strip()) or "")
            else:
                body = human_text(scrubber.scrub_prose(t.text or "") or "", level)
            note_secrets(rid, before)
            if t.extra.get("queued"):
                body = "(queued) " + body
            for a in t.attachments:
                body += f" [attachment removed: {a.kind if a.kind in ATTACHMENT_KINDS else 'file'}]"
            out.append(RTurn(rid, "USER", body, u, _iso(t.timestamp)))
            ids[t.id] = rid
            continue
        if t.role == "assistant":
            if t.model and last_model and t.model != last_model and t.model != "<synthetic>":
                flush()
                agent_batch = None
                e += 1
                out.append(RTurn(f"E{e}", "EVENT", f"model changed {last_model} → {t.model}", u, _iso(t.timestamp)))
            if t.model and t.model != "<synthetic>":
                last_model = t.model
            if t.kind == "response":
                before = scrubber.snapshot()
                g = gist(scrubber.scrub_prose(t.text), level)
                note_secrets(f"A{u}", before)
                if g:
                    pending_assistant.append(g)
                    pending_ts = pending_ts or _iso(t.timestamp)
                ids[t.id] = f"A{u}"
            elif t.kind == "tool_call" and t.tool:
                is_agent = (t.tool.category == "agent" and t.tool.name.lower() in ("agent", "task"))
                key = t.request_id or t.message_id
                if is_agent and agent_batch is not None and key and agent_batch[0] == key:
                    before = scrubber.snapshot()
                    agent_batch[1].text += "\n" + _agent_brief(t, results, subs, scrubber, level)
                    note_secrets(agent_batch[1].id, before)
                    ids[t.id] = agent_batch[1].id
                    if t.tool.id:
                        call_rid[t.tool.id] = agent_batch[1].id
                    continue
                flush()
                tool_k += 1
                rid = f"A{u}.t{tool_k}"
                before = scrubber.snapshot()
                if is_agent:
                    line = RTurn(rid, "TOOL", "Agent launch (subagents started together are listed together):\n"
                                 + _agent_brief(t, results, subs, scrubber, level), u, _iso(t.timestamp))
                    agent_batch = (key or rid, line)
                else:
                    line = RTurn(rid, "TOOL", _tool_stub(t, results, scrubber, level), u, _iso(t.timestamp))
                    agent_batch = None
                note_secrets(rid, before)
                out.append(line)
                ids[t.id] = rid
                if t.tool.id:
                    call_rid[t.tool.id] = rid
            continue
        if t.kind == "tool_result" and t.tool and t.tool.id and t.tool.id in call_rid:
            ids[t.id] = call_rid[t.tool.id]
            continue
        if t.kind == "interrupt":
            flush()
            agent_batch = None
            e += 1
            out.append(RTurn(f"E{e}", "EVENT", "user interrupted the assistant", u, _iso(t.timestamp)))
            ids[t.id] = f"E{e}"
        elif t.kind == "compaction" and t.role == "system" and t.extra.get("boundary") is not False:
            flush()
            agent_batch = None
            e += 1
            trig = t.extra.get("trigger") or ""
            pre = t.extra.get("pre_tokens")
            out.append(RTurn(f"E{e}", "EVENT", f"context compaction {trig} preTokens={pre}", u, _iso(t.timestamp)))
            ids[t.id] = f"E{e}"
        elif t.kind == "mode_change":
            flush()
            agent_batch = None
            e += 1
            what = t.extra.get("what") or "mode"
            if what == "permission_mode":
                txt = f"permission mode {t.extra.get('from')} → {t.extra.get('to') or t.permission_mode}"
            else:
                txt = f"mode event: {what}"
            out.append(RTurn(f"E{e}", "EVENT", txt, u, _iso(t.timestamp)))
            ids[t.id] = f"E{e}"
        elif t.kind == "removed":
            flush()
            agent_batch = None
            e += 1
            out.append(RTurn(f"E{e}", "EVENT", REMOVED_LINE, u, None))
            ids[t.id] = f"E{e}"
    flush()
    for r in out:
        before = scrubber.snapshot()
        r.text = final_scrub(r.text, scrubber, prose=r.label in PROSE_LABELS) or ""
        note_secrets(r.id, before)
    return out
def _agent_brief(t: Turn, results: Dict[str, Turn], subs: Dict[str, Session], scrubber: Scrubber, level: str) -> str:
    tool = t.tool
    arg = scrubber.scrub(tool.input_summary or "") or ""
    res = results.get(tool.id or "")
    status = ""
    if res and res.tool:
        status = " → REJECTED" if res.tool.is_rejected else " → interrupted" if res.tool.is_interrupted else (
            " → error" if res.tool.is_error else " → ok")
    prompt = scrubber.scrub_prose(t.text or "") or ""
    cap = 3000 if level == "L0" else 1400
    if len(prompt) > cap:
        prompt = cut_head(prompt, cap - 300) + " […] " + cut_tail(prompt, 250)
    s = f"  - {tool.name} {arg}{status}\n    brief: \"{prompt}\""
    sub = subs.get(tool.spawned_agent_id or "")
    if sub:
        s += f"\n    [SUBAGENT {_subagent_summary(sub, scrubber)}]"
    return s
def _tool_stub(t: Turn, results: Dict[str, Turn], scrubber: Scrubber, level: str) -> str:
    tool = t.tool
    name = tool.name
    arg = scrubber.scrub(tool.input_summary or "") or ""
    res = results.get(tool.id or "")
    status = ""
    if res and res.tool:
        if res.tool.is_rejected:
            status = f" → REJECTED ({res.tool.denial_kind or 'user'})"
        elif res.tool.is_interrupted:
            status = " → interrupted"
        elif res.tool.is_error:
            status = f" → error: \"{cut_head(scrubber.scrub(_first_line(res.text, None)) or '', 120)}\""
        else:
            status = " → ok"
    cat = tool.category or "other"
    if cat == "plan" and t.text:
        plan = scrubber.scrub_prose(t.text) or ""
        cap = 1100 if level == "L0" else 400
        return f"{name}{status}\n  plan: \"{cut_head(plan, cap)}\""
    if cat == "shell":
        verify = " [verification command]" if VERIFY_RE.search(arg) else ""
        risky = " [high-impact command]" if (RISKY_RE.search(arg) or t.extra.get("sandbox_disabled")) else ""
        return f"{name} {cut_head(arg, 220)}{status}{verify}{risky}"
    return f"{name} {cut_head(arg, 160)}{status}"
def _facts(session: Session, rturns: List[RTurn], turns: List[Turn], surface: str, level: str,
           scrubber: Scrubber) -> Dict[str, object]:
    models: Dict[str, int] = {}
    efforts: Dict[str, int] = {}
    modes = set()
    for t in turns:
        if t.role == "assistant" and t.kind == "response" and t.model and t.model != "<synthetic>":
            models[t.model] = models.get(t.model, 0) + 1
        if t.effort:
            efforts[t.effort] = efforts.get(t.effort, 0) + 1
        if t.permission_mode:
            modes.add(t.permission_mode)
    total = sum(models.values()) or 1
    tool_stubs = [r for r in rturns if r.label == "TOOL"]
    ts = [t.timestamp for t in turns if t.timestamp]
    active = 0.0
    for a, b in zip(ts, ts[1:]):
        gap = (b - a).total_seconds()
        if 0 < gap <= 600:
            active += gap
    facts: Dict[str, object] = {
        "surface": surface,
        "human_turns": sum(1 for r in rturns if r.label == "USER"),
        "active_minutes": round(active / 60),
        "tool_calls": len(tool_stubs),
        "subagent_spawns": sum(1 for t in turns if t.tool and t.tool.category == "agent"),
        "verification_commands": sum(1 for r in tool_stubs if "[verification command]" in r.text),
        "high_impact_commands": sum(1 for r in tool_stubs if "[high-impact command]" in r.text),
        "plan_mode_or_todo": sum(1 for t in turns if t.tool and t.tool.category == "plan"),
        "skills_used": sorted({t.tool.input_summary or "" for t in turns if t.tool and t.tool.category == "skill"}),
        "mcp_calls": sum(1 for t in turns if t.tool and t.tool.category == "mcp"),
        "web_calls": sum(1 for t in turns if t.tool and t.tool.category == "web"),
        "compactions": sum(1 for t in turns if t.kind == "compaction" and t.role == "system" and t.extra.get("boundary") is not False),
        "slash_commands": [r.text.split()[0] for r in rturns if r.label == "USER" and r.text.startswith("/")],
        "models_share": {m: round(c / total, 2) for m, c in sorted(models.items(), key=lambda x: -x[1])},
        "effort_levels": efforts,
        "permission_modes": sorted(modes),
        "interrupts": sum(1 for r in rturns if r.label == "EVENT" and "interrupted" in r.text),
        "secrets_redacted": 0,
        "compression": level,
    }
    facts = _final_tree(facts, scrubber)
    facts["secrets_redacted"] = scrubber.secret_count
    return facts
def project_key(session: Session) -> Optional[str]:
    base = session.cwd or session.project or session.metadata.get("encoded_project_dir")
    return hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:10] if base else None
class _Rendered:
    def __init__(self, session: Session):
        self.session = session
        self.id_map: Dict[str, str] = {}
        self.secret_by_turn: Dict[str, Dict[str, int]] = {}
        self.levels: Dict[str, Tuple[List[RTurn], Scrubber]] = {}
        for level in ("L0", "L1"):
            sc = Scrubber()
            if level == "L0":
                rt = render_session(session, sc, level, self.id_map, self.secret_by_turn)
            else:
                rt = render_session(session, sc, level)
            self.levels[level] = (rt, sc)
        self.main = [t for t in session.turns if not t.is_sidechain and t.on_main_branch]
        humans = [t for t in self.main if t.is_human_input]
        self.first_human = humans[0].id if humans else None
        self.last_human = humans[-1].id if humans else None
        self.scrub = self.levels["L0"][1].scrub_prose
    def raw_for(self, rids: set) -> List[Turn]:
        return [t for t in self.main if self.id_map.get(t.id) in rids]
    def metrics(self, rturns: List[RTurn], raw: List[Turn]) -> Dict[str, Any]:
        ids = {t.id for t in raw}
        m = detectors.compute(raw, self.id_map, self.session, self.scrub, self.secret_by_turn,
                              is_session_start=self.first_human in ids, is_session_end=self.last_human in ids)
        return _final_tree(m, self.levels["L0"][1])
def build_units(session: Session, surface: str = "claude_code") -> List[Unit]:
    rd = _Rendered(session)
    return _build_segments(rd, surface)
def _build_segments(rd: _Rendered, surface: str) -> List[Unit]:
    session = rd.session
    episodes = split_episodes(session)
    units: List[Unit] = []
    ep_of_u: Dict[int, int] = {}
    u = 0
    for ep in episodes:
        for t in ep.turns:
            if t.is_human_input:
                u += 1
                ep_of_u[u] = ep.index
    pkey = project_key(session)
    for ep in episodes:
        if ep.human_count == 0:
            continue
        chosen = None
        for level in ("L0", "L1"):
            rt_all, scrub = rd.levels[level]
            rts = [r for r in rt_all if ep_of_u.get(r.human_index, 0) == ep.index and r.text != REMOVED_LINE]
            est = sum(len(r.text) + len(r.id) + 10 for r in rts) / CHARS_PER_TOKEN
            chosen = (level, rts, scrub)
            if est <= TARGET_TOKENS:
                break
        level, rts, scrub = chosen
        windows = _window(rts, MAX_TOKENS)
        for part, w in enumerate(windows):
            raw = rd.raw_for({r.id for r in w})
            facts = _facts(session, w, ep.turns, surface, level, scrub)
            if len(windows) > 1:
                facts["window"] = f"part {part + 1} of {len(windows)} of a long episode"
            stamps = [t.timestamp for t in raw if t.timestamp] or [t.timestamp for t in ep.turns if t.timestamp]
            unit = Unit(f"{session.id[:8]}-e{ep.index}-p{part}", session.id, surface, ep.index, part, w, level, facts,
                        kind="segment", metrics=rd.metrics(w, raw),
                        started=_iso(min(stamps)) if stamps else _iso(session.started_at),
                        ended=_iso(max(stamps)) if stamps else _iso(session.ended_at), project_key=pkey)
            units.append(unit)
    return units
def _window(rts: List[RTurn], max_tokens: int) -> List[List[RTurn]]:
    out: List[List[RTurn]] = []
    cur: List[RTurn] = []
    size = 0.0
    for r in rts:
        s = (len(r.text) + len(r.id) + 10) / CHARS_PER_TOKEN
        if cur and r.label == "USER" and size + s > max_tokens * 0.75:
            out.append(cur)
            cur, size = [], 0.0
        elif cur and r.label == "TOOL" and size + s > max_tokens:
            out.append(cur)
            cur, size = [], 0.0
        cur.append(r)
        size += s
    if cur:
        out.append(cur)
    return out
def _pacific_day(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(PACIFIC).date().isoformat()
def split_windows(session: Session) -> List[List[Turn]]:
    main = [t for t in session.turns if not t.is_sidechain and t.on_main_branch]
    wins: List[List[Turn]] = []
    cur: List[Turn] = []
    day = None
    for t in main:
        boundary = False
        if _is_clear(t) or (t.kind == "compaction" and t.role == "system" and t.extra.get("boundary") is not False):
            boundary = True
        if t.is_human_input and t.timestamp is not None:
            d = _pacific_day(t.timestamp)
            if day is not None and d != day:
                boundary = True
            day = d
        if boundary and any(x.is_human_input for x in cur):
            wins.append(cur)
            cur = []
        cur.append(t)
    if cur:
        if wins and not any(x.is_human_input for x in cur):
            wins[-1].extend(cur)
        else:
            wins.append(cur)
    return [w for w in wins if any(x.is_human_input for x in w)]
def _seeded_scrubber(rturns: List[RTurn]) -> Scrubber:
    return seeded_scrubber(r.text for r in rturns)
def digest(rturns: List[RTurn], max_tokens: int = WINDOW_MAX_TOKENS,
           scrubber: Optional[Scrubber] = None) -> Tuple[List[RTurn], str]:
    trimmed, mode = _trim(rturns, max_tokens)
    sc = scrubber if scrubber is not None else _seeded_scrubber(rturns)
    out: List[RTurn] = []
    for r in trimmed:
        text = final_scrub(r.text, sc, prose=r.label in PROSE_LABELS) or ""
        out.append(r if text == r.text else RTurn(r.id, r.label, text, r.human_index, r.ts))
    return out, mode
def _trim(rturns: List[RTurn], max_tokens: int) -> Tuple[List[RTurn], str]:
    def size(rs: List[RTurn]) -> float:
        return sum(len(r.text) + len(r.id) + 10 for r in rs) / CHARS_PER_TOKEN
    if size(rturns) <= max_tokens:
        return rturns, "full"
    tagged = ("[verification command]", "[high-impact command]", "Agent launch", "plan:", "REJECTED")
    t2 = []
    for r in rturns:
        if r.label in ("USER", "EVENT") or (r.label == "TOOL" and any(k in r.text for k in tagged)):
            t2.append(r)
        elif r.label == "A":
            t2.append(RTurn(r.id, r.label, cut_head(r.text, 160), r.human_index, r.ts))
    if size(t2) <= max_tokens:
        return t2, "trimmed"
    t3 = []
    for r in t2:
        if r.label == "USER":
            t3.append(RTurn(r.id, r.label, r.text if len(r.text) <= 600 else cut_head(r.text, 600) + " […]",
                            r.human_index, r.ts))
        elif r.label in ("EVENT", "TOOL"):
            t3.append(RTurn(r.id, r.label, cut_head(r.text, 300), r.human_index, r.ts))
    if size(t3) <= max_tokens:
        return t3, "user_and_events"
    keep: List[RTurn] = []
    total = 0.0
    for r in reversed(t3):
        s = (len(r.text) + len(r.id) + 10) / CHARS_PER_TOKEN
        if total + s > max_tokens - 200:
            break
        keep.append(r)
        total += s
    keep.reverse()
    dropped = [r for r in t3 if r not in keep]
    n_users = sum(1 for r in dropped if r.label == "USER")
    first = dropped[0].id if dropped else ""
    last = dropped[-1].id if dropped else ""
    head = RTurn("E0", "EVENT", f"[earlier part of this window summarized: {first} to {last}, {n_users} user turns "
                                f"not shown]", 0, None)
    return [head] + keep, "recent_only"
def build_windows(session: Session, surface: str = "claude_code", rendered: Optional[_Rendered] = None) -> List[Unit]:
    rd = rendered or _Rendered(session)
    rt_all, l1_scrubber = rd.levels["L1"]
    pkey = project_key(session)
    out: List[Unit] = []
    for k, wturns in enumerate(split_windows(session)):
        rids = {rd.id_map.get(t.id) for t in wturns if rd.id_map.get(t.id)}
        rts = [r for r in rt_all if r.id in rids]
        if not rts:
            continue
        dig, mode = digest(rts, scrubber=l1_scrubber)
        stamps = [t.timestamp for t in wturns if t.timestamp]
        facts = {"surface": surface, "digest": mode}
        out.append(Unit(f"{session.id[:8]}-w{k}", session.id, surface, k, 0, dig, "L1", facts, kind="window",
                        metrics=rd.metrics(dig, wturns), started=_iso(min(stamps)) if stamps else None,
                        ended=_iso(max(stamps)) if stamps else None, project_key=pkey))
    return out
SURFACES = ("claude_code", "claude_ai", "chatgpt", "codex", "gemini_app", "gemini_cli", "antigravity",
            "copilot_consumer", "copilot_cli", "cursor", "opencode", "pi", "hermes", "openclaw",
            "nanoclaw", "aider", "cline", "roo_code", "continue_dev", "goose", "qwen_code", "zed",
            "claude_cowork")
def build_all(session: Session, surface: Optional[str] = None) -> List[Unit]:
    surface = surface or (session.source if session.source in SURFACES else "claude_code")
    rd = _Rendered(session)
    units = _build_segments(rd, surface) + build_windows(session, surface, rd)
    grouping = (session.metadata or {}).get("grouping")
    if isinstance(grouping, str) and grouping:
        for u in units:
            u.facts["grouping"] = "approximate" if grouping in APPROXIMATE_GROUPING else grouping
    return units
APPROXIMATE_GROUPING = ("time_gap",)
