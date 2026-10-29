"""Dry runs of level 2 and level 3 end to end with fakes; report writing; judge merge; compare."""
import json
from pathlib import Path

import pytest

from evals import report as report_mod
from evals import run as run_mod
from evals.common import read_json, read_jsonl, write_json
from evals.persona import FakePersona
from evals.testing.fakes import FakeEngine


def test_level2_dry_run(tmp_path):
    out = tmp_path / "run2"
    rep = run_mod.main(["--level", "2", "--dry-run", "--runs", "1", "--out", str(out), "--parallel", "4"], log=lambda *a: None)
    assert rep["metrics"]["cases_total"] == 94 and rep["metrics"]["runs_total"] == 94   # includes 2 night-arrival cases
    assert rep["metrics"]["input_errors"] == 0 and rep["metrics"]["llm_errors"] == 0
    assert rep["metrics"]["served_models"] == {"fake/model": 94}
    recs = read_jsonl(out / "level2.jsonl")
    assert len(recs) == 94 and all(r["results"] for r in recs)
    assert {"na_relax_daytime_arrival", "na_senior_daytime_arrival"} <= {r["case_id"] for r in recs}
    assert Path(rep["report_paths"]["json"]).is_file() and Path(rep["report_paths"]["md"]).is_file()
    assert Path(rep["report_paths"]["json"]).parent == out / "reports"
    assert read_json(out / "summary.json")["run_id"] == "run2"
    md = Path(rep["report_paths"]["md"]).read_text(encoding="utf-8")
    assert "фазовые кейсы 5/5" in md and "| a1_warm_sea_kid |" in md


def test_level2_only_and_unknown(tmp_path):
    rep = run_mod.main(["--level", "2", "--dry-run", "--only", "a1_warm_sea_kid,c2_sochi_variants", "--out",
                        str(tmp_path / "r")], log=lambda *a: None)
    assert rep["metrics"]["runs_total"] == 10  # 2 cases x 5 runs (case default)
    with pytest.raises(SystemExit):
        run_mod.main(["--level", "2", "--dry-run", "--only", "nope", "--out", str(tmp_path / "r2")], log=lambda *a: None)


def test_level3_dry_run(tmp_path):
    out = tmp_path / "run3"
    rep = run_mod.main(["--level", "3", "--dry-run", "--out", str(out), "--parallel", "3"], log=lambda *a: None)
    m = rep["metrics"]
    assert m["scenarios"] == 26 and m["completion_rate"] == 1.0 and m["first_rec_rate"] == 1.0
    assert m["touches_to_cart_mean"] is not None and m["touches_to_cart_max"] <= 3
    assert m["response_schema_error_turns"] == 0
    assert m["jev_calls"] == 26 and m["jev_fallback_rate"] == 1.0
    assert m["served_models"] and m["persona_models"] == {"fake/buttons": m["persona_models"]["fake/buttons"]}
    assert 0 < m["hidden_must_rate"] < 1
    assert rep["scenarios"][0]["must"]
    turns = read_jsonl(out / "level3.jsonl")
    assert turns and all("request" in t for t in turns)
    photo = [t for t in turns if t["scenario_id"] == "s04_photo_landmark" and t["turn"] == 1][0]
    assert photo["request"]["image"] == "<image evals/images/landmark_hagia_sophia.jpg>"
    packets = sorted((out / "judge").glob("*.md"))
    assert len(packets) == 26
    text = (out / "judge" / "s15_after_early_checkin.md").read_text(encoding="utf-8")
    assert "Hidden facts" in text and "Must predicates" in text and "Пользователь:" in text and "[A1]" not in text
    s15 = next(s for s in rep["scenarios"] if s["scenario_id"] == "s15_after_early_checkin")
    assert s15["end_reason"] == "persona_stop_done" and any(m["name"] == "order_action_done" and m["ok"] for m in s15["must"])


def test_level3_holdout_dry_run(tmp_path):
    rep = run_mod.main(["--level", "3", "--set", "holdout", "--dry-run", "--out", str(tmp_path / "h")], log=lambda *a: None)
    assert rep["metrics"]["scenarios"] == 10 and rep["set"] == "holdout"


def _scenario():
    return read_json(run_mod.SCENARIOS_DIR / "s08_sochi_exact.json")


def test_run_scenario_with_text_turns_and_engine_error(tmp_path):
    scen = _scenario()
    persona = FakePersona([{"type": "text", "text": "а с завтраком?"}, {"type": "click", "ref": "V2"}])
    row = run_mod.run_scenario(scen, 0, FakeEngine(fail_on_turn=3), persona, tmp_path, "main", log=lambda *a: None)
    assert row["end_reason"] == "engine_error" and row["turns"] == 2
    assert row["touches_to_cart"] is None  # the cart never arrived: the engine failed on the 3rd request
    recs = read_jsonl(tmp_path / "level3.jsonl")
    assert recs[-1]["error"].startswith("RuntimeError")


