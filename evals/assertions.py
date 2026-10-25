"""Level-2 assertions on one card output (AGENT_PLAN.md section 10).

    evaluate(assertion, output, case_input, card_schema) -> {"ok": bool, "type", "detail"}

Assertion types: schema, path_equals, path_in, path_absent, max_items, regex_absent,
numbers_subset, ids_subset, predicate (named function in PREDICATES, args in "args").
Paths are "/"-separated (see evals.common.resolve); "$text" in regex_absent means every
user-visible string of the output joined.
"""
import re

import jsonschema

from .common import MISSING, get_path, iter_strings, number_pool, numbers_in_text, resolve, same_number

ASSERTION_TYPES = ("schema", "path_equals", "path_in", "path_absent", "path_present", "any_of", "max_items", "regex_absent",
                   "numbers_subset", "ids_subset", "predicate")

# output keys whose strings are not shown to the user (ids)
NON_TEXT_KEYS = ("id", "recommended_id", "candidate_id", "kind", "ref", "intent")


def _res(ok, detail=""):
    return bool(ok), detail


def output_texts(output):
    """User-visible strings of a present card output (message, why, sacrifices, ... upsell lines)."""
    return list(iter_strings(output, skip_keys=NON_TEXT_KEYS))


def all_text(output):
    return "\n".join(output_texts(output))


def message_of(output):
    return output.get("message") or "" if isinstance(output, dict) else ""


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+|\n+", text or "") if s.strip()]


# ---------------------------------------------------------------- generic assertion types

def a_schema(a, output, inp, schema):
    if schema is None:
        return _res(False, "no card schema given")
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(output), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        return _res(False, f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}")
    return _res(True)


def _values(a, output):
    return [v for _, v in resolve(output, a["path"])]


def a_path_equals(a, output, inp, schema):
    vals = _values(a, output)
    if not vals:
        return _res(False, f"{a['path']} is absent")
    want = a["value"]
    bad = [v for v in vals if not _equal(v, want, a.get("casefold", False))]
    return _res(not bad, f"{a['path']} = {bad[0]!r}, want {want!r}" if bad else "")


def _equal(v, want, casefold):
    if casefold and isinstance(v, str) and isinstance(want, str):
        return v.casefold() == want.casefold()
    if isinstance(v, (int, float)) and isinstance(want, (int, float)) and not isinstance(v, bool):
        return same_number(v, want)
    return v == want


def a_path_in(a, output, inp, schema):
    vals = _values(a, output)
    if not vals:
        return _res(bool(a.get("allow_absent")), f"{a['path']} is absent")
    allowed = a["values"]
    cf = a.get("casefold", False)
    bad = [v for v in vals if not any(_equal(v, w, cf) for w in allowed)]
    return _res(not bad, f"{a['path']} = {bad[0]!r} not in {allowed!r}" if bad else "")


def a_path_absent(a, output, inp, schema):
    """Absent or null (an explicit null counts as absent)."""
    present = [(p, v) for p, v in resolve(output, a["path"]) if v is not None and v != [] and v != {}]
    return _res(not present, f"{present[0][0]} = {present[0][1]!r}" if present else "")


def a_path_present(a, output, inp, schema):
    """Set to something (null, [] and {} count as absent)."""
    present = [v for _, v in resolve(output, a["path"]) if v is not None and v != [] and v != {}]
    return _res(bool(present), "" if present else f"{a['path']} is absent")


def a_any_of(a, output, inp, schema):
    """Passes when one of `of` (assertions) passes: equivalent readings of one meaning."""
    details = []
    for sub in a["of"]:
        r = evaluate(sub, output, inp, schema)
        if r["ok"]:
            return _res(True)
        details.append(r["detail"])
    return _res(False, " | ".join(d for d in details if d)[:300])


def a_max_items(a, output, inp, schema):
    vals = _values(a, output)
    if not vals:
        return _res(True, "absent")
    for v in vals:
        n = len(v) if isinstance(v, (list, dict, str)) else 1
        if n > a["max"]:
            return _res(False, f"{a['path']} has {n} items > {a['max']}")
    return _res(True)


