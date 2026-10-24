"""Shared helpers: paths, JSON IO, JSON-pointer-like paths, number extraction, stats."""
import base64
import json
import math
import re
from datetime import datetime
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent
CASES_DIR = EVALS_DIR / "cases"
SCENARIOS_DIR = EVALS_DIR / "scenarios"
HOLDOUT_DIR = EVALS_DIR / "holdout"
SCHEMAS_DIR = EVALS_DIR / "schemas"
IMAGES_DIR = EVALS_DIR / "images"
RUNS_DIR = EVALS_DIR / "runs"
REPORTS_DIR = EVALS_DIR / "reports"
SKILLS_DIR = EVALS_DIR / "skills"
RESPONSE_SCHEMA = ROOT / "agent" / "contracts" / "response.schema.json"
FIXTURES_DIR = ROOT / "agent" / "contracts" / "fixtures"

MISSING = object()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_schema(name):
    return read_json(SCHEMAS_DIR / name)


def timestamp(now=None):
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def image_data_url(rel_or_abs):
    """Data URL of an eval image (path relative to the repository root or absolute)."""
    path = Path(rel_or_abs)
    if not path.is_absolute():
        path = ROOT / path
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext)
    if mime is None:
        raise ValueError(f"unsupported image type: {path}")
    return f"data:image/{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------- paths
# "/a/b/0/c" — "/" separated (keys may contain dots, e.g. "/patch/set/destination.name"),
# "*" matches every list element or every object value. "" or "/" is the root.

def split_path(path):
    path = (path or "").strip()
    if path in ("", "/"):
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]


def resolve(data, path):
    """-> list of (concrete_path, value) for every match. Missing branches yield nothing."""
    results = [("", data)]
    for part in split_path(path):
        nxt = []
        for where, node in results:
            if part == "*":
                if isinstance(node, list):
                    nxt += [(f"{where}/{i}", v) for i, v in enumerate(node)]
                elif isinstance(node, dict):
                    nxt += [(f"{where}/{k}", v) for k, v in node.items()]
            elif isinstance(node, dict):
                if part in node:
                    nxt.append((f"{where}/{part}", node[part]))
            elif isinstance(node, list) and re.fullmatch(r"-?\d+", part):
                idx = int(part)
                if -len(node) <= idx < len(node):
                    nxt.append((f"{where}/{part}", node[idx]))
        results = nxt
    return results


def get_path(data, path, default=MISSING):
    found = resolve(data, path)
    if not found:
        return default
    return found[0][1]


def iter_strings(data, skip_keys=()):
    """Every string value in a JSON structure (dict keys are not included)."""
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for k, v in data.items():
            if k in skip_keys:
                continue
            yield from iter_strings(v, skip_keys)
    elif isinstance(data, list):
        for v in data:
            yield from iter_strings(v, skip_keys)


def iter_numbers(data):
    if isinstance(data, bool):
        return
    if isinstance(data, (int, float)):
        yield data
    elif isinstance(data, dict):
        for v in data.values():
            yield from iter_numbers(v)
    elif isinstance(data, list):
        for v in data:
            yield from iter_numbers(v)


# ---------------------------------------------------------------- numbers in text
# Russian formatting: "90 300 ₽" (space / nbsp / narrow nbsp as thousands separator), "6,5 ч".
_SEP = "[ \u00a0\u202f\u2009]"
NUMBER_RE = re.compile(rf"(?<![\w.,])\d{{1,3}}(?:{_SEP}\d{{3}})+(?:[.,]\d+)?(?![\w])|\d+(?:[.,]\d+)?")


def _to_number(token):
    t = re.sub(_SEP, "", token).replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def numbers_in_text(text):
    """Numbers written in a text, grouped thousands joined ("90 300" -> 90300)."""
    out = []
    for m in NUMBER_RE.finditer(text or ""):
        value = _to_number(m.group(0))
        if value is not None:
            out.append(value)
    return out


def number_pool(data):
    """Every number that appears in a JSON structure: numeric values (+ their 1-decimal and
    integer roundings for floats), numbers inside strings (both grouped and digit runs),
    so ISO dates/times contribute their parts (2026-11-12T06:40 -> 2026, 11, 12, 6, 40)."""
    pool = set()
    for n in iter_numbers(data):
        pool.add(n)
        pool.add(abs(n))  # texts write differences without a sign ("на 2 200 ₽ дешевле")
        if isinstance(n, float):
            pool.add(round(n, 1))
            if abs(n - round(n)) < 1e-9:
                pool.add(int(round(n)))
    for s in iter_strings(data):
        for n in numbers_in_text(s):
            pool.add(n)
        for run in re.findall(r"\d+", s):
            pool.add(int(run))
    return pool


def same_number(a, b):
    return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-6)


# ---------------------------------------------------------------- stats

def percentile(values, pct):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def rub(n):
    """90300 -> '90 300 ₽' (UI-like)."""
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ") + " ₽"
