"""Turn a sealed benchmark version written by an outside model into the runner's formats, and check its form.

    .venv/bin/python -m evals.bench.convert evals/bench/v1

Reads <version>/source/cases.json (one message -> expected reading, the short format of BRIEF.md) and
<version>/source/scenarios/*.json (persona scenarios, already in the runner's format). Writes
<version>/cases/*.json (level-2 understand cases) and <version>/scenarios/*.json (copies). Prints every
problem of form (unknown check, wrong argument, a place outside the catalog, a date outside the window,
a home city without airports) and exits 1 if there is one: fix the source by hand and log the edit in
<version>/README.md. Content is never changed here.
"""
import inspect
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from evals.common import ROOT, read_json, write_json
from evals.predicates import PREDICATES
from evals.run import HOME_IATA

TODAY = "2026-10-25"
WINDOW = ("2026-11-01", "2027-06-30")
SHOWN = [{"label": "1", "kind": "variant", "id": "cheapest", "title": "Дешевле"},
         {"label": "2", "kind": "variant", "id": "fastest", "title": "Быстрее"},
         {"label": "3", "kind": "variant", "id": "comfort", "title": "Комфортнее"}]
PREFS = {"meals": "any", "free_cancellation": None, "stars_min": None, "direct_only": None, "max_travel_hours": None,
         "district": "any", "beach_max_m": None, "transport": "any"}


def catalog():
    con = sqlite3.connect(ROOT / "data" / "opendata.sqlite")
    rows = con.execute("select id, name_ru, country from destinations").fetchall()
    return {name: (did, country) for did, name, country in rows}


def segment(adults, ages):
    if ages:
        return "family"
    return {1: "solo", 2: "couple"}.get(adults, "friends")


def selection_brief(base, places, problems, cid):
    dest = places.get(base["destination"])
    if dest is None:
        problems.append(f"{cid}: base destination {base['destination']!r} is not in the catalog")
        dest = (None, None)
    if base["origin"] not in HOME_IATA:
        problems.append(f"{cid}: base origin {base['origin']!r} is not a home city")
    start, end = date.fromisoformat(base["start"]), date.fromisoformat(base["end"])
    ages = list(base.get("children_ages") or [])
    return {"origin": {"city": base["origin"], "iata": HOME_IATA.get(base["origin"], [])},
            "destination": {"id": dest[0], "name": base["destination"], "country": dest[1]},
            "traits": None,
            "dates": {"start": base["start"], "end": base["end"], "month": start.month, "window": None,
                      "nights": (end - start).days, "flexible_days": 0},
            "party": {"segment": segment(base["adults"], ages), "adults": base["adults"], "children": len(ages),
                      "children_ages": ages or None, "pets": []},
            "budget_rub": None, "scope": None, "prefs": dict(PREFS), "constraints": None, "mood": None}


def empty_brief():
    return {k: None for k in ("origin", "destination", "traits", "dates", "party", "budget_rub", "scope", "prefs",
                              "constraints", "mood")}


# equivalent readings under the understand contract (segments.json defaults, no_hostel implies no_dorm):
# the benchmark checks meaning, not one of several correct spellings of it
def _contains(path, value):
    return {"type": "predicate", "name": "contains", "args": {"path": path, "values": [value]}}


def equivalent(e):
    """An expectation with an equivalent reading -> an any_of assertion, else None."""
    if e.get("set") == "party.children_ages" and e.get("equals") == []:
        return {"type": "path_in", "path": "/patch/set/party.children_ages", "values": [[], None], "allow_absent": True}
    if e.get("set") == "party.adults" and e.get("equals") in (1, 2):
        seg = {1: "solo", 2: "couple"}[e["equals"]]
        return {"type": "any_of", "of": [{"type": "path_equals", "path": "/patch/set/party.adults", "value": e["equals"]},
                                         {"type": "path_equals", "path": "/patch/set/party.segment", "value": seg}]}
    if e.get("constraint") == "no_dorm":
        return {"type": "any_of", "of": [_contains("/patch/set/constraints.add", "no_dorm"),
                                         _contains("/patch/set/constraints.add", "no_hostel")]}
    return None


