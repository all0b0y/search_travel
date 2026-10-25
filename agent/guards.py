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
  "вдвое", "в 2 раза", "на треть", "на 20% дешевле" are computed claims (block `computed_claim`: the model
  computed a comparison the input does not have, d2 «втрое дешевле»);
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
# «убрал хостелы» is a claim about what code changed: only with a changes row that added constraint no_hostel;
# «без хостелов» only when the brief has no_hostel (the search and ranking then drop every hostel)
HOSTEL_REMOVED_RE = re.compile(r"(?:убрал|убрали|убраны|исключил|исключены|отсеял|выкинул)\w*\s+(?:все\s+|из\s+подборки\s+)?хостел",
                               re.I)
HOSTEL_WITHOUT_RE = re.compile(r"\bбез\s+хостел", re.I)


def hostel_claim_issues(card, card_input, message):
    if card not in ("present.update", "present.variants") or "хостел" not in message.casefold():
        return []
    added = any(isinstance(r, dict) and r.get("field") == "constraints" and "no_hostel" in (r.get("after") or [])
                and "no_hostel" not in (r.get("before") or []) for r in card_input.get("changes") or [])
    has = "no_hostel" in ((card_input.get("brief") or {}).get("constraints") or [])
    out = []
    m = HOSTEL_REMOVED_RE.search(message)
    if m and not added:
        out.append(_issue("hostel_claim", "block", f"«{m.group(0)}» — код хостелы в этот раз не убирал",
                          phrase=m.group(0)))
    m = HOSTEL_WITHOUT_RE.search(message)
    if m and not has:
        out.append(_issue("hostel_claim", "block", f"«{m.group(0)}» — в запросе нет условия «без хостелов»",
                          phrase=m.group(0)))
    return out


# ---------------------------------------------------------------- claims about what code did (table-driven)
# A reply may say it removed / added / moved something, or that the variants now have a property, only when this
# turn's evidence holds: the `changes` rows (present.update) and the shown items' facts. Each row: (kind, regex,
# scope) — scope "state": a property of the items (some / all when the sentence says «все», «везде», «оба»);
# "removed": code dropped it this turn; "change": a changes row of that field; "negative": a «нет …» claim.
# A sentence with a negation («нет», «не », «ничего», «невозможно», «никак») is no positive claim («с кухней ничего
# нет», «прямыми рейсами туда не долететь», «прямыми туда никак» are honest); a kind may have its own negations
# (CLAIM_KIND_NEG_RE: «без прямых», «только с пересадкой» deny a direct flight).
CLAIM_NEG_RE = re.compile(r"\bнет\b|\bнету\b|\bне\s|\bни\b|ничего|нельзя|невозможн|\bникак|\bникто\b|\bнигде\b|"
                          r"отсутству|без\s+не[её]\b|без\s+него\b", re.I)
CLAIM_NEG_STRICT_RE = re.compile(r"\bне\s+(?:смог|получил|вышло|удалось|убрал|нашл|получится|выйдет)|ничего|нельзя|"
                                 r"невозможн|\bникак", re.I)
