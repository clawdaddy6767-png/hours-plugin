from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from .privacy import PLACEHOLDER_RE
from .render import REMOVED_LINE
REMOVED_TEXT = REMOVED_LINE
REMOVED_KIND = "removed"
PROSE_LABELS = ("USER", "A")
_HEALTH_CI = r"|".join([
    r"\bdiagnos(?:ed|is)[ \t]+(?:with|of)\b",
    r"\b(?:my|your|his|her|their|our)[ \t]+(?:new[ \t]+)?(?:diagnosis|doctor|physician|gp|therapist|psychiatrist|"
    r"psychologist|counsel(?:l)?or|oncologist|cardiologist|neurologist|dermatologist|gynecologist|ob-?gyn|urologist|"
    r"surgeon|prescriptions?|medications?|meds|symptoms|illness|disease|disorder|surgery|biopsy|"
    r"lab[ \t]+results|blood[ \t]*work|mri|ct[ \t]+scan|x-ray|chemo|therapy|relapse|"
    r"disability|insulin|inhaler|pregnancy|miscarriage|abortion|ivf|fertility|ovulation)\b|\bmenstrua\w*",
    r"\b(?:cancer|tumou?r|chemotherapy|radiation[ \t]+therapy|leukemia|lymphoma|melanoma|diabetes|diabetic|"
    r"hypertension|epilep(?:sy|tic)|seizures?|had[ \t]+a[ \t]+stroke|heart[ \t]+attack|arrhythmia|hepatitis|herpes|"
    r"chlamydia|gonorrh(?:o)?ea|syphilis|multiple[ \t]+sclerosis|parkinson'?s|alzheimer'?s|dementia|autism|autistic|"
    r"bipolar|schizophreni\w*|depression|depressed|depressive|anxiety[ \t]+(?:disorder|attacks?)|my[ \t]+anxiety|"
    r"panic[ \t]+attacks?|eating[ \t]+disorder|anorexi\w*|bulimi\w*|self-?harm|suicid\w*|overdos(?:e|ed)|"
    r"addiction|addicted[ \t]+to|alcoholi(?:c|sm)|rehab\b|sobriety|relapsed|methadone|suboxone|naloxone|"
    r"antidepressants?|ssris?|zoloft|prozac|lexapro|xanax|adderall|ritalin|wellbutrin|ozempic|wegovy|"
    r"mental[ \t]+(?:health|illness|breakdown)|psychiatric|therapy[ \t]+session|in[ \t]+therapy|"
    r"physical[ \t]+therapy|chronic[ \t]+(?:pain|illness|fatigue)|long[ \t]+covid|tested[ \t]+positive|"
    r"medical[ \t]+(?:records?|history|leave|condition|appointment)|doctor'?s[ \t]+appointment)\b",
    r"\b(?:pregnant|pregnancy|miscarriage|abortion|contracepti\w+|birth[ \t]+control|morning[ \t]+after[ \t]+pill|"
    r"fertility[ \t]+(?:treatment|clinic)|infertility|erectile|libido|sexual[ \t]+health|"
    r"gender[ \t]+(?:affirming|dysphoria)|hormone[ \t]+(?:therapy|replacement))\b",
    r"\b(?:genetic[ \t]+(?:test(?:ing)?|results?|counsel\w*|condition|disorder)|"
    r"family[ \t]+(?:medical[ \t]+)?history[ \t]+of|hereditary[ \t]+(?:condition|disease|cancer)|carrier[ \t]+screening)\b",
])
_HEALTH_CS = r"\b(?:HIV|AIDS|STDs?|STIs?|PTSD|OCD|ADHD|IVF|HRT|BRCA[12]?)\b(?!::|\.h\b)"
HEALTH_RE = re.compile(r"(?i)(?:" + _HEALTH_CI + r")")
HEALTH_CS_RE = re.compile(_HEALTH_CS)
RESTRICTED_PH = re.compile(r"<RESTRICTED_F?\d+>")
def health_text(text: Optional[str]) -> bool:
    if not text:
        return False
    t = PLACEHOLDER_RE.sub(" ", text)
    return bool(HEALTH_RE.search(t) or HEALTH_CS_RE.search(t))
