from __future__ import annotations
import re
from typing import Callable, Dict, List, Optional, Tuple
SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("AWS_KEY", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("STRIPE_KEY", re.compile(r"\b[sr]k_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("PASSWORD", re.compile(r"(?i)\b((?:[a-z_]+[ _-])?(?:password|passwd|pwd|passcode|pin|secret|api[_ -]?key|access[_ -]?key|token|bearer|auth))\s*[:=]\s*['\"]?([^\s'\"]{1,})")),
    ("LABELED_KEY", re.compile(r"(?im)^\s*([A-Za-z][\w .-]{1,30}?)\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=.]{16,})['\"]?\s*$")),
    ("CONN_STRING", re.compile(r"\b(postgres(ql)?|mysql|mongodb(\+srv)?|redis)://[^\s'\"<>]+")),
    ("URL_CREDENTIALS", re.compile(r"(?<=://)[^\s/:@'\"<>]{1,64}:[^\s/@'\"<>]{1,128}(?=@[\w\[])")),
]
PII_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")),
]
HIGH_ENTROPY = re.compile(r"(?<![\w/.<-])[A-Za-z0-9_\-+=]{28,}(?![\w/.>])")
def _entropy(s: str) -> float:
    import math
    from collections import Counter
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())
def _looks_secret(value: str, strict: bool = False) -> bool:
    if value.startswith("<") or "/" in value:
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    if not (has_digit and has_alpha):
        return False
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        return False
    return _entropy(value) >= (3.8 if strict else 3.3)
def _luhn(raw: str) -> bool:
    digits = [int(c) for c in raw if c.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0
_REQUIRES: Dict[str, Tuple[str, ...]] = {
    "AWS_KEY": ("AKIA", "ASIA"), "GITHUB_TOKEN": ("gh",), "ANTHROPIC_KEY": ("sk-ant-",), "OPENAI_KEY": ("sk-",),
    "STRIPE_KEY": ("k_live_", "k_test_"), "SLACK_TOKEN": ("xox",), "GOOGLE_KEY": ("AIza",), "JWT": ("eyJ",),
    "PRIVATE_KEY": ("PRIVATE KEY",), "CONN_STRING": ("://",), "URL_CREDENTIALS": ("://",),
    "SSN": ("-",),
}
PROVIDER_KINDS = {"AWS_KEY", "GITHUB_TOKEN", "ANTHROPIC_KEY", "OPENAI_KEY", "STRIPE_KEY", "SLACK_TOKEN",
                  "GOOGLE_KEY", "PRIVATE_KEY"}
TEST_KEY_RE = re.compile(r"^(sk|pk|rk)_test_|^sk-test-|_test_[A-Za-z0-9]", re.I)
PLACEHOLDER_KINDS = ("EMAIL", "PHONE", "SSN", "CARD", "PERSON", "GOV_ID", "TAX_ID", "BANK", "RESTRICTED", "LOCATION",
                     "ADDRESS", "ATTACHMENT")
_KIND_ALT = "|".join(PLACEHOLDER_KINDS)
PLACEHOLDER_RE = re.compile(r"<(?:SECRET(?:_[A-Z_]+)?|" + _KIND_ALT + r")_F?\d+>")
PLACEHOLDER_NUM_RE = re.compile(r"<(SECRET(?:_[A-Z_]+)?|" + _KIND_ALT + r")_(\d+)>")
_PLACEHOLDER_MAX = 64
def cut_head(text: str, n: int) -> str:
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    for m in PLACEHOLDER_RE.finditer(text, max(0, n - _PLACEHOLDER_MAX)):
        if m.start() >= n:
            break
        if m.end() > n:
            return text[:m.start()]
    return text[:n]
def cut_tail(text: str, n: int) -> str:
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    start = len(text) - n
    for m in PLACEHOLDER_RE.finditer(text, max(0, start - _PLACEHOLDER_MAX)):
        if m.start() >= start:
            break
        if m.end() > start:
            return text[m.end():]
    return text[start:]
DATA_URI_RE = re.compile(r"data:(?:image|audio|video|application|font)/[\w.+-]{1,60}(?:;[\w.+-]{1,40}=[\w.+-]{1,60})*"
                         r";base64,[A-Za-z0-9+/]{16,}={0,2}")
B64_BLOB_RE = re.compile(r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/]*[0-9+/])[A-Za-z0-9+/]{400,}={0,2}(?![A-Za-z0-9+/=])")
_GAP = r"""[ \t]*(?:(?:[:#=]|\bis\b|\bwas\b|\bare\b|\bof\b)[ \t]*){0,2}['"]?"""
_NUM = r"(?:[ \t]*(?:number|numbers|no\.?|num|nbr|\#|id))?"
_LABELED: List[Tuple[str, Optional[str], re.Pattern, Callable[[str], bool], Callable[[str], bool]]] = []
def _digits(v: str) -> int:
    return sum(c.isdigit() for c in v)