CLAIM_KIND_NEG_RE = {
    "direct": re.compile(r"без\s+прям|только\s+(?:рейс\w*\s+)?(?:с\s+)?(?:пересадк|стыковк)|"
                         r"(?:все|одни|лишь)\s+(?:рейсы\s+)?с\s+пересадк|с\s+пересадк\w*\s+(?:—\s*|-\s*)?только", re.I),
}
CLAIM_ALL_RE = re.compile(r"\b(?:все|всех|везде|каждом|каждый|оба|обоих|обе)\b", re.I)
TRANSPORT_WORDS_RE = re.compile(r"поезд|рейс|перел[её]т|дорог|отправлен|прибыти|вылет|едете|летите|в\s+пути", re.I)
CLAIMS = (
    ("night_removed", re.compile(r"(?:убрал|убрали|убраны|исключил|отсеял|выкинул)\w*\s+(?:все\s+)?ночн|"
                                 r"ночн\w*\s+(?:рейс\w*|поезд\w*|перее\w*)\s+(?:я\s+)?(?:убрал|исключил)\w*|"
                                 r"ночн\w*\s+(?:рейсов|поездов)\s+больше\s+нет", re.I), "removed"),
    ("night", re.compile(r"без\s+ночн\w*\s+(?:рейс|поезд|перел|перее|дорог)|только\s+дневн\w*\s+(?:рейс|поезд)|"
                         r"\b(?:все|оба|обе|везде|теперь)\b[^.!?]{0,50}?\bдн[её]м\b", re.I), "state"),
    ("kitchen", re.compile(r"\bс\s+кухн|\bкухн\w*\s+в\s+номер|\bесть\s+(?:своя\s+|собственная\s+)?кухн|"
                           r"\bкухн\w*\s+есть|\bс\s+кухоньк", re.I), "state"),
    ("cot", re.compile(r"\bс\s+(?:детской\s+)?кроватк|\bкроватк\w*\s+(?:есть|будет|гарантир|бесплатн|включен)|"
                       r"\bесть\s+(?:детская\s+)?кроватк", re.I), "state"),
    ("step_free", re.compile(r"без\s+ступен|без\s+лестниц|безбарьер|доступ\w*\s+для\s+(?:коляс|инвалид)|"
                             r"(?:можно|удобно)\s+(?:проехать\s+|заехать\s+)?(?:с|на)\s+коляск", re.I), "state"),
    ("lift", re.compile(r"\bесть\s+лифт|\bс\s+лифтом|\bлифт\s+есть", re.I), "state"),
    ("direct", re.compile(r"\bпрям(?:ой|ые|ым|ыми|ых|ого)\s+(?:рейс|перел)|без\s+пересад", re.I), "state"),
    ("dates_moved", re.compile(r"перен[её]с\w*\s+(?:поездку\s+|даты\s+|всё\s+)?на\b|"
                               r"сдвинул\w*\s+(?:поездку\s+|даты\s+|всё\s+)?на\b|сдвинул\w*\s+даты", re.I), "change"),
    ("party", re.compile(r"пересчитал\w*\s+(?:цены\s+|всё\s+|варианты\s+)?на\b", re.I), "change"),
    ("no_airport", re.compile(r"нет\s+(?:своего\s+|собственного\s+)?аэропорт|аэропорт\w*\s+(?:там\s+|здесь\s+|у\s+\w+\s+)?нет|"
                              r"без\s+аэропорт", re.I), "negative"),
    ("no_tickets", re.compile(r"билет\w*\s+не\s+нашл|не\s+(?:смотрел|искал)\w*\s+(?:ещё\s+)?билет|"
                              r"билет\w*\s+(?:я\s+)?(?:пока\s+|ещё\s+)?не\s+(?:смотрел|искал)", re.I), "negative"),
)
# offers of a rebuild the engine cannot make: no searchable data behind these words
REBUILD_OFFER_RE = re.compile(r"пересоберу|подберу|поищу|найду|соберу|могу\s+(?:подобрать|поискать|найти|пересобрать|"
                              r"собрать)|скажите\s*[—-]", re.I)
NOT_SEARCHABLE_RE = re.compile(r"вид\w*\s+на\s+(?:мор|гор|город)|балкон|бассейн|парковк|\bспа\b|саун|тренаж|джакузи|"
                               r"камин|террас|кондиционер|аквапарк|анимаци|песчан\w*\s+пляж|wi-?fi|вайфа", re.I)
AMENITY_WORDS = {"constraints.kitchen": re.compile(r"кухн", re.I), "constraints.crib": re.compile(r"кроватк", re.I),
                 "constraints.accessible": re.compile(r"ступен|лестниц|коляск|безбарьер", re.I)}
ONE_TAP_RE = re.compile(r"одной\s+кнопк|(?:добавлю|докину|подкину)\s+(?:и\s+)?(?:дорогу|перел[её]т|билеты|поезд)", re.I)


def _items(card_input):
    return [i for i in card_input.get("items") or [] if isinstance(i, dict)]


def _facts_of(item):
    return item.get("facts") or {}


def _night_item(item):
    return bool(_facts_of(item).get("out_night") or _facts_of(item).get("back_night"))


def _has_legs(item):
    return any(st.get("type") in ("flight", "train") for st in item.get("steps") or [] if isinstance(st, dict))


def _direct_item(item):
    f = _facts_of(item)
    return _has_legs(item) and all((f.get(f"{d}_stops") or 0) == 0 for d in ("out", "back") if f"{d}_kind" in f)


STATE_OK = {
    "night": lambda i: _has_legs(i) and not _night_item(i),
    "kitchen": lambda i: _facts_of(i).get("kitchen") is True,
    "cot": lambda i: _facts_of(i).get("infant_cot") in ("free", "paid"),
    "step_free": lambda i: _facts_of(i).get("step_free") is True,
    "lift": lambda i: _facts_of(i).get("lift") is True,
    "direct": _direct_item,
}



