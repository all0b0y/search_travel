"""Deterministic guards on present-card output (AGENT_PLAN.md section 5 step 7, decision 10).

    issues = check_card_output("present.variants", card_input, output, rules)

Checks (Issue = {code, severity, message, data}):
- numbers: every number written in the output appears in the card input (block `invented_number`);
  inside an item's why/sacrifices/risks/not_for/watch_out, a number found only in ANOTHER item's
  input is `foreign_number` (warn: comparisons are allowed, mixing variants up is suspicious).
- ids: items[].id, recommended_id and upsell[].candidate_id are input ids/candidates (block), item
  ids unique, upsell count ≤ rules.max_upsell and none without candidates.
- questions: question marks in the message ≤ rules.max_questions (block); more than the engine
  asked -> warn `extra_question`.
- tone: jokes ≤ guard.max_jokes, emoji ≤ guard.max_emoji (★ counts), "!" ≤ guard.max_exclamations,
  no joke in a sentence about money, risks or cancellation (warn: heuristics); ratios such as
  "вдвое", "в 2 раза", "на треть", "на 20% дешевле" are computed claims (warn `computed_claim`);
  guard.banned_phrases (case-insensitive substrings) block.
- length: per card guard.message_chars[card] = [min, max]: empty or over max + length_slack_pct (20 %)
  blocks, over max warns, under min warns; without a per-card entry: hard max message_max_chars (1500)
  blocks and message_soft_max_chars (900) warns.
- questions: "?" groups > rules.max_questions block; more than the engine asked warns (only for the
  cards in guard.question_mark_needs_questions when that list is given).

Settings are read from rules.json "guard" (the skill's key; "guards" is accepted too).

Number normalisation rules (documented contract for the skill prompts):
- spaces of any kind as thousands separators: "84 000", "84 000", "84 000" == 84000 == "84000";
  a grouped number that is not in the input is also accepted when every group alone is.
- "84 тыс.", "84 тыс", "84 тысячи", "84к", "84k", "84К" (no space before к) == 84 000 ± half of the last written digit
  (84 тыс. matches 83 500..84 499; "84,3 тыс" matches 84 250..84 349); "1,5 млн" likewise.
- decimals with comma or dot: "6,5" == "6.5" == 6.5; input floats also match when rounded to 0/1 digit.
- "d.mm" / "d.mm.yyyy" may be a date: accepted when that day.month is in the input (any ISO date or
  "dd.mm" string there), or as a decimal; the day and month numbers of input dates are also allowed
  alone ("12–19 ноября").
- clock times "06:40" / "6:40" must be a time present in the input (ISO datetimes, "HH:MM" strings).
- ranges "12–19" are two numbers; signs and "+"/"−" are ignored; "₽", "%", "ч", "мин" are units only.
- always allowed: integers 0..rules.guards.small_int_max (default 10) used as counts and labels.
- derived input values: for keys ending in "_min" also hours (x/60, 1 digit) and the minute part
  (x % 60); for keys containing "hours" also the minutes of the fraction; for keys ending in "_m"
  also kilometres (x/1000, 1 digit). Years and dates count only when present in the input.
"""
from __future__ import annotations

import re
import unicodedata

DEFAULT_GUARDS = {"small_int_max": 10, "max_emoji": 2, "max_exclamations": None, "message_max_chars": 1500,
                  "message_soft_max_chars": 900, "length_slack_pct": 20, "banned_phrases": [],
                  "question_mark_needs_questions": None}
RATIO = re.compile(r"\b(?:вдвое|втрое|вчетверо|впятеро|в\s+полтора\s+раза|в\s+\d+(?:[.,]\d+)?\s+раз[а]?|"
                   r"в\s+(?:два|три|четыре|пять|десять)\s+раз[а]?|на\s+треть|на\s+четверть|наполовину|"
                   r"на\s+\d+(?:[.,]\d+)?\s*%\s+(?:дешевле|дороже|быстрее|медленнее))(?!\w)", re.I)   # not «вдвоём»
ITEM_TEXT_KEYS = ("why", "sacrifices", "risks", "not_for", "watch_out")
LATIN_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*")
LATIN_OK = {"wi-fi", "wifi", "spa", "ok", "all", "inclusive", "ultra", "check-in", "check-out", "sup", "vip"}
ID_KEYS = {"id", "candidate_id", "recommended_id", "offer_id", "hotel_id", "destination_id", "rule_id"}
MONEY_WORDS = re.compile(r"₽|руб|\bцен|стоим|отмен|штраф|невозврат|риск|стыковк|опозда|доплат", re.I)
JOKE_WORDS = re.compile(r"шучу|хаха|ахах|хех|\bлол\b|😄|😂|🤣|😅|😜|😉|🙃|😏|😎|🤪", re.I)
SMILEY = re.compile(r"[:;]-?\)|\){2,}|\(\s*:")

_SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009\u2007\u2002\u2003"), " ")
_SCALES = [(re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:млн\.?|миллион\w*)", re.I), 1_000_000),
           (re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?:\s*(?:тыс\.?|тысяч\w*)|(?:к|k)\b)", re.I), 1_000)]
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_DM = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{2})(?:\.(\d{4}|\d{2}))?(?!\d|[.,]\d)")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?: \d{3})+(?![\d])(?![.,]\d)")
_NUM = re.compile(r"(?<![\d])\d+(?:[.,]\d+)?")


def _issue(code, severity, message, **data):
    return {"code": code, "severity": severity, "message": message, "data": data}


def _guards(rules):
    """rules.json "guard" (the skill's key) or "guards"; max_jokes falls back to the top-level key."""
    rules = rules or {}
    own = {**(rules.get("guards") or {}), **(rules.get("guard") or {})}
    g = {**DEFAULT_GUARDS, **own}
    g["max_jokes"] = own.get("max_jokes", rules.get("max_jokes", 1))
    return g


def _f(text):
    return float(text.replace(",", "."))


# ---------------------------------------------------------------- tokens

def tokens(text):
    """Numeric tokens of a text: [(kind, value, raw)] with kind in num|scaled|time|dm|grouped.

    scaled: value = (lo, hi) range in units; grouped: value = (int, [parts]); dm: (day, month, decimal);
    time: (h, m); num: float.
    """
    text = unicodedata.normalize("NFC", str(text)).translate(_SPACES)
    out = []

    def blank(m):
        return " " * (m.end() - m.start())

    for rx, scale in _SCALES:
        def scaled(m, scale=scale):
            raw = m.group(1)
            digits = len(raw.replace(",", ".").partition(".")[2])
            half = 0.5 * scale / (10 ** digits)
            v = _f(raw) * scale
            out.append(("scaled", (v - half, v + half), m.group(0)))
            return blank(m)
        text = rx.sub(scaled, text)

    def time_(m):
        out.append(("time", (int(m.group(1)), int(m.group(2))), m.group(0)))
        return blank(m)
    text = _TIME.sub(time_, text)

    def iso(m):
        out.append(("dm", (int(m.group(3)), int(m.group(2)), None), m.group(0)))
        out.append(("num", float(m.group(1)), m.group(1)))
        return blank(m)
    text = _ISO_DATE.sub(iso, text)

    def dm(m):
        d, mo = int(m.group(1)), int(m.group(2))
        out.append(("dm", (d, mo, _f(f"{m.group(1)}.{m.group(2)}")), m.group(0)))
        if m.group(3):
            year = int(m.group(3))
            out.append(("num", float(year if year > 99 else 2000 + year), m.group(3)))
        return blank(m)
    text = _DM.sub(dm, text)

    def grouped(m):
        parts = m.group(0).split(" ")
        out.append(("grouped", (int("".join(parts)), [int(p) for p in parts]), m.group(0)))
        return blank(m)
    text = _GROUPED.sub(grouped, text)
    for m in _NUM.finditer(text):
        out.append(("num", _f(m.group(0)), m.group(0)))
    return out