def _any(*keys: str) -> Callable[[str], bool]:
    return lambda low: any(k in low for k in keys)
def _lab(kind: str, rule: Optional[str], label: str, value: str, ok: Callable[[str], bool],
         gate: Callable[[str], bool]) -> None:
    _LABELED.append((kind, rule, re.compile(r"(?i)\b(?:" + label + r")" + _GAP + r"(?P<v>" + value + r")"), ok, gate))
_TAX_WORD = re.compile(r"\b(?:f?eins?|tins?|tax|taxpayer|employer)\b")
_HEALTH_PREFIX = ("health", "medical", "dental", "vision", "insurance")
_HEALTH_SUFFIX = ("member", "subscriber", "policy", "group", "id")
_lab("SSN", "pii", r"ssns?|ss\#|social[ \t]+security" + _NUM + r"|soc(?:ial)?\.?[ \t]*sec(?:urity)?\.?" + _NUM,
     r"\d{3}[ -]?\d{2}[ -]?\d{4}(?![\d-])", lambda v: _digits(v) == 9 and v.replace(" ", "")[:3] not in ("000", "666"),
     _any("ssn", "ss#", "social sec", "soc sec", "soc. sec", "socsec"))
_lab("TAX_ID", None, r"itins?" + _NUM, r"9\d{2}[ -]?\d{2}[ -]?\d{4}(?![\d-])", lambda v: _digits(v) == 9,
     _any("itin"))
_lab("TAX_ID", None, r"f?eins?" + _NUM + r"|employer[ \t]+identification" + _NUM + r"|(?:federal[ \t]+)?tax[ \t]*id"
     + _NUM + r"|tax(?:payer)?[ \t]+identification" + _NUM + r"|tins?",
     r"\d{2}[ -]?\d{7}(?![\d-])|\d{3}[ -]?\d{2}[ -]?\d{4}(?![\d-])", lambda v: _digits(v) == 9,
     lambda low: bool(_TAX_WORD.search(low)))
_lab("GOV_ID", None, r"passport" + _NUM, r"[A-Za-z]{0,2}\d{6,9}(?![A-Za-z0-9])", lambda v: _digits(v) >= 6,
     _any("passport"))
_lab("GOV_ID", None, r"driver'?s?[ \t]*licen[cs]e" + _NUM + r"|driving[ \t]+licen[cs]e" + _NUM + r"|DL[ \t]*(?:number|no\.?|num|\#)"
     + r"|licen[cs]e[ \t]*(?:number|no\.?|\#)|state[ \t]+id(?:[ \t]*card)?" + _NUM + r"|id[ \t]+card[ \t]*(?:number|no\.?|\#)",
     r"[A-Za-z0-9](?:[A-Za-z0-9-]{3,18})[A-Za-z0-9](?![A-Za-z0-9-])", lambda v: _digits(v) >= 4,
     _any("licen", "dl n", "dl#", "dl #", "dln", "state id", "id card"))