def test_touch_counting(tmp_path):
    scen = _scenario()
    persona = FakePersona([{"type": "text", "text": "а с завтраком?"}, {"type": "click", "ref": "V2"},
                           {"type": "click", "ref": "A1"}, {"type": "click", "ref": "A1"}])
    row = run_mod.run_scenario(scen, 0, FakeEngine(), persona, tmp_path, "main", log=lambda *a: None)
    # touches: start text (1) -> variants; text (2) -> variants; V2 (3) -> cart ready
    assert row["touches_to_cart"] == 3 and row["paid"] and row["end_reason"] == "paid"
    assert row["first_rec"] is True and row["calls_per_turn"][0] == 2


def test_persona_error_ends_scenario(tmp_path):
    class Broken:
        name = "broken"

        def reply(self, *a):
            raise RuntimeError("no")
    row = run_mod.run_scenario(_scenario(), 0, FakeEngine(), Broken(), tmp_path, "main", log=lambda *a: None)
    assert row["end_reason"] == "persona_error" and row["turns"] == 1


def test_cart_ready_and_kinds():
    assert run_mod.required_cart_kinds({"scope": ["stay"]}) == {"hotel"}
    assert run_mod.required_cart_kinds({"scope": None}) == {"hotel", "transport"}
    assert run_mod.required_cart_kinds({"scope": ["flights"]}) == {"transport"}
    hotel_only = {"cart": {"items": [{"kind": "hotel"}]}}
    assert run_mod.cart_ready(hotel_only, {"hotel"}) and not run_mod.cart_ready(hotel_only, {"hotel", "transport"})


def _fake_report(level=3, set_name="main", **metrics):
    base = {"run_id": f"r_{set_name}", "level": level, "set": set_name, "model": "fake", "flags": "stage1",
            "metrics": metrics, "scenarios": [], "cases": []}
    return base


def test_thresholds_and_compare(tmp_path):
    a = _fake_report(first_rec_rate=0.9, touches_to_cart_mean=3.0, hidden_must_rate=0.95, connection_errors_total=0,
                     completion_rate=0.9)
    b = _fake_report(set_name="holdout", first_rec_rate=0.7, touches_to_cart_mean=4.5, hidden_must_rate=0.92,
                     connection_errors_total=1, completion_rate=0.75)
    assert all(l["pass"] for l in report_mod.threshold_lines(a)) and report_mod.is_green(a)
    assert [l["pass"] for l in report_mod.threshold_lines(b)] == [False, False, True, False]
    text = report_mod.compare(a, b)
    assert "first_rec_rate" in text and "-0.200" in text
    assert "Holdout vs main" in text and "FAIL completion_rate" in text and "PASS hidden_must_rate" in text
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    write_json(pa, a)
    write_json(pb, b)
    assert report_mod.main(["--compare", str(pa), str(pb)]) == 0
    l2 = _fake_report(level=2, set_name="cases", cases_full_pass_rate=0.5)
    assert not report_mod.is_green(l2)


def test_merge_judge(tmp_path):
    out = tmp_path / "run3"
    rep = run_mod.main(["--level", "3", "--dry-run", "--only", "s08_sochi_exact,s10_hotel_only_kazan", "--out", str(out)],
                       log=lambda *a: None)
    scores = {k: 4 for k in report_mod.VERDICT_SCORES}
    write_json(out / "judge" / "s08_sochi_exact.verdict.json",
               {"scenario_id": "s08_sochi_exact", "pass": True, "scores": scores, "failures": [], "notes": "ok"})
    write_json(out / "judge" / "s10_hotel_only_kazan.verdict.json", {"scenario_id": "s10_hotel_only_kazan", "pass": "yes"})
    merged = report_mod.merge_judge(out)
    j = merged["judge"]
    assert j["count"] == 1 and j["expected"] == 2 and j["pass_rate"] == 1.0
    assert "s10_hotel_only_kazan" in j["invalid"] and j["missing"] == ["s10_hotel_only_kazan"]
    assert j["mean_scores"]["tone"] == 4
    again = read_json(rep["report_paths"]["json"])
    assert again["judge"]["count"] == 1 and again["metrics"]["judge_pass_rate"] == 1.0
    md = Path(rep["report_paths"]["md"]).read_text(encoding="utf-8")
    assert "## Judge" in md
    assert report_mod.main(["--merge-judge", str(out)]) == 0


def test_inject_profile_real_store():
    from agent.profiles import ProfileStore

    class E:
        profiles = ProfileStore(":memory:")
    prof = _scenario()["persona"]["profile"]
    assert run_mod.inject_profile(E(), "eval_x", prof)
    got = E.profiles.get("eval_x")
    assert got.home_city == "Москва" and got.home_iata == ["SVO", "DME", "VKO"] and got.age == prof["age"]
    assert run_mod.inject_profile(FakeEngine(), "eval_y", prof)

    class NoStore:
        pass
    assert not run_mod.inject_profile(NoStore(), "eval_z", prof)