class Allowed:
    """Numbers, clock times and day.month pairs available in a card input."""

    def __init__(self):
        self.nums: set[float] = set()
        self.times: set[tuple] = set()
        self.dates: set[tuple] = set()

    def add_num(self, x):
        x = float(x)
        self.nums.add(round(x, 6))
        self.nums.add(float(round(x)))
        self.nums.add(round(x, 1))

    def add_text(self, text):
        for kind, value, _ in tokens(text):
            if kind == "num":
                self.add_num(value)
            elif kind == "grouped":
                self.add_num(value[0])
            elif kind == "time":
                self.times.add(value)
            elif kind == "dm":
                self.dates.add(value[:2])
                self.add_num(value[0])   # "12–19 ноября": days of input dates may stand alone
                self.add_num(value[1])
                if value[2] is not None:
                    self.add_num(value[2])
            elif kind == "scaled":
                self.add_num((value[0] + value[1]) / 2)

    def walk(self, obj, key=""):
        if isinstance(obj, bool) or obj is None:
            return
        if isinstance(obj, (int, float)):
            self.add_num(obj)
            k = key.lower()
            if k.endswith("_min") or k == "minutes":
                self.add_num(round(obj / 60, 1))
                self.add_num(obj % 60)
                self.add_num(obj // 60)
            if "hours" in k:
                self.add_num(round((obj % 1) * 60))
                self.add_num(round(obj * 60))
            if k.endswith("_m") and obj >= 100:
                self.add_num(round(obj / 1000, 1))
            return
        if isinstance(obj, str):
            self.add_text(obj)
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                self.walk(v, str(k))
            return
        if isinstance(obj, (list, tuple)):
            for v in obj:
                self.walk(v, key)

    def ok(self, kind, value, small_max):
        if kind == "num":
            if value == int(value) and 0 <= value <= small_max:
                return True
            return round(value, 6) in self.nums
        if kind == "grouped":
            whole, parts = value
            return float(whole) in self.nums or all(self.ok("num", float(p), small_max) for p in parts)
        if kind == "scaled":
            lo, hi = value
            return any(lo <= x < hi for x in self.nums)
        if kind == "time":
            return value in self.times
        if kind == "dm":
            d, m, dec = value
            return (d, m) in self.dates or (dec is not None and round(dec, 6) in self.nums)
        return False


def allowed_from(obj) -> Allowed:
    a = Allowed()
    a.walk(obj)
    return a


def bad_numbers(text, allowed: Allowed, small_max=10):
    return [raw for kind, value, raw in tokens(text) if not allowed.ok(kind, value, small_max)]


# ---------------------------------------------------------------- helpers

def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _strings(obj, skip=ID_KEYS):
    """Every string in an output object except id fields."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                yield from _strings(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, skip)


def is_emoji(ch):
    cp = ord(ch)
    return (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x1F000 <= cp <= 0x1F2FF
            or 0x2B00 <= cp <= 0x2BFF) and ch not in "✓✔→"   # ★ counts: prompts write "4 звезды"


def emoji_count(text):
    return sum(1 for ch in text if is_emoji(ch))


def _sentences(text):
    """Sentences; an emoji or a smiley also ends one (each closes a joke)."""
    text = SMILEY.sub(lambda m: m.group(0) + "\n", text)
    text = "".join(ch + "\n" if is_emoji(ch) else ch for ch in text)
    return [s for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]


def _has_joke(sentence):
    """Joke markers: laughing/winking faces, "шучу"/"хаха", text smileys. Decorative emoji are counted
    separately (max_emoji), not as jokes."""
    return bool(JOKE_WORDS.search(sentence) or SMILEY.search(sentence))


def question_count(text):
    return len(re.findall(r"\?+", text))


def _input_ids(card_input):
    items = card_input.get("items") or []
    return [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id") is not None]


def _candidate_ids(card_input):
    cands = card_input.get("upsell_candidates") or []
    return {str(c.get("candidate_id") or c.get("id")) for c in cands if isinstance(c, dict)}


# ---------------------------------------------------------------- entry point

def latin_issues(output, card_input, g=None) -> list:
    """Russian text only: a Latin word that is not a name from the input (hotel, carrier, IATA) is a model slip
    («families», «brief»), and a word that mixes scripts («dobерётесь») always is. Both block: one rerun."""
    allowed = {w.casefold() for w in LATIN_OK | set((g or {}).get("latin_allowed") or [])}
    in_text = " ".join(_strings(card_input)).casefold()
    # single lowercase Latin values of the input are enum codes («walkable», «breakfast», «coupe»), not names
    codes = {s.casefold() for s in _strings(card_input) if re.fullmatch(r"[a-z][a-z_\-]*", s)} - allowed
    found, seen = [], set()
    for text in _strings(output):
        for w in LATIN_WORD.findall(text):
            if not re.search(r"[A-Za-z]", w) or w in seen or len(w) < 2:   # «x», «A» are not words
                continue
            seen.add(w)
            mixed = bool(re.search(r"[А-Яа-яЁё]", w))
            if mixed:
                found.append(_issue("mixed_script", "block", f"Слово «{w}» смешивает латиницу и кириллицу", word=w))
            elif w.casefold() not in allowed and (w.casefold() not in in_text or w.casefold() in codes):
                found.append(_issue("latin_word", "block", f"Слово «{w}» на латинице — пишите по-русски", word=w))
    return found


def check_card_output(card, card_input: dict, output: dict, rules: dict):
    g = _guards(rules)
    small = g["small_int_max"]
    found = []
    message = output.get("message") or ""
    lo, hi = 1, g["message_max_chars"]
    per_card = (g.get("message_chars") or {}).get(card)
    if per_card:
        lo, hi = per_card
    hard = hi if not per_card else int(hi * (1 + g["length_slack_pct"] / 100))
    if not message.strip():
        found.append(_issue("message_empty", "block", "Пустое сообщение"))
    elif len(message) > hard:
        found.append(_issue("message_long", "block", "Сообщение слишком длинное", chars=len(message), limit=hi))
    elif len(message) > hi or (not per_card and len(message) > g["message_soft_max_chars"]):
        found.append(_issue("message_longish", "warn", "Сообщение длиннее рекомендованного", chars=len(message),
                            limit=hi if per_card else g["message_soft_max_chars"]))
    elif len(message) < lo:
        found.append(_issue("message_short", "warn", "Сообщение короче рекомендованного", chars=len(message), limit=lo))

    # ids
    ids = _input_ids(card_input)
    out_items = [i for i in (output.get("items") or []) if isinstance(i, dict)]
    seen = set()
    for item in out_items:
        iid = str(item.get("id"))
        if iid not in ids:
            found.append(_issue("unknown_id", "block", f"Неизвестный id {iid}", id=iid))
        if iid in seen:
            found.append(_issue("duplicate_id", "block", f"id {iid} дважды", id=iid))
        seen.add(iid)
    rec = output.get("recommended_id")
    if rec is not None and str(rec) not in ids:
        found.append(_issue("unknown_recommended", "block", f"Рекомендован неизвестный id {rec}", id=str(rec)))
    cands = _candidate_ids(card_input)
    ups = [u for u in (output.get("upsell") or []) if isinstance(u, dict)]
    for u in ups:
        cid = str(u.get("candidate_id"))
        if cid not in cands:
            found.append(_issue("unknown_upsell", "block", f"Доп {cid} не из кандидатов", candidate_id=cid))
    if len(ups) > (rules or {}).get("max_upsell", 2):
        found.append(_issue("too_many_upsell", "block", "Слишком много допов", count=len(ups),
                            limit=(rules or {}).get("max_upsell", 2)))

    # numbers
    common = {k: v for k, v in card_input.items() if k != "items"}
    allowed_all = allowed_from(card_input)
    allowed_common = allowed_from(common)
    per_item = {}
    for item in card_input.get("items") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            a = allowed_from(item)
            a.nums |= allowed_common.nums
            a.times |= allowed_common.times
            a.dates |= allowed_common.dates
            per_item[str(item["id"])] = a
    for item in out_items:
        iid = str(item.get("id"))
        own = per_item.get(iid)
        for key in ITEM_TEXT_KEYS:
            for text in _texts(item.get(key)):
                invented = bad_numbers(text, allowed_all, small)
                for raw in invented:
                    found.append(_issue("invented_number", "block", f"Число «{raw}» не из входа", where=f"{iid}.{key}",
                                        token=raw))
                if own is not None:
                    for raw in bad_numbers(text, own, small):
                        if raw not in invented:
                            found.append(_issue("foreign_number", "warn", f"Число «{raw}» из другого варианта",
                                                where=f"{iid}.{key}", token=raw))
    rest = [message] + [u.get("line", "") for u in ups if isinstance(u.get("line"), str)]
    for item in out_items:
        for k, v in item.items():
            if k not in ITEM_TEXT_KEYS and k not in ID_KEYS:
                rest += list(_strings(v))
    for text in rest:
        for raw in bad_numbers(text, allowed_all, small):
            found.append(_issue("invented_number", "block", f"Число «{raw}» не из входа", where="message", token=raw))

    # questions
    q = question_count(message)
    limit = (rules or {}).get("max_questions", 2)
    asked = len(card_input.get("questions") or [])
    needs = g.get("question_mark_needs_questions")
    if q > limit:
        found.append(_issue("too_many_questions", "block", "Слишком много вопросов", count=q, limit=limit))
    elif q > asked and (needs is None or card in needs):
        found.append(_issue("extra_question", "warn", "Вопрос, которого движок не задавал", count=q, asked=asked))

    # tone
    all_text = "\n".join(_strings(output))
    sentences = _sentences(all_text)
    jokes = [s for s in sentences if _has_joke(s)]
    if len(jokes) > g["max_jokes"]:
        found.append(_issue("too_many_jokes", "warn", "Больше одной шутки", count=len(jokes), limit=g["max_jokes"]))
    for s in jokes:
        if MONEY_WORDS.search(s):
            found.append(_issue("joke_on_money", "warn", "Шутка рядом с деньгами, рисками или отменой", sentence=s[:120]))
            break
    if g.get("max_exclamations") is not None:
        ex = len(re.findall(r"!+", all_text))
        if ex > g["max_exclamations"]:
            found.append(_issue("too_many_exclamations", "warn", "Слишком много восклицаний", count=ex,
                                limit=g["max_exclamations"]))
    for m in RATIO.finditer(all_text):
        found.append(_issue("computed_claim", "warn", f"Вычисленное сравнение «{m.group(0)}» — его нет во входе",
                            phrase=m.group(0)))
    low = all_text.casefold()
    for phrase in g.get("banned_phrases") or []:
        if phrase.casefold() in low:
            found.append(_issue("banned_phrase", "block", f"Запрещённая фраза «{phrase}»", phrase=phrase))
    found += latin_issues(output, card_input, g)
    emo = emoji_count(all_text)
    if emo > g["max_emoji"]:
        found.append(_issue("too_many_emoji", "warn", "Слишком много эмодзи", count=emo, limit=g["max_emoji"]))
    return found