def assertion(e, problems, cid):
    """The short format of BRIEF.md -> the runner's assertion."""
    eq = equivalent(e)
    if eq is not None:
        return eq
    if "intent" in e:
        return {"type": "path_equals", "path": "/intent", "value": e["intent"]}
    if "set" in e:
        path = f"/patch/set/{e['set']}"
        if "equals" in e:
            return {"type": "path_equals", "path": path, "value": e["equals"], "casefold": True}
        if "in" in e:
            return {"type": "path_in", "path": path, "values": e["in"], "casefold": True}
        if e.get("present"):
            return {"type": "path_present", "path": path}
    if "absent" in e:
        return {"type": "path_absent", "path": f"/patch/set/{e['absent']}"}
    if "constraint" in e:
        return {"type": "predicate", "name": "contains",
                "args": {"path": "/patch/set/constraints.add", "values": [e["constraint"]]}}
    if "trait" in e:
        return {"type": "predicate", "name": "contains", "args": {"path": "/patch/set/traits.add", "values": [e["trait"]]}}
    problems.append(f"{cid}: unknown expectation {e!r}")
    return None


def convert_cases(src, places, problems):
    out = []
    for c in read_json(src):
        cid = c["id"]
        if c["context"] == "selection":
            inp = {"today": TODAY, "stage": "selection", "brief": selection_brief(c["base"], places, problems, cid),
                   "assumed": [], "assumptions": [], "shown": SHOWN}
        else:
            inp = {"today": TODAY, "stage": "inspiration", "brief": empty_brief(), "assumed": [], "assumptions": [],
                   "shown": []}
        inp.update({"pending_questions": [], "history": [], "message": c["message"], "has_image": False})
        expect = [assertion(e, problems, cid) for e in c["expect"]]
        out.append({"id": cid, "category": "bench", "card": "understand", "input": inp, "image": None,
                    "expect": [{"type": "schema"}] + [e for e in expect if e], "runs": 5,
                    "notes": "sealed benchmark: written by an outside model; never tune on it"})
    return out


def check_scenario(path, places, problems):
    s = read_json(path)
    sid = s.get("id")
    if sid != path.stem:
        problems.append(f"{path.name}: id {sid!r} differs from the file name")
    p = s["persona"]
    if p["profile"]["home_city"] not in HOME_IATA:
        problems.append(f"{sid}: home city {p['profile']['home_city']!r} has no flights in the mock")
    for m in p["hidden"]["must"]:
        fn = PREDICATES.get(m["name"])
        if fn is None:
            problems.append(f"{sid}: unknown check {m['name']!r}")
            continue
        args = m.get("args") or {}
        try:
            inspect.signature(fn).bind(None, **args)
        except TypeError as exc:
            problems.append(f"{sid}: {m['name']} {args}: {exc}")
        if m["name"] == "destination_in":
            for n in args.get("names", []):
                if n not in places:
                    problems.append(f"{sid}: destination_in {n!r} is not a catalog name")
        for k in ("start", "end", "day"):
            v = args.get(k)
            if m["name"] in ("dates_within", "stay_starts_on") and v and not WINDOW[0] <= v <= WINDOW[1]:
                problems.append(f"{sid}: {m['name']} {k}={v} is outside {WINDOW}")
    if any(m["name"] == "order_action_done" for m in p["hidden"]["must"]):
        s["stop_on_paid"] = False   # the runner's flag: the dialogue goes on after payment (BRIEF v1 lacked it)
    return s


def main(argv=None):
    version = Path((argv or sys.argv[1:])[0])
    places = catalog()
    problems = []
    cases = convert_cases(version / "source" / "cases.json", places, problems)
    scenarios = [check_scenario(p, places, problems) for p in sorted((version / "source" / "scenarios").glob("*.json"))]
    for p in problems:
        print("PROBLEM", p)
    if problems:
        return 1
    for d in ("cases", "scenarios"):
        (version / d).mkdir(exist_ok=True)
        for old in (version / d).glob("*.json"):
            old.unlink()
    for c in cases:
        write_json(version / "cases" / f"{c['id']}.json", c)
    for s in scenarios:
        write_json(version / "scenarios" / f"{s['id']}.json", s)
    print(f"{len(cases)} cases, {len(scenarios)} scenarios -> {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