def _changed(card_input, prefix):
    return any(isinstance(r, dict) and str(r.get("field") or "").startswith(prefix) and r.get("before") != r.get("after")
               for r in card_input.get("changes") or [])


def _claim_problem(kind, scope, sentence, items, card, card_input, own=None):
    """None when the claim holds; else why it does not (a short Russian reason)."""
    if kind == "night_removed":
        if any(_night_item(i) for i in items):
            return "в подборке ещё есть ночной рейс или поезд"
        if card == "present.update" and not (_changed(card_input, "prefs.no_night_flights")
                                             or _changed(card_input, "constraints")):
            return "код в этот раз ночную дорогу не убирал"
        prefs = (card_input.get("brief") or {}).get("prefs") or {}
        if card != "present.update" and not prefs.get("no_night_flights"):
            return "в запросе нет условия «без ночных»"
        return None
    if kind == "night" and not TRANSPORT_WORDS_RE.search(sentence) and "ночн" not in sentence.casefold():
        return None
    if scope == "state":
        pool = [own] if own is not None else items
        ok = [i for i in pool if STATE_OK[kind](i)]
        if kind == "night":   # «без ночных»: every shown variant with a road must be by day
            bad = [i for i in pool if _has_legs(i) and _night_item(i)]
            return "в подборке есть ночной рейс или поезд" if bad or not ok else None
        if not ok:
            return "ни у одного варианта этого нет в данных"
        if own is None and CLAIM_ALL_RE.search(sentence) and len(ok) < len(pool):
            return "это есть не у всех вариантов"
        return None
    if kind == "dates_moved":
        moved = _changed(card_input, "dates.") or any(
            _facts_of(i).get("dates_shifted_days") or _facts_of(i).get("nights_changed_from")
            or _facts_of(i).get("budget_fit") in ("dates", "dates_and_hotel") for i in items)
        return None if moved else "даты в этот раз не переносились"
    if kind == "party":
        return None if _changed(card_input, "party.") else "состав в этот раз не менялся"
    if kind == "no_airport":
        facts = [_facts_of(i) for i in items] + [f for f in card_input.get("facts") or [] if isinstance(f, dict)]
        return None if any(f.get("dest_airports") is False for f in facts) else "у места в данных есть аэропорт"
    if kind == "no_tickets":
        searched = any(_facts_of(i).get("transport_gap") for i in items) or any(
            isinstance(f, dict) and f.get("kind") == "transport_search" for f in card_input.get("facts") or [])
        return "код искал дорогу — скажите, что нашлось" if searched else None
    return None


def change_claim_issues(card, card_input, output):
    """Block `change_claim` for every claim of the CLAIMS table without this turn's evidence; `promise_claim` for an
    offer the response has no means for (one tap that adds the road without an add_road offer, a rebuild by a
    property no data has, a rebuild by an amenity code already found nowhere); `budget_claim` for «ничего не
    укладывается» while code marked items within the budget; `long_train_claim` for a day-long train leg written as
    decimal hours, or a recommended trip that leaves days before check-in without the leave day."""
    if card == "present.destinations":
        return budget_claim_issues(card, card_input, output) + long_train_claim_issues(card, card_input, output)
    if card not in ("present.variants", "present.update", "present.compare", "present.info"):
        return []
    items = _items(card_input)
    by_id = {str(i.get("id")): i for i in items}
    texts = [(output.get("message") or "", None)]
    for it in output.get("items") or []:
        if isinstance(it, dict):
            for key in ITEM_TEXT_KEYS:
                texts += [(t, by_id.get(str(it.get("id")))) for t in _texts(it.get(key))]
    out, seen = [], set()
    for text, own in texts:
        for sentence in _sentences(text):
            for kind, rx, scope in CLAIMS:
                if card == "present.info" and scope != "negative":
                    continue
                m = rx.search(sentence)
                if not m:
                    continue
                if scope == "state" and (CLAIM_NEG_RE.search(sentence) or (
                        kind in CLAIM_KIND_NEG_RE and CLAIM_KIND_NEG_RE[kind].search(sentence))):
                    continue
                if scope in ("removed", "change") and CLAIM_NEG_STRICT_RE.search(sentence):
                    continue
                why = _claim_problem(kind, scope, sentence, items, card, card_input, own)
                if why and (kind, m.group(0)) not in seen:
                    seen.add((kind, m.group(0)))
                    out.append(_issue("change_claim", "block", f"«{m.group(0)}» — {why}", kind=kind,
                                      phrase=m.group(0)))
            out += _promise_issues(sentence, card, card_input, items)
    out += budget_claim_issues(card, card_input, output) + long_train_claim_issues(card, card_input, output)
    return out


