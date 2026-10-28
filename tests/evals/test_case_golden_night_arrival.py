"""Own night-arrival cases accept the daytime pick and reject the night pick."""
import json

import pytest

from evals.common import CASES_DIR

from .test_case_golden import run


@pytest.mark.parametrize("suffix", ["relax", "senior"])
def test_daytime_recommendation_case(suffix):
    cid = "na_" + suffix + "_daytime_arrival"
    inp = json.loads((CASES_DIR / (cid + ".json")).read_text())["input"]
    good = {"message": "Советую вариант с дневным прибытием. Более дешёвый вариант приезжает ночью.",
            "items": [{"id": item["id"], "why": "Подходит для поездки.", "sacrifices": [], "risks": [],
                       "not_for": []} for item in inp["items"]],
            "recommended_id": inp["recommended_hint"], "upsell": []}
    assert run(cid, good) == (True, [])
    bad = dict(good, recommended_id="cheapest")
    ok, failed = run(cid, bad)
    assert not ok and any(f["type"] == "path_equals" for f in failed)