_lab("GOV_ID", None, r"alien[ \t]+(?:registration[ \t]+)?(?:number|no\.?|\#)|a-number|uscis[ \t]*(?:number|no\.?|\#|account)",
     r"A?-?\d{8,9}(?![\d-])", lambda v: _digits(v) >= 8, _any("alien", "a-number", "uscis"))
_lab("BANK", None, r"routing" + _NUM + r"|aba(?:[ \t]*routing)?" + _NUM + r"|rtn",
     r"\d{9}(?!\d)", lambda v: True, _any("routing", "aba", "rtn"))
_lab("BANK", None, r"(?:(?:bank|checking|savings|brokerage|deposit)[ \t]+)?(?:account|acct|a/c)\.?[ \t]*(?:number|no\.?|num|\#)"
     + r"|(?:bank|checking|savings)[ \t]+(?:account|acct)|acct\.?",
     r"\d[\d -]{2,22}\d(?![\d-])", lambda v: 4 <= _digits(v) <= 17 and len(set(c for c in v if c.isdigit())) > 1,
     _any("account n", "account#", "account #", "accountn", "acct", "a/c", "bank account", "checking account",
          "savings account"))
_lab("RESTRICTED", None, r"mrn|medical[ \t]+record" + _NUM + r"|patient[ \t]*(?:id|number|no\.?|\#)|chart[ \t]*(?:number|no\.?|\#)"
     + r"|medicare[ \t]*(?:id|number|no\.?|\#|card(?:[ \t]*number)?|beneficiary[ \t]+identifier)"
     + r"|medicaid[ \t]*(?:id|number|no\.?|\#|card(?:[ \t]*number)?)|mbi"
     + r"|(?:health|medical|dental|vision|insurance)(?:[ \t]+(?:insurance|plan))?[ \t]*(?:member|subscriber|policy|group|id)"
     + _NUM + r"|health[ \t]*plan" + _NUM + r"|rx[ \t]*(?:bin|pcn|group)",
     r"[A-Za-z0-9](?:[A-Za-z0-9-]{2,20})[A-Za-z0-9](?![A-Za-z0-9-])", lambda v: _digits(v) >= 3,
     lambda low: (any(k in low for k in ("mrn", "medical record", "patient", "chart n", "chart #", "chart#",
                                         "medicare", "medicaid", "mbi", "health plan", "healthplan", "rx bin", "rxbin",
                                         "rx pcn", "rxpcn", "rx group", "rxgroup"))
                  or (any(k in low for k in _HEALTH_PREFIX) and any(k in low for k in _HEALTH_SUFFIX))))
_A = "AC-HJKMNP-RT-Y"
MBI_RE = re.compile(r"[1-9][" + _A + r"][" + _A + r"0-9]\d-?[" + _A + r"][" + _A + r"0-9]\d-?[" + _A
                    + r"]{2}\d{2}(?![A-Za-z0-9-])")
