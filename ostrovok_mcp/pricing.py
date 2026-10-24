"""Price factors shared by the generators (all deterministic, documented here).

season_factor(day, profile): month curve per destination profile x Russian holiday peaks
  profiles: `beach` (summer-peak seaside), `tropical` (winter-peak hot countries), `ski`, `city`;
  holidays: 28 Dec - 8 Jan x1.35, 1-11 May x1.2 (applied on top of the month curve).
lead_factor(days, table): piecewise-linear in days between `today` and the travel date.
"""
from __future__ import annotations

from datetime import date

SEASON = {
    "beach":    [0.80, 0.80, 0.85, 0.95, 1.10, 1.30, 1.40, 1.40, 1.20, 1.00, 0.85, 0.90],
    "tropical": [1.30, 1.25, 1.15, 1.00, 0.85, 0.80, 0.80, 0.80, 0.80, 1.00, 1.15, 1.30],
    "ski":      [1.35, 1.40, 1.25, 0.90, 0.85, 0.90, 0.95, 0.95, 0.85, 0.85, 0.85, 1.25],
    "city":     [0.85, 0.85, 0.90, 0.95, 1.10, 1.15, 1.15, 1.15, 1.05, 1.00, 0.90, 1.00],
}
# hot-winter countries: their seaside peaks in the northern winter
TROPICAL_COUNTRIES = {"AE", "EG", "TH", "VN", "LK", "MV", "IN", "ID", "CU", "DO", "MX", "OM", "QA", "SC", "MU",
                      "MY", "PH", "SG", "TZ", "KE", "BH", "SA", "JO", "IL"}
FLIGHT_LEAD = [(0, 1.70), (3, 1.45), (7, 1.25), (14, 1.10), (30, 1.00), (60, 0.95), (120, 0.93), (366, 1.00)]
HOTEL_LEAD = [(0, 1.15), (7, 1.05), (21, 1.00), (60, 0.97), (366, 1.00)]
MAX_HORIZON_DAYS = 365  # schedules / inventory are open one year ahead


def profile(kind: str | None, tags: list[str] | None, country_code: str | None) -> str:
    tagset = set(tags or [])
    if "ski" in tagset or ("snow" in tagset and "sea" not in tagset):
        return "ski"
    seaside = (kind == "resort") or bool(tagset & {"sea", "beach", "island"})
    if seaside:
        hot = (country_code or "").upper() in TROPICAL_COUNTRIES or "tropical" in tagset
        return "tropical" if hot else "beach"
    return "city"


def holiday_factor(day: date) -> float:
    if (day.month == 12 and day.day >= 28) or (day.month == 1 and day.day <= 8):
        return 1.35
    if day.month == 5 and day.day <= 11:
        return 1.20
    return 1.0


def season_factor(day: date, prof: str) -> float:
    return SEASON.get(prof, SEASON["city"])[day.month - 1] * holiday_factor(day)


def lead_factor(days: int, table=FLIGHT_LEAD) -> float:
    if days <= table[0][0]:
        return table[0][1]
    for (d0, f0), (d1, f1) in zip(table, table[1:]):
        if days <= d1:
            return f0 + (f1 - f0) * (days - d0) / (d1 - d0)
    return table[-1][1]


def round10(x: float) -> int:
    return int(round(x / 10.0)) * 10