def a_regex_absent(a, output, inp, schema):
    path = a.get("path", "$text")
    text = all_text(output) if path == "$text" else "\n".join(
        s for _, v in resolve(output, path) for s in iter_strings(v))
    flags = re.IGNORECASE if a.get("ignore_case", True) else 0
    m = re.search(a["pattern"], text, flags)
    return _res(m is None, f"found {m.group(0)!r}" if m else "")


def a_numbers_subset(a, output, inp, schema):
    """Every number in the output's user-visible texts appears in the input.

    `small_ok` (default 3): integers 0..small_ok are always allowed (counts like «3 варианта»).
    `allow`: extra allowed numbers. `path`: restrict to a sub-tree of the output (default all texts).
    """
    pool = number_pool(inp)
    pool |= set(a.get("allow", []))
    # a decimal said whole («днём около 14°» for 14.1) is what the engine's number guard accepts too
    pool |= {float(round(p)) for p in pool if isinstance(p, float) and p != int(p)}
    small_ok = a.get("small_ok", 3)
    path = a.get("path")
    texts = output_texts(output) if not path else [s for _, v in resolve(output, path) for s in iter_strings(v)]
    bad = []
    for text in texts:
        for n in numbers_in_text(text):
            if isinstance(n, int) and 0 <= n <= small_ok:
                continue
            if not any(same_number(n, p) for p in pool):
                bad.append(n)
    return _res(not bad, f"numbers not in input: {sorted(set(bad))[:8]}" if bad else "")


def _input_ids(inp, card=None):
    ids = set()
    for item in inp.get("items") or []:
        if isinstance(item, dict) and "id" in item:
            ids.add(item["id"])
    return ids


def a_ids_subset(a, output, inp, schema):
    """items[*].id and recommended_id ⊂ input item ids; upsell[*].candidate_id ⊂ input candidates.

    `paths` may override: {"<output path>": "<input path>"}.
    """
    pairs = a.get("paths") or {"/items/*/id": "/items/*/id", "/recommended_id": "/items/*/id",
                               "/upsell/*/candidate_id": "/upsell_candidates/*/candidate_id"}
    for out_path, in_path in pairs.items():
        allowed = {v for _, v in resolve(inp, in_path)}
        for where, v in resolve(output, out_path):
            if v is None:
                continue
            if v not in allowed:
                return _res(False, f"{where} = {v!r} not among input ids {sorted(map(str, allowed))}")
    return _res(True)


def a_predicate(a, output, inp, schema):
    name = a["name"]
    fn = PREDICATES.get(name)
    if fn is None:
        return _res(False, f"unknown predicate {name}")
    return fn(output, inp, **(a.get("args") or {}))


TYPE_FUNCS = {
    "schema": a_schema, "path_equals": a_path_equals, "path_in": a_path_in, "path_absent": a_path_absent,
    "path_present": a_path_present, "any_of": a_any_of,
    "max_items": a_max_items, "regex_absent": a_regex_absent, "numbers_subset": a_numbers_subset,
    "ids_subset": a_ids_subset, "predicate": a_predicate,
}


def evaluate(assertion, output, case_input, card_schema=None):
    kind = assertion.get("type")
    fn = TYPE_FUNCS.get(kind)
    if fn is None:
        return {"ok": False, "type": kind, "detail": f"unknown assertion type {kind}"}
    try:
        ok, detail = fn(assertion, output, case_input, card_schema)
    except (KeyError, TypeError, ValueError, re.error) as exc:
        ok, detail = False, f"assertion error: {type(exc).__name__}: {exc}"
    label = assertion.get("name") if kind == "predicate" else assertion.get("path")
    return {"ok": ok, "type": kind, "label": label, "detail": detail}


def evaluate_all(assertions, output, case_input, card_schema=None):
    results = [evaluate(a, output, case_input, card_schema) for a in assertions]
    return all(r["ok"] for r in results), results


# ---------------------------------------------------------------- named predicates
# Signature: fn(output, case_input, **args) -> (ok: bool, detail: str)

def patch_has_no_assumptions(output, inp, allowed, allow_unset=False):
    """The understand patch sets only paths the user actually said (`allowed`), unsets nothing."""
    patch = output.get("patch") or {}
    sets = set((patch.get("set") or {}).keys())
    extra = sorted(sets - set(allowed))
    if extra:
        return _res(False, f"patch sets assumed paths {extra}")
    if not allow_unset and patch.get("unset"):
        return _res(False, f"patch unsets {patch['unset']}")
    return _res(True)


