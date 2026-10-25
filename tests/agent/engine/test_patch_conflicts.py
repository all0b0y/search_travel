"""A model patch that sets and unsets one path does not end the turn: the set wins (real data, scripted model).

Seen live: «Я без самолёта хотела… что-нибудь рядом с Питером, куда на поезде или электричке можно?» came back
with prefs.max_travel_hours both in set and in unset, and apply_patch raised PatchError -> engine_error.
"""
from .fakes import u
from .test_scopes import live  # noqa: F401  (the real-data harness fixture)

TRIP = {"origin.city": "Москва", "destination.name": "Казань", "dates.start": "2026-11-20", "dates.end": "2026-11-23",
        "party.segment": "couple", "party.adults": 2}


def test_set_and_unset_of_one_path_keeps_the_set(live):
    h = live
    h.understand(u(set={**TRIP, "prefs.max_travel_hours": 6}, unset=["prefs.max_travel_hours"]))
    r = h.turn("В Казань на три ночи вдвоём, не дольше 6 часов в дороге")
    assert r.get("error") is None, (r["message"], r.get("error"))
    assert h.session().brief.values()["prefs"]["max_travel_hours"] == 6
    assert any("both set and unset" in n for n in r["debug"]["notes"]), r["debug"]["notes"]


def test_set_and_unset_on_a_refine_keeps_the_set(live):
    h = live
    h.understand(u(set=TRIP))
    assert h.turn("В Казань на три ночи вдвоём").get("error") is None
    h.understand(u(intent="refine", set={"prefs.transport": "train", "prefs.max_travel_hours": 12},
                   unset=["prefs.max_travel_hours", "prefs.direct_only"]))
    r = h.turn("Без самолёта, на поезде, но не дольше 12 часов")
    assert r.get("error") is None, (r["message"], r.get("error"))
    prefs = h.session().brief.values()["prefs"]
    assert prefs["max_travel_hours"] == 12 and prefs["transport"] == "train"
