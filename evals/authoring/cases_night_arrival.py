"""Own level-2 regression cases for a daytime recommendation on a quiet trip.

Inputs are built offline by the real engine, catalogs and mock with FakeBackend;
no paid model calls. Run: python -m evals.authoring.cases_night_arrival.
"""
import tempfile

from agent.flags import parse_flags
from agent.night_arrival import has_night_arrival
from agent.profiles import Profile
from agent.ranking import ranking_rules
from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _engine, _last_input, _u
from evals.card_inputs import validate_card_input
from evals.common import CASES_DIR, write_json


def cases():
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        engine.flags = parse_flags("stage1")
        engine.profiles.put(Profile(id="senior", age=72, home_city="Москва"))
        try:
            for suffix, profile_id, extra, message in [
                ("relax", "demo", {"mood.add": ["relax"]}, "В Ереван вдвоём, спокойно отдохнуть"),
                ("senior", "senior", {}, "В Ереван вдвоём, нам за семьдесят"),
            ]:
                sid = engine.new_session(profile_id=profile_id, now=NOW)
                backend.add("understand", _u(set_={"origin.city": "Москва", "destination.name": "Ереван",
                            "dates.start": "2026-11-17", "dates.end": "2026-11-22",
                            "party.adults": 2, "party.segment": "couple", **extra}))
                response = engine.handle(sid, {"session_id": sid, "text": message + ", 17–22 ноября"})
                assert not response.get("error")
                inp = _last_input(backend, "present.variants")
                assert not validate_card_input("present.variants", inp)
                variants = engine.get_session(sid).variants
                window = ranking_rules(engine.skill.rules)["night_window"]
                assert any(has_night_arrival(v, window) for v in variants)
                hint = inp["recommended_hint"]
                assert not has_night_arrival(next(v for v in variants if v.id == hint), window)
                out.append(case("na_" + suffix + "_daytime_arrival", "C", "present.variants", inp,
                                [a("ids_subset"), a("numbers_subset"), pred("russian_only"),
                                 a("path_equals", path="/recommended_id", value=hint),
                                 pred("no_unbacked_claims"), pred("money_risks_plain")],
                                "Own example: Moscow–Yerevan, 17–22 November. The cheapest trip arrives at night; "
                                "code recommends a daytime arrival for a quiet holiday or older travellers. "
                                "Follow recommended_hint and keep the cheaper night's risks honest."))
        finally:
            engine.close()
    return out


if __name__ == "__main__":
    for item in cases():
        write_json(CASES_DIR / (item["id"] + ".json"), item)