def patch_empty(output, inp):
    patch = output.get("patch") or {}
    if patch.get("set") or patch.get("unset"):
        return _res(False, f"patch is not empty: {patch}")
    return _res(True)


def contains(output, inp, path, values, mode="all"):
    """The list at `path` contains all (mode=all) / any (mode=any) of `values`."""
    got = get_path(output, path, None)
    if not isinstance(got, list):
        return _res(False, f"{path} is not a list: {got!r}")
    hits = [v for v in values if v in got]
    ok = len(hits) == len(values) if mode == "all" else bool(hits)
    return _res(ok, f"{path} = {got!r}, want {mode} of {values!r}" if not ok else "")


def number_between(output, inp, path, min=None, max=None, allow_absent=False):
    got = get_path(output, path, MISSING)
    if got is MISSING or got is None:
        return _res(allow_absent, f"{path} is absent")
    if not isinstance(got, (int, float)) or isinstance(got, bool):
        return _res(False, f"{path} is not a number: {got!r}")
    if min is not None and got < min:
        return _res(False, f"{path} = {got} < {min}")
    if max is not None and got > max:
        return _res(False, f"{path} = {got} > {max}")
    return _res(True)


def matches(output, inp, path, pattern, ignore_case=True):
    """The string at `path` matches the regex (search)."""
    got = get_path(output, path, None)
    if not isinstance(got, str):
        return _res(False, f"{path} is not a string: {got!r}")
    ok = re.search(pattern, got, re.IGNORECASE if ignore_case else 0) is not None
    return _res(ok, f"{path} = {got!r} does not match {pattern!r}" if not ok else "")


def refs_include(output, inp, kind, ref):
    refs = output.get("refs") or []
    ok = any(r.get("kind") == kind and str(r.get("ref")) == str(ref) for r in refs)
    return _res(ok, f"refs {refs!r} lack {kind}:{ref}" if not ok else "")


def asks_at_most(output, inp, n=1):
    """The message asks at most `n` questions of its own (sentences ending with '?')."""
    count = sum(1 for s in sentences(message_of(output)) if s.endswith("?"))
    return _res(count <= n, f"message asks {count} questions > {n}" if count > n else "")


def mentions_any(output, inp, words, path="/message"):
    """Some text (default: message; "$text": all texts) mentions one of `words` (regexes)."""
    text = all_text(output) if path == "$text" else str(get_path(output, path, "") or "")
    for w in words:
        if re.search(w, text, re.IGNORECASE):
            return _res(True)
    return _res(False, f"none of {words!r} in {path}")


def mentions_risk(output, inp, keywords=None, item_ids=None):
    """Every input item with computed risks keeps at least one risk in the output item (or the
    message mentions one of `keywords`)."""
    out_items = {i.get("id"): i for i in output.get("items") or []}
    msg = message_of(output)
    for item in inp.get("items") or []:
        if item_ids and item["id"] not in item_ids:
            continue
        if not item.get("risks"):
            continue
        out = out_items.get(item["id"])
        if out and out.get("risks"):
            continue
        if keywords and any(re.search(k, msg, re.IGNORECASE) for k in keywords):
            continue
        return _res(False, f"risk of {item['id']} not mentioned")
    return _res(True)


def keeps_notes(output, inp, field="not_for", item_ids=None):
    """Every input item with a non-empty `field` (sacrifices/risks/not_for) keeps one in the output."""
    out_items = {i.get("id"): i for i in output.get("items") or []}
    for item in inp.get("items") or []:
        if item_ids and item["id"] not in item_ids:
            continue
        if item.get(field) and item["id"] in out_items and not out_items[item["id"]].get(field):
            return _res(False, f"{field} of {item['id']} dropped")
        if item.get(field) and item["id"] not in out_items and item_ids:
            return _res(False, f"{item['id']} missing from output items")
    return _res(True)


def recommended_in_items(output, inp, allow_null=False):
    rec = output.get("recommended_id")
    if rec is None:
        return _res(allow_null, "recommended_id is null")
    ids = {i.get("id") for i in output.get("items") or []} | _input_ids(inp)
    return _res(rec in ids, f"recommended_id {rec!r} not in items" if rec not in ids else "")