IBAN_LENGTHS = {"AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
                "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
                "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
                "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
                "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
                "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
                "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
                "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20}
IBAN_RE = re.compile(r"[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?(?![A-Za-z0-9])")
def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if IBAN_LENGTHS.get(s[:2]) != len(s):
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False
EIN_RE = re.compile(r"(\d{2})-\d{7}(?![\w-])")
_EIN_PREFIXES = {f"{n:02d}" for n in list(range(1, 7)) + list(range(10, 17)) + list(range(20, 28)) + list(range(30, 49))
                 + list(range(50, 69)) + list(range(71, 78)) + list(range(80, 89)) + list(range(90, 96)) + [98, 99]}
HEALTH_CONNECTOR_WORDS = (r"healthex|function[_-]?health|b[_-]?well|apple[_-]?health|healthkit|health[_-]?connect"
                          r"|google[_-]?fit|mychart|epic[_-]?(?:fhir|mychart|ehr)|cerner|athena[_-]?health|fhir"
                          r"|health[_-]?gorilla|1up[_-]?health|particle[_-]?health|medical[_-]?records?"
                          r"|patient[_-]?portal|health[_-]?records?|lab[_-]?results?|labcorp|quest[_-]?diagnostics"
                          r"|myfitnesspal|oura|whoop|dexcom|libre[_-]?view|withings|fitbit|garmin[_-]?health"
                          r"|chatgpt[_-]?health|openai[_-]?health")
HEALTH_TOOL_RE = re.compile(r"(?i)\bmcp__[a-z0-9_-]*?(?:" + HEALTH_CONNECTOR_WORDS + r")[a-z0-9_-]*")
_HEALTH_NAME_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(?:" + HEALTH_CONNECTOR_WORDS + r")(?:[^a-z0-9]|$)")
def is_health_connector(name: Optional[str]) -> bool:
    return bool(name) and bool(_HEALTH_NAME_RE.search(str(name)))
_LAB_LONG = (r"hemoglobin[ \t]*a1c|hb[ \t]?a1c|a1c|fasting[ \t]+glucose|glucose|ldl(?:-c)?|hdl(?:-c)?|"
             r"total[ \t]+cholesterol|cholesterol|triglycerides|tsh|free[ \t]+t4|free[ \t]+t3|hemoglobin|hematocrit|wbc|rbc|"
             r"platelets?|sodium|potassium|chloride|bicarbonate|bun|creatinine|e?gfr|calcium|albumin|total[ \t]+protein|"
             r"bilirubin|alkaline[ \t]+phosphatase|ferritin|vitamin[ \t]+d|25-oh[ \t]+vitamin[ \t]+d|vitamin[ \t]+b12|b12|"
             r"folate|psa|crp|hs-crp|esr|inr|troponin|bnp|nt-probnp|uric[ \t]+acid|magnesium|phosphorus|testosterone|"
             r"estradiol|progesterone|cortisol|c-peptide|lipase|amylase|lactate|microalbumin|hcg|ggt|tibc")
_LAB_SHORT = r"na|k|cl|co2|alt|ast|alp|plt|hgb|hct|mcv|mch|mchc|rdw|t3|t4|lh|fsh|pt|ptt|ca|mg|fe"
_LAB_UNIT = (r"mg/dl|mmol/l|g/dl|miu/l|uiu/ml|µiu/ml|miu/ml|ng/ml|pg/ml|ng/dl|u/l|iu/l|meq/l|k/ul|k/µl|"
             r"x ?10\^?e?3/u?l|x ?10\^?e?3/µl|10\^3/µl|m/ul|fl|ml/min(?:/1\.73 ?m2)?|mm/hr|nmol/l|pmol/l|µg/dl|ug/dl|mcg/dl")
_LAB_TAIL = (r"(?:[ \t]*\(?\b(?:h|l|high|low|abnormal|critical)\b\)?"
             r"|[ \t]*\(?(?:ref(?:erence)?(?:[ \t]*range)?[ \t]*:?[ \t]*)?\d+(?:\.\d+)?[ \t]*-[ \t]*\d+(?:\.\d+)?\)?)")
LAB_RE = re.compile(
    r"(?i)(?<![\w/.-])(?:(?P<long>" + _LAB_LONG + r")|(?P<short>" + _LAB_SHORT + r"))\b[ \t]*(?:\([^)\n]{0,20}\))?"
    r"[ \t]*[:=]?[ \t]*(?P<val>[<>]?\d{1,4}(?:\.\d{1,3})?)[ \t]*(?:(?P<unit>" + _LAB_UNIT + r")\b|(?P<pct>%))?"
    r"(?P<tail>" + _LAB_TAIL + r")?")
MED_RE = re.compile(
    r"(?i)(?<![\w/.-])[a-z][a-z]{3,24}(?:[ -](?:xr|er|sr|xl|dr|cr|hcl|odt))?[ \t]+\d{1,4}(?:\.\d{1,2})?[ \t]*"
    r"(?:mg|mcg|µg|ug|ml|units?|iu)\b[^\n]{0,40}?\b(?:once|twice|three[ \t]+times|daily|nightly|weekly|bid|tid|qid|qd|"
    r"qhs|qam|qpm|prn|q\d{1,2}h|every[ \t]+\d{1,2}[ \t]+hours|as[ \t]+needed|at[ \t]+bedtime|before[ \t]+meals|with[ \t]+meals|"
    r"by[ \t]+mouth|orally|po|subq|sc|im|iv|inhaled|topically)\b")
MED_HEADER_RE = re.compile(r"(?im)^[ \t>*#-]*(?:current[ \t]+)?(?:medications?|meds|med[ \t]+list|prescriptions?|"
                           r"active[ \t]+medications?)[ \t]*:")
_CLUSTER_GAP = 220
def _lab_hits(text: str) -> List[Tuple[int, int]]:
    out = []
    for m in LAB_RE.finditer(text):
        unit, pct, tail = m.group("unit"), m.group("pct"), m.group("tail")
        if m.group("short"):
            if not unit:
                continue
        elif not (unit or tail or (pct and re.match(r"(?i)hb[ \t]?a1c|a1c|hemoglobin[ \t]*a1c", m.group("long")))):
            continue
        out.append((m.start(), m.end()))
    return out
def _health_record_spans(text: str) -> List[Tuple[int, int]]:
    raw = sorted(_lab_hits(text) + [(m.start(), m.end()) for m in MED_RE.finditer(text)])
    if not raw:
        return []
    hits: List[Tuple[int, int]] = []
    for h in raw:
        if hits and h[0] < hits[-1][1]:
            hits[-1] = (hits[-1][0], max(h[1], hits[-1][1]))
        else:
            hits.append(h)
    headers = [m.end() for m in MED_HEADER_RE.finditer(text)]
    groups: List[List[Tuple[int, int]]] = []
    for h in hits:
        if groups and h[0] - groups[-1][-1][1] <= _CLUSTER_GAP:
            groups[-1].append(h)
        else:
            groups.append([h])
    spans = []
    for g in groups:
        near_header = any(0 <= g[0][0] - e <= _CLUSTER_GAP for e in headers)
        if len(g) < 2 and not near_header:
            continue
        s, e = g[0][0], g[-1][1]
        ls = text.rfind("\n", 0, s) + 1
        le = text.find("\n", e)
        le = len(text) if le < 0 else le
        if s - ls <= 40:
            s = ls
        if le - e <= 60:
            e = le
        spans.append((s, e))
    return spans
GPS_PAIR_RE = re.compile(r"(?<![\w.])(?P<a>[-+]?\d{1,3}\.\d{4,})°?[ \t]*(?P<ah>[NSns])?[ \t]*,[ \t]*"
                         r"(?P<b>[-+]?\d{1,3}\.\d{4,})°?[ \t]*(?P<bh>[EWew])?(?![\w.])")
GPS_LABELED_RE = re.compile(r"(?i)\b(?:lat(?:itude)?|lng|lon(?:g(?:itude)?)?)\b[\"']?[ \t]*[:=][ \t]*(?P<v>[-+]?\d{1,3}\.\d{4,})")
GPS_DMS_RE = re.compile(r"\d{1,3}[ \t]*°[ \t]*\d{1,2}[ \t]*['′][ \t]*(?:\d{1,2}(?:\.\d+)?[ \t]*[\"″][ \t]*)?[NSEW]\b")
def _free(m: "re.Match", before: str) -> bool:
    s = m.start()
    if s == 0:
        return True
    c = m.string[s - 1]
    return not (c.isalnum() or c == "_" or c in before)
_GPS_GATE = re.compile(r"\d\.\d{4}")
_LAB_GATE = re.compile(r"(?i)\d[ \t]*(?:mg|mcg|µg|ug|ml\b|g/dl|mmol|miu|uiu|µiu|ng/|pg/|u/l|iu\b|meq|k/[uµ]l|x ?10|"
                       r"10\^|m/ul|fl\b|ml/min|mm/hr|nmol|pmol|units?\b)")
_LAB_GATE_WORDS = ("(h)", "(l)", "(high)", "(low)", "(abnormal)", "a1c", "medication", "meds:", "med list",
                   "prescription")
def _gps_ok(a: str, b: str, text: str, s: int, e: int) -> bool:
    x, y = float(a), float(b)
    if not ((abs(x) <= 90 and abs(y) <= 180) or (abs(x) <= 180 and abs(y) <= 90)):
        return False
    if abs(x) < 1 and abs(y) < 1:
        return False
    before, after = text[max(0, s - 24):s], text[e:e + 24]
    if re.search(r"\d[ \t]*,[ \t]*$", before) or re.match(r"[ \t]*,[ \t]*[-+]?\d", after):
        return False
    return True
_CLEAN: set = set()
_CLEAN_SIZE = [0]
CLEAN_MAX_CHARS = 8_000_000
CLEAN_MAX_TEXT = 20_000
def _remember_clean(text: str) -> None:
    n = len(text)
    if n > CLEAN_MAX_TEXT:
        return
    if _CLEAN_SIZE[0] + n > CLEAN_MAX_CHARS:
        _CLEAN.clear()
        _CLEAN_SIZE[0] = 0
    _CLEAN.add(text)
    _CLEAN_SIZE[0] += n
class Scrubber:
    def __init__(self) -> None:
        self.map: Dict[Tuple[str, str], str] = {}
        self.counts: Dict[str, int] = {}
        self.rule_kinds: Dict[str, int] = {"provider": 0, "structured": 0, "labeled": 0, "entropy": 0, "pii": 0}
        self.test_keys = 0
        self.provider_live = 0
        self.tier_counts: Dict[str, int] = {}
        self.hits = 0
    def _note(self, rule: str, value: str) -> None:
        if rule == "provider" and TEST_KEY_RE.search(value):
            self.test_keys += 1
            return
        self.rule_kinds[rule] = self.rule_kinds.get(rule, 0) + 1
        if rule == "provider":
            self.provider_live += 1
    def _hit(self, rule: str, kind: str, value: str) -> str:
        self.hits += 1
        self._note(rule, value)
        return self._token(kind, value)
    def _tier(self, kind: str, value: str, rule: Optional[str] = None) -> str:
        self.hits += 1
        if rule:
            self._note(rule, value)
        self.tier_counts[kind] = self.tier_counts.get(kind, 0) + 1
        return self._token(kind, value)
    def _token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self.map:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.map[key] = f"<{kind}_{self.counts[kind]}>"
        return self.map[key]
    def scrub(self, text: str | None) -> str | None:
        if not text or text in _CLEAN:
            return text
        before = self.hits
        out = self._scrub(text)
        if self.hits == before and out == text:
            _remember_clean(text)
        return out
    def _scrub(self, text: str) -> str:
        if "base64," in text:
            text = DATA_URI_RE.sub(lambda m: self._tier("ATTACHMENT", m.group(0)), text)
        if len(text) >= 400 and max(map(len, text.split())) >= 400:
            text = B64_BLOB_RE.sub(lambda m: self._tier("ATTACHMENT", m.group(0)), text)
        for kind, pat in SECRET_PATTERNS:
            req = _REQUIRES.get(kind)
            if req and not any(r in text for r in req):
                continue
            if kind == "PASSWORD":
                text = pat.sub(lambda m: f"{m.group(1)}: {self._hit('labeled', 'SECRET', m.group(2))}", text)
            elif kind == "LABELED_KEY":
                text = pat.sub(lambda m: (f"{m.group(1)}: {self._hit('labeled', 'SECRET', m.group(2))}"
                                          if _looks_secret(m.group(2)) else m.group(0)), text)
            elif kind == "URL_CREDENTIALS":
                text = pat.sub(lambda m: self._tier("SECRET_URL_CREDENTIALS", m.group(0), "structured"), text)
            else:
                rule = "provider" if kind in PROVIDER_KINDS else "structured"
                text = pat.sub(lambda m, k=kind, r=rule: self._hit(r, "SECRET_" + k, m.group(0)), text)
        text = HIGH_ENTROPY.sub(lambda m: self._hit("entropy", "SECRET", m.group(0))
                                if _looks_secret(m.group(0), strict=True) else m.group(0), text)
        for kind, pat in PII_PATTERNS:
            req = _REQUIRES.get(kind)
            if req and not any(r in text for r in req):
                continue
            if kind == "CARD":
                text = pat.sub(lambda m: self._hit("pii", "CARD", m.group(0)) if _luhn(m.group(0)) else m.group(0),
                               text)
            else:
                text = pat.sub(lambda m, k=kind: self._hit("pii", k, m.group(0)), text)
        return self._tier_a(text)
    def _tier_a(self, text: str) -> str:
        low = text.lower()
        for kind, rule, pat, ok, gate in _LABELED:
            if not gate(low):
                continue
            def rep(m, kind=kind, rule=rule, ok=ok):
                v = m.group("v")
                if not ok(v):
                    return m.group(0)
                g, base = m.group(0), m.start()
                s, e = m.span("v")
                return g[:s - base] + self._tier(kind, v, rule) + g[e - base:]
            text = pat.sub(rep, text)
        if "mcp__" in text:
            text = HEALTH_TOOL_RE.sub(lambda m: self._tier("RESTRICTED", m.group(0).lower()), text)
        if any(c.isdigit() for c in text):
            if _LAB_GATE.search(text) or any(k in low for k in _LAB_GATE_WORDS):
                for s, e in reversed(_health_record_spans(text)):
                    text = text[:s] + self._tier("RESTRICTED", text[s:e]) + text[e:]
            text = MBI_RE.sub(lambda m: self._tier("RESTRICTED", m.group(0).replace("-", ""))
                              if _free(m, "-") else m.group(0), text)
            text = IBAN_RE.sub(lambda m: self._tier("BANK", m.group(0).replace(" ", ""))
                               if _free(m, "") and _iban_ok(m.group(0)) else m.group(0), text)
            if "-" in text:
                text = EIN_RE.sub(lambda m: self._tier("TAX_ID", m.group(0))
                                  if _free(m, "-") and m.group(1) in _EIN_PREFIXES else m.group(0), text)
            if _GPS_GATE.search(text):
                text = GPS_LABELED_RE.sub(lambda m: m.group(0)[:m.start("v") - m.start()]
                                          + self._tier("LOCATION", m.group("v")), text)
                text = GPS_PAIR_RE.sub(lambda m: self._tier("LOCATION", m.group(0))
                                       if _gps_ok(m.group("a"), m.group("b"), m.string, m.start(), m.end())
                                       else m.group(0), text)
            if "°" in text:
                text = GPS_DMS_RE.sub(lambda m: self._tier("LOCATION", m.group(0)), text)
        return text
    def scrub_safe(self, text: str | None) -> str | None:
        if not text:
            return text
        parts: List[str] = []
        last = 0
        for m in PLACEHOLDER_RE.finditer(text):
            parts.append(self.scrub(text[last:m.start()]) or "")
            parts.append(m.group(0))
            last = m.end()
        parts.append(self.scrub(text[last:]) or "")
        return "".join(parts)
    def scrub_people(self, text: str | None) -> str | None:
        return text
    def scrub_prose(self, text: str | None) -> str | None:
        return self.scrub(text)
    def snapshot(self) -> Dict[str, int]:
        out = dict(self.rule_kinds)
        out["test_keys"] = self.test_keys
        out["provider_live"] = self.provider_live
        return out
    @property
    def secret_count(self) -> int:
        return sum(v for k, v in self.counts.items() if k.startswith("SECRET"))
def seeded_scrubber(texts) -> Scrubber:
    sc = Scrubber()
    for t in texts:
        for m in PLACEHOLDER_NUM_RE.finditer(t or ""):
            sc.counts[m.group(1)] = max(sc.counts.get(m.group(1), 0), int(m.group(2)))
    return sc