# «В 25 000 ₽ ничего не укладывается» (or «почти ничего») while code marked places within the budget
# (destinations items, or the variants' other_places) — the card must name those places instead.
BUDGET_NONE_RE = re.compile(r"(?:почти\s+)?ничего\s+(?:из\s+\w+\s+)?не\s+(?:уклад|влеза|помеща|вписыва)\w*|"
                            r"ни\s+одно\w*\s+(?:\w+\s+){0,2}не\s+(?:уклад|влеза|вписыва)\w*|"
                            r"не\s+(?:уклад|влеза|вписыва)\w*\s+(?:\w+\s+){0,2}(?:ни\s+одно|ничего)|"
                            r"в\s+(?:этот\s+|ваш\s+)?бюджет\w*\s+(?:пока\s+|почти\s+)?ничего", re.I)
PLACE_WORDS_RE = re.compile(r"мест|направлен|город|куда", re.I)


def _output_texts(output):
    texts = [output.get("message") or ""]
    for it in output.get("items") or []:
        if isinstance(it, dict):
            for key in ITEM_TEXT_KEYS:
                texts += _texts(it.get(key))
    return texts


def budget_claim_issues(card, card_input, output):
    if card == "present.destinations":
        fits = [i for i in _items(card_input) if _facts_of(i).get("within_budget") is True]
        need_place_word = False
    elif card in ("present.variants", "present.update"):
        fits = [p for p in card_input.get("other_places") or [] if isinstance(p, dict) and p.get("within_budget") is True]
        need_place_word = True   # the variants themselves may be above the budget: only a claim about places
    else:
        return []
    if not fits:
        return []
    names = ", ".join(str(i.get("name")) for i in fits[:3])
    for text in _output_texts(output):
        for sentence in _sentences(text):
            m = BUDGET_NONE_RE.search(sentence)
            if m and (not need_place_word or PLACE_WORDS_RE.search(sentence)):
                return [_issue("budget_claim", "block", f"«{m.group(0)}» — в бюджет укладываются: {names}",
                               kind="nothing_fits", phrase=m.group(0))]
    return []


# a long-distance train (facts out|back_long_train, destinations facts.long_train): its time on board is said as
# code wrote it (on_road_ru «1 сутки 3 ч»), never as decimal hours («24,1 ч»), and a recommended trip that leaves
# days before check-in names the leave day (out_road_ru) or «за день / за два дня до заезда»
LONG_LEG_HOURS = 18
DECIMAL_HOURS_RE = re.compile(r"(?<![\d.,])(\d{2,3}[.,]\d)\s*(?:ч\b|ч\.|час)", re.I)
LEAVE_WORDS_RE = re.compile(r"за\s+(?:один\s+|1\s+)?(?:день|сутки|два\s+дня|2\s+дня|три\s+дня|3\s+дня|двое\s+суток)\s+до\s+"
                            r"(?:заезда|заселения)|накануне\s+(?:заезда|заселения)", re.I)
LEAVE_DATE_RE = re.compile(r"отправление\s+(\d{1,2}\s+[а-яё]+)", re.I)


def _long_item(item):
    f = _facts_of(item)
    return bool(f.get("out_long_train") or f.get("back_long_train") or f.get("long_train"))