def no_upsell_without_candidate(output, inp):
    """Upsell lines only for input candidates; none at all when the input has none."""
    cands = {c.get("candidate_id") for c in inp.get("upsell_candidates") or []}
    ups = output.get("upsell") or []
    if not cands and ups:
        return _res(False, f"{len(ups)} upsell lines without candidates")
    bad = [u.get("candidate_id") for u in ups if u.get("candidate_id") not in cands]
    if bad:
        return _res(False, f"unknown candidates {bad}")
    if not cands:
        text = all_text(output)
        m = re.search(r"страховк|докупит|добавьте .*за \d", text, re.IGNORECASE)
        if m:
            return _res(False, f"upsell-like text without candidates: {m.group(0)!r}")
    return _res(True)


BUREAUCRATESE = [
    r"\bданн(ый|ая|ое|ые|ого|ой|ом)\b", r"осуществл", r"\bв рамках\b", r"\bявляется\b", r"производится",
    r"\bв целях\b", r"\bуважаем", r"\bнастоящим\b", r"\bв соответствии с\b", r"предоставляем",
    r"не представляется возможным", r"вышеуказанн", r"нижеследующ", r"\bв связи с\b", r"ваш запрос (обработан|принят)",
    r"\bдоводим до вашего сведения\b", r"\bпросим учесть\b", r"\bпо вопросу\b",
]


def tone_no_bureaucratese(output, inp, extra=None):
    text = all_text(output)
    for pat in BUREAUCRATESE + list(extra or []):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _res(False, f"bureaucratese: {m.group(0)!r}")
    return _res(True)


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F]")
SMILEY_RE = re.compile(r"[:;]-?[)D]|\){2,}|(?<=\s)\)(?=\s|$)|\bха-?ха\b|\bшучу\b", re.IGNORECASE)


def joke_markers(text):
    return EMOJI_RE.findall(text) + SMILEY_RE.findall(text)


def at_most_one_joke(output, inp, n=1):
    """Heuristic: emoji, smileys and «шучу»/«хаха» count as jokes; at most `n` per reply."""
    marks = joke_markers(all_text(output))
    return _res(len(marks) <= n, f"{len(marks)} joke markers: {marks[:5]}" if len(marks) > n else "")


MONEY_RISK_RE = re.compile(r"₽|руб|отмен|штраф|невозврат|риск|пересадк|ночью|поздн", re.IGNORECASE)
RISK_ONLY_RE = re.compile(r"штраф|невозврат|риск|пересадк|ночью|ночной|поздн|нюанс|без обмена|не вернут|отмен", re.IGNORECASE)
FREE_CANCEL_RE = re.compile(r"бесплатн\w*\s+отмен\w*|отмен\w*\s+бесплатн\w*", re.IGNORECASE)


def money_risks_plain(output, inp):
    """Money and risks are said plainly: no text jokes or smileys next to money or a risk, and no emoji at all
    in a sentence that states a risk. A decorative emoji next to a price or good news («бесплатная отмена»)
    is not a joke."""
    for s in sentences(all_text(output)):
        if MONEY_RISK_RE.search(s) and SMILEY_RE.findall(s):
            return _res(False, f"joke next to money/risk: {s[:120]!r}")
        if RISK_ONLY_RE.search(FREE_CANCEL_RE.sub("", s)) and EMOJI_RE.findall(s):
            return _res(False, f"emoji next to a risk: {s[:120]!r}")
    return _res(True)


LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
LATIN_OK = {"wi-fi", "wifi", "spa", "ok", "pnr", "all", "inclusive", "ultra", "check-in", "check-out"}


def russian_only(output, inp, min_cyrillic=0.85, allow=None):
    """User-visible text is Russian: Latin words only when they appear in the input (names,
    carriers, IATA codes) or in a small allow-list; Cyrillic share of letters ≥ min_cyrillic."""
    text = all_text(output)
    if not text.strip():
        return _res(False, "no text")
    in_text = " ".join(iter_strings(inp)).casefold()
    allowed = LATIN_OK | {w.casefold() for w in (allow or [])}
    bad = [w for w in LATIN_WORD_RE.findall(text) if w.casefold() not in allowed and w.casefold() not in in_text]
    if bad:
        return _res(False, f"Latin words not from input: {bad[:6]}")
    letters = [c for c in text if c.isalpha()]
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    share = cyr / len(letters) if letters else 0
    return _res(share >= min_cyrillic, f"cyrillic share {share:.2f} < {min_cyrillic}" if share < min_cyrillic else "")