def _get(o: Any, key: str, default: Any = None) -> Any:
    if isinstance(o, dict):
        return o.get(key, default)
    return getattr(o, key, default)
def turn_flagged(r: Any) -> bool:
    text = _get(r, "text") or ""
    if text == REMOVED_TEXT:
        return False
    if RESTRICTED_PH.search(text):
        return True
    return _get(r, "label") in PROSE_LABELS and health_text(text)
def unit_flagged(unit: Any) -> bool:
    return any(turn_flagged(r) for r in (_get(unit, "rturns") or []))
def _hi(r: Any) -> int:
    try:
        return int(_get(r, "human_index") or 0)
    except (TypeError, ValueError):
        return 0
def _is_window(u: Any) -> bool:
    return (_get(u, "kind") or "segment") == "window"
def drop_units(units: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    info = {"segments": 0, "windows": 0, "windows_cut": 0, "prompts": 0, "tool_calls": 0}
    units = [u for u in units if isinstance(u, dict)]
    gone_eps: Set[Tuple[str, int]] = set()
    gone_hi: Dict[str, Set[int]] = {}
    while True:
        changed = False
        for u in units:
            sid = str(u.get("session_id") or "")
            his = gone_hi.setdefault(sid, set())
            if _is_window(u):
                for r in u.get("rturns") or []:
                    if turn_flagged(r) and _hi(r) not in his:
                        his.add(_hi(r))
                        changed = True
                continue
            key = (sid, int(u.get("episode_index") or 0))
            if key in gone_eps:
                continue
            if unit_flagged(u) or any(_hi(r) in his for r in u.get("rturns") or []):
                gone_eps.add(key)
                changed = True
        for u in units:
            if not _is_window(u) and (str(u.get("session_id") or ""), int(u.get("episode_index") or 0)) in gone_eps:
                his = gone_hi.setdefault(str(u.get("session_id") or ""), set())
                for r in u.get("rturns") or []:
                    if _hi(r) not in his:
                        his.add(_hi(r))
                        changed = True
        if not changed:
            break
    kept: List[Dict[str, Any]] = []
    for u in units:
        sid = str(u.get("session_id") or "")
        his = gone_hi.get(sid) or set()
        if not _is_window(u):
            if (sid, int(u.get("episode_index") or 0)) in gone_eps:
                info["segments"] += 1
                for r in u.get("rturns") or []:
                    t = r.get("text") or ""
                    if r.get("label") == "USER" and not t.startswith("/") and not t.startswith("! "):
                        info["prompts"] += 1
                    elif r.get("label") == "TOOL":
                        info["tool_calls"] += 1
                continue
            kept.append(u)
            continue
        rts = u.get("rturns") or []
        if not any(_hi(r) in his for r in rts):
            kept.append(u)
            continue
        out: List[Dict[str, Any]] = []
        n = 0
        prev_cut = False
        for r in rts:
            if _hi(r) in his:
                if not prev_cut:
                    n += 1
                    out.append({"id": "X%d" % n, "label": "EVENT", "text": REMOVED_TEXT, "human_index": _hi(r),
                                "ts": None})
                prev_cut = True
                continue
            prev_cut = False
            out.append(r)
        if not any(r.get("label") == "USER" for r in out):
            info["windows"] += 1
            continue
        w = dict(u)
        w["rturns"] = out
        w["metrics"] = {}
        stamps = sorted(r["ts"] for r in out if isinstance(r.get("ts"), str) and r.get("ts"))
        if stamps:
            w["started"], w["ended"] = stamps[0], stamps[-1]
        if "est_tokens" in w:
            w["est_tokens"] = int(sum(len(r.get("text") or "") + len(r.get("id") or "") + 4 for r in out) / 3.5)
        info["windows_cut"] += 1
        kept.append(w)
    return kept, info
def removed_any(info: Dict[str, int]) -> bool:
    return bool(info.get("segments") or info.get("windows") or info.get("windows_cut"))
def drop_bundle(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    units = doc.get("units") or []
    kept, info = drop_units(units)
    if not removed_any(info):
        return doc, info
    out = dict(doc)
    out["units"] = kept
    sig = dict(out.get("signals") or {})
    for key, n in (("human_prompts", info["prompts"]), ("tool_calls_total", info["tool_calls"]),
                   ("tool_calls_main", info["tool_calls"])):
        if isinstance(sig.get(key), (int, float)) and not isinstance(sig.get(key), bool):
            sig[key] = max(0, int(sig[key]) - n)
    sess = dict(out.get("session") or {})
    stamps = sorted(x for u in kept for x in (u.get("started"), u.get("ended")) if isinstance(x, str) and x)
    if stamps:
        sess["started_at"], sess["ended_at"] = stamps[0], stamps[-1]
        sig["wall_minutes"] = None
    if not kept:
        for key in ("human_prompts", "tool_calls_total", "tool_calls_main", "active_minutes", "wall_minutes"):
            sig[key] = 0
        for key in ("tool_calls_by_name", "tool_calls_by_category", "skills_used", "model_mix"):
            sig[key] = {}
    out["session"] = sess
    out["signals"] = sig
    return out, info
def _episode_of_human(eps: List[Any]) -> Dict[int, int]:
    out: Dict[int, int] = {0: eps[0].index} if eps else {}
    u = 0
    for ep in eps:
        for t in ep.turns:
            if t.is_human_input:
                u += 1
                out[u] = ep.index
    return out
def _placeholder(n: int) -> Any:
    from ..model import Turn
    return Turn(id="hours-removed-%d" % n, role="system", kind=REMOVED_KIND, text=REMOVED_TEXT)
def drop_session(session: Any, surface: Optional[str] = None, rounds: int = 4) -> Tuple[List[Any], Dict[str, int]]:
    from .render import build_all, split_episodes
    info = {"episodes": 0, "turns": 0}
    units = build_all(session, surface)
    for _ in range(rounds):
        eps = split_episodes(session)
        ep_of = _episode_of_human(eps)
        flagged: Set[int] = set()
        for u in units:
            if u.kind != "window":
                if unit_flagged(u):
                    flagged.add(u.episode_index)
            else:
                for r in u.rturns:
                    if turn_flagged(r) and r.human_index in ep_of:
                        flagged.add(ep_of[r.human_index])
        if not flagged:
            return units, info
        gone: Set[int] = set()
        spawned: Set[str] = set()
        for ep in eps:
            if ep.index in flagged:
                for t in ep.turns:
                    gone.add(id(t))
                    if t.tool is not None and t.tool.spawned_agent_id:
                        spawned.add(t.tool.spawned_agent_id)
        kept_turns = []
        prev_gone = False
        for t in session.turns:
            if id(t) in gone:
                if not prev_gone:
                    info["episodes"] += 1
                    kept_turns.append(_placeholder(info["episodes"]))
                prev_gone = True
                info["turns"] += 1
                continue
            if (t.is_sidechain and t.agent_id and t.agent_id in spawned) or (prev_gone and not t.on_main_branch):
                info["turns"] += 1
                continue
            if not t.is_sidechain and t.on_main_branch:
                prev_gone = False
            kept_turns.append(t)
        session.turns = kept_turns
        session.subagents = [s for s in session.subagents if not (s.agent_id and s.agent_id in spawned)]
        stamps = sorted(t.timestamp for t in session.iter_turns(recursive=True) if t.timestamp is not None)
        if stamps:
            session.started_at, session.ended_at = stamps[0], stamps[-1]
        units = build_all(session, surface)
    return units, info
