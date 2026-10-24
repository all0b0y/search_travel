"""Anonymous insights with k-anonymity (contract.K_MIN = 20).

Sources: catalog `insights_events` (synthetic, built by data_build) UNION runtime
`insights_events_live` (written by insights_log_event). Only groups with n >= K_MIN are ever
returned: for popular destinations both the (origin, month, segment) group and every
destination row; for segment preferences the segment(+kind) group, and each statistic needs
>= K_MIN non-null values (otherwise that statistic is null).
Live events carry the preference fields in `data` (meals, free_cancellation, direct_flight,
district, nights, budget_rub) and the destination kind is looked up in opendata.
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any

from . import db, geo
from .contract import K_MIN, PERSONAL_KEYS
from .db import ServiceError, jdump, jload

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"(?:\+?\d[\s\-()]*){10,}")
EXTRA_PERSONAL = {"first_name", "last_name", "surname", "full_name", "fio", "phone_number", "mail", "e_mail",
                  "passport_number", "document", "address", "card", "card_number", "ip", "user_id", "user"}


def _live_rows() -> list[dict]:
    with db.state() as conn:
        rows = conn.execute("SELECT kind, segment, origin_city, destination_id, month, data FROM insights_events_live"
                            ).fetchall()
    out = []
    for r in rows:
        data = jload(r["data"], {})
        out.append({"kind": r["kind"], "segment": r["segment"], "origin_city": r["origin_city"],
                    "destination_id": r["destination_id"], "month": r["month"], **{
                        k: data.get(k) for k in ("meals", "free_cancellation", "direct_flight", "district", "nights",
                                                 "budget_rub", "destination_kind")}})
    return out


def _catalog_rows(where: str, params: tuple) -> list[dict]:
    return db.catalog().query("insights_events", where, params, key="id")


def popular_destinations(origin_city: str, month: int, segment: str) -> list[dict]:
    want = geo.norm(origin_city)
    rows = [r for r in _catalog_rows("month = ? AND segment = ?", (month, segment))
            if r.get("origin_city") and geo.norm(r["origin_city"]) == want and r.get("destination_id")]
    rows += [r for r in _live_rows() if r["month"] == month and r["segment"] == segment and r["origin_city"]
             and geo.norm(r["origin_city"]) == want and r["destination_id"]]
    total = len(rows)
    if total < K_MIN:
        return []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["destination_id"]] = counts.get(r["destination_id"], 0) + 1
    items = [{"destination_id": d, "share": round(n / total, 4), "n": n} for d, n in counts.items() if n >= K_MIN]
    items.sort(key=lambda x: (-x["n"], x["destination_id"]))
    return items


def _kind_of(dest_id: str | None, cache: dict) -> str | None:
    if not dest_id:
        return None
    if dest_id not in cache:
        d = geo.destination(dest_id)
        cache[dest_id] = d.get("kind") if d else None
    return cache[dest_id]


def _share(values: list[Any], pred) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < K_MIN:
        return None
    return round(sum(1 for v in vals if pred(v)) / len(vals), 4)


def _pct(vals: list[int], q: float) -> int:
    s = sorted(vals)
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(len(s) - 1, int(pos) + 1)
    return int(round(s[lo] + (s[hi] - s[lo]) * (pos - lo)))


def segment_preferences(segment: str, destination_kind: str | None) -> dict:
    if destination_kind:
        rows = _catalog_rows("segment = ? AND destination_kind = ?", (segment, destination_kind))
    else:
        rows = _catalog_rows("segment = ?", (segment,))
    cache: dict = {}
    for r in _live_rows():
        if r["segment"] != segment:
            continue
        kind = r.get("destination_kind") or _kind_of(r["destination_id"], cache)
        if destination_kind and kind != destination_kind:
            continue
        if any(r.get(k) is not None for k in ("meals", "free_cancellation", "direct_flight", "district", "nights",
                                              "budget_rub")):
            rows.append(r)
    n = len(rows)
    out = {"segment": segment, "destination_kind": destination_kind, "available": False, "n": n, "shares": None,
           "median_nights": None, "budget_rub_percentiles": None, "district_preference": None, "k_min": K_MIN,
           "synthetic": True}
    if n < K_MIN:
        out["n"] = 0  # do not reveal the size of a small group
        return out
    out["available"] = True
    meals = [r.get("meals") for r in rows]
    shares = {"breakfast": _share(meals, lambda v: v == "breakfast"),
              "all_inclusive": _share(meals, lambda v: v == "all_inclusive"),
              "free_cancellation": _share([r.get("free_cancellation") for r in rows], lambda v: bool(v)),
              "direct_flights": _share([r.get("direct_flight") for r in rows], lambda v: bool(v))}
    if all(v is not None for v in shares.values()):
        out["shares"] = shares
    nights = [int(r["nights"]) for r in rows if r.get("nights") is not None]
    if len(nights) >= K_MIN:
        out["median_nights"] = float(median(nights))
    budgets = [int(r["budget_rub"]) for r in rows if r.get("budget_rub") is not None]
    if len(budgets) >= K_MIN:
        out["budget_rub_percentiles"] = {"p25": _pct(budgets, 0.25), "p50": _pct(budgets, 0.5),
                                         "p75": _pct(budgets, 0.75)}
    districts = [r.get("district") for r in rows if r.get("district") in ("lively", "quiet", "family", "walkable")]
    if len(districts) >= K_MIN:
        out["district_preference"] = {k: round(districts.count(k) / len(districts), 4)
                                      for k in ("lively", "quiet", "family", "walkable")}
    return out


def _scan_personal(value: Any, path: str = "event") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            kl = str(k).lower()
            if kl in PERSONAL_KEYS or kl in EXTRA_PERSONAL:
                raise ServiceError("personal_data", f"personal data key not allowed in insights: {path}.{k}")
            _scan_personal(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _scan_personal(v, f"{path}[{i}]")
    elif isinstance(value, str):
        if EMAIL_RE.search(value) or PHONE_RE.search(value):
            raise ServiceError("personal_data", f"value looks like an email or a phone number: {path}")


def log_event(event: dict) -> dict:
    _scan_personal({k: v for k, v in event.items() if k != "at"})
    with db.state() as conn:
        conn.execute("INSERT INTO insights_events_live (at, kind, stage, segment, origin_city, destination_id, month, "
                     "data, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (event["at"], event["kind"], event.get("stage"), event.get("segment"), event.get("origin_city"),
                      event.get("destination_id"), event.get("month"), jdump(event.get("data") or {}), "live",
                      event["at"]))
    return {"ok": True}