def max_chars(output, inp, n=600, path="/message"):
    text = str(get_path(output, path, "") or "")
    return _res(len(text) <= n, f"{path} has {len(text)} chars > {n}" if len(text) > n else "")


NO_DATA_RE = r"нет данных|не знаю|не нашл|нет в демо|в демо нет|нет информации|не могу сказать|не подскаж|неизвестн|не располага|нет цены|цены пока нет|не смог"


def admits_no_data(output, inp, path="/message", pattern=NO_DATA_RE):
    """The text says plainly that data is missing (honest «нет данных»)."""
    text = all_text(output) if path == "$text" else str(get_path(output, path, "") or "")
    ok = re.search(pattern, text, re.IGNORECASE) is not None
    return _res(ok, "no honest 'no data' statement" if not ok else "")


def null_price_not_invented(output, inp):
    """For destination items with price_from_rub null the output writes no ₽ amount in their texts."""
    nulls = {i["id"] for i in inp.get("items") or [] if i.get("price_from_rub") is None}
    for item in output.get("items") or []:
        if item.get("id") in nulls:
            text = " ".join(iter_strings(item, skip_keys=NON_TEXT_KEYS))
            if re.search(r"\d[\d \u00a0\u202f]*\s*(₽|руб)", text):
                return _res(False, f"price written for {item['id']} without a quote")
    return _res(True)


def pet_recorded(output, inp, pet="dog"):
    """A pet is recorded either in party.pets or as constraints.add pet:<pet>."""
    sets = (output.get("patch") or {}).get("set") or {}
    pets = [str(p).casefold() for p in sets.get("party.pets") or []]
    words = {"dog": ("dog", "собака", "пёс", "пес", "собак"), "cat": ("cat", "кошка", "кот")}.get(pet, (pet,))
    if any(any(w in p for w in words) for p in pets):
        return _res(True)
    if f"pet:{pet}" in (sets.get("constraints.add") or []):
        return _res(True)
    return _res(False, f"pet {pet} not recorded: {sets}")


def any_of(output, inp, assertions):
    """At least one sub-assertion holds."""
    details = []
    for sub in assertions:
        r = evaluate(sub, output, inp)
        if r["ok"]:
            return _res(True)
        details.append(r["detail"])
    return _res(False, " | ".join(details))


def none_of(output, inp, assertions):
    """No sub-assertion holds (negation)."""
    for sub in assertions:
        r = evaluate(sub, output, inp)
        if r["ok"]:
            return _res(False, f"forbidden condition holds: {sub}")
    return _res(True)


def no_unbacked_claims(output, inp, card="present.variants"):
    """The code guard of claims holds: no «убрал ночные / с кухней / прямые / перенёс / пересчитал / нет аэропорта /
    билеты не нашлись» without this turn's evidence and no button or rebuild promised without the means
    (agent.guards.change_claim_issues: change_claim / promise_claim)."""
    from agent.guards import change_claim_issues
    found = [i["message"] for i in change_claim_issues(card, inp, output) if i["severity"] == "block"]
    return _res(not found, "; ".join(found[:3]))


PREDICATES = {
    "no_unbacked_claims": no_unbacked_claims,
    "patch_has_no_assumptions": patch_has_no_assumptions,
    "patch_empty": patch_empty,
    "contains": contains,
    "number_between": number_between,
    "matches": matches,
    "refs_include": refs_include,
    "asks_at_most": asks_at_most,
    "mentions_any": mentions_any,
    "mentions_risk": mentions_risk,
    "keeps_notes": keeps_notes,
    "recommended_in_items": recommended_in_items,
    "no_upsell_without_candidate": no_upsell_without_candidate,
    "tone_no_bureaucratese": tone_no_bureaucratese,
    "at_most_one_joke": at_most_one_joke,
    "money_risks_plain": money_risks_plain,
    "russian_only": russian_only,
    "max_chars": max_chars,
    "admits_no_data": admits_no_data,
    "null_price_not_invented": null_price_not_invented,
    "pet_recorded": pet_recorded,
    "any_of": any_of,
    "none_of": none_of,
}