def long_train_claim_issues(card, card_input, output):
    items = _items(card_input)
    if not any(_long_item(i) for i in items):
        return []
    out = []
    for text in _output_texts(output):
        for m in DECIMAL_HOURS_RE.finditer(text):
            if _f(m.group(1)) >= LONG_LEG_HOURS:
                out.append(_issue("long_train_claim", "block", f"«{m.group(0)}» — поезд в пути больше суток: "
                                  f"скажите время в пути словами из on_road_ru", kind="decimal_hours",
                                  phrase=m.group(0)))
                break
        if out:
            break
    if card in ("present.variants", "present.update"):
        rec = str(output.get("recommended_id") or card_input.get("recommended_hint") or "")
        item = next((i for i in items if str(i.get("id")) == rec), None)
        f = _facts_of(item) if item else {}
        if f.get("out_days_before_checkin") and f.get("out_road_ru"):
            said = [output.get("message") or ""] + [t for it in output.get("items") or [] if isinstance(it, dict)
                                                   and str(it.get("id")) == rec for k in ITEM_TEXT_KEYS
                                                   for t in _texts(it.get(k))]
            text = " ".join(said)
            m = LEAVE_DATE_RE.search(f["out_road_ru"])
            leave = m.group(1) if m else None
            if not (LEAVE_WORDS_RE.search(text) or (leave and leave.casefold() in text.casefold())):
                out.append(_issue("long_train_claim", "block", f"Поезд туда уходит за {f['out_days_before_checkin']} "
                                  f"дн. до заезда — назовите день выезда ({leave or 'из out_road_ru'})",
                                  kind="leave_day", leave=leave))
    return out


def _promise_issues(sentence, card, card_input, items):
    if card not in ("present.variants", "present.update"):
        return []
    offers = {o.get("kind") for o in card_input.get("offers") or [] if isinstance(o, dict)}
    out = []
    m = ONE_TAP_RE.search(sentence)
    if m and "add_road" not in offers:
        out.append(_issue("promise_claim", "block", f"«{m.group(0)}» — такой кнопки в ответе нет", kind="add_road",
                          phrase=m.group(0)))
    if REBUILD_OFFER_RE.search(sentence):
        m = NOT_SEARCHABLE_RE.search(sentence)
        if m:
            out.append(_issue("promise_claim", "block", f"«{m.group(0)}» — по этому код искать не умеет",
                              kind="not_searchable", phrase=m.group(0)))
        relaxed = {(n.get("data") or {}).get("field") for i in items for n in i.get("risks") or [] if isinstance(n, dict)
                   and n.get("code") == "relaxed_pref"}
        for field, rx in AMENITY_WORDS.items():
            m = rx.search(sentence) if field in relaxed else None
            if m:
                out.append(_issue("promise_claim", "block", f"«{m.group(0)}» — код уже искал и не нашёл",
                                  kind="exhausted", phrase=m.group(0)))
    return out


MONEY_WORDS = re.compile(r"₽|руб|\bцен|стоим|отмен|штраф|невозврат|риск|стыковк|опозда|доплат", re.I)
JOKE_WORDS = re.compile(r"шучу|хаха|ахах|хех|\bлол\b|😄|😂|🤣|😅|😜|😉|🙃|😏|😎|🤪", re.I)
SMILEY = re.compile(r"[:;]-?\)|\){2,}|\(\s*:")

_SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2009\u2007\u2002\u2003"), " ")
_SCALES = [(re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:млн\.?|миллион\w*)", re.I), 1_000_000),
           (re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?:\s*(?:тыс\.?|тысяч\w*)|(?:к|k)\b)", re.I), 1_000)]
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_ISO_TIME = re.compile(r"(?<=\d)T([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
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
    text = _ISO_TIME.sub(time_, text)   # «…T07:55:00+02:00»: the clock time, not the seconds or the offset
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
        for v in (x, abs(x)):   # «−3,2°» for an input -3.2: the sign in the text is not part of the token
            self.nums.add(round(v, 6))
            self.nums.add(float(round(v)))
            self.nums.add(round(v, 1))

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
    singles = {s.casefold() for s in _strings(card_input) if re.fullmatch(r"[a-z][a-z_\-]*", s)} - allowed
    in_names = {w.casefold() for s in _strings(card_input) if " " in s.strip() for w in LATIN_WORD.findall(s)}
    codes = singles - in_names
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
        found.append(_issue("computed_claim", "block", f"Вычисленное сравнение «{m.group(0)}» — его нет во входе",
                            phrase=m.group(0)))
    low = all_text.casefold()
    for phrase in g.get("banned_phrases") or []:
        if phrase.casefold() in low:
            found.append(_issue("banned_phrase", "block", f"Запрещённая фраза «{phrase}»", phrase=phrase))
    found += latin_issues(output, card_input, g)
    found += hostel_claim_issues(card, card_input, message)
    found += change_claim_issues(card, card_input, output)
    emo = emoji_count(all_text)
    if emo > g["max_emoji"]:
        found.append(_issue("too_many_emoji", "warn", "Слишком много эмодзи", count=emo, limit=g["max_emoji"]))
    return found
