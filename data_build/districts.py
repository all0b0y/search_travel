"""District profiles around a hotel from OSM POIs (AGENT_PLAN decision 9).

Input: the POIs and ways of one destination (parsed from the batched Overpass response, or from
Photon results in the fallback). Output per hotel: POI counts within RADIUS_M = 500 m, nearest
distances, and four scores in 0..1.

POI categories (counted within 500 m of the hotel):
  bar, pub, nightclub, restaurant, cafe, fast_food, pharmacy, supermarket, convenience,
  playground, park, pedestrian (pedestrian street ways), stop (public transport
  stop_position/platform), station (railway=station|halt), beach.
  `nightlife` = bar + pub + nightclub; `food` = restaurant + cafe + fast_food;
  `shops` = supermarket + convenience + pharmacy.
Nearest distances (metres, rounded; key absent when no such feature in the fetched area):
  beach, major_road (highway=motorway|trunk|primary), railway (railway=rail), station, stop, centre.

Helpers:  sat(x, k) = min(1, x / k)   (linear saturation at k items)
          near(d, d0, d1) = 1 when d <= d0, 0 when d >= d1 or unknown, linear in between.

Scores (each clamped to 0..1, rounded to 3 decimals):
  lively   = 0.6 * sat(nightlife_w, 12) + 0.4 * sat(food, 40),
             nightlife_w = bar + pub + 2 * nightclub
  quiet    = 1 - (0.55 * sat(nightlife_w, 8) + 0.15 * sat(food, 60)
                  + 0.2 * near(major_road, 50, 300) + 0.1 * near(railway, 80, 400))
  family   = 0.3 * sat(playground, 3) + 0.25 * sat(park, 3) + 0.2 * sat(shops, 5)
             + 0.25 * B - 0.45 * sat(nightlife_w, 12),
             B = near(beach, 300, 1200) when the destination has any beach in its fetched
             area; otherwise B = sat(park, 3) (the beach weight goes to parks for inland places)
  walkable = 0.35 * sat(food + shops, 40) + 0.25 * sat(stop + station, 8)
             + 0.2 * sat(pedestrian, 4) + 0.2 * near(centre, 1000, 5000)
`source` is 'overpass' or 'photon' (Photon gives at most ~50 results per category, so its
counts are lower bounds and the district is marked accordingly).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .common import haversine_m, point_polyline_m

RADIUS_M = 500
POI_CATEGORIES = ("bar", "pub", "nightclub", "restaurant", "cafe", "fast_food", "pharmacy", "supermarket",
                  "convenience", "playground", "park", "pedestrian", "stop", "station", "beach")
NEAREST_KEYS = ("beach", "major_road", "railway", "station", "stop", "centre")


@dataclass
class Features:
    """POIs as points per category, plus lines for roads/railways/pedestrian ways and beaches."""
    points: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    lines: dict[str, list[list[tuple[float, float]]]] = field(default_factory=dict)

    def add_point(self, cat, lat, lon):
        self.points.setdefault(cat, []).append((lat, lon))

    def add_line(self, cat, pts):
        if pts:
            self.lines.setdefault(cat, []).append(pts)

    def has_beach(self) -> bool:
        return bool(self.points.get("beach") or self.lines.get("beach"))


def sat(x: float, k: float) -> float:
    return max(0.0, min(1.0, x / k))


def near(d: float | None, d0: float, d1: float) -> float:
    if d is None or d >= d1:
        return 0.0
    if d <= d0:
        return 1.0
    return (d1 - d) / (d1 - d0)


def _clamp(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 3)


def scores(counts: dict[str, int], nearest: dict[str, float], dest_has_beach: bool) -> dict[str, float]:
    c = lambda k: counts.get(k, 0)  # noqa: E731
    nightlife_w = c("bar") + c("pub") + 2 * c("nightclub")
    food = c("restaurant") + c("cafe") + c("fast_food")
    shops = c("supermarket") + c("convenience") + c("pharmacy")
    lively = 0.6 * sat(nightlife_w, 12) + 0.4 * sat(food, 40)
    quiet = 1 - (0.55 * sat(nightlife_w, 8) + 0.15 * sat(food, 60)
                 + 0.2 * near(nearest.get("major_road"), 50, 300) + 0.1 * near(nearest.get("railway"), 80, 400))
    b = near(nearest.get("beach"), 300, 1200) if dest_has_beach else sat(c("park"), 3)
    family = (0.3 * sat(c("playground"), 3) + 0.25 * sat(c("park"), 3) + 0.2 * sat(shops, 5) + 0.25 * b
              - 0.45 * sat(nightlife_w, 12))
    walkable = (0.35 * sat(food + shops, 40) + 0.25 * sat(c("stop") + c("station"), 8)
                + 0.2 * sat(c("pedestrian"), 4) + 0.2 * near(nearest.get("centre"), 1000, 5000))
    return {"lively": _clamp(lively), "quiet": _clamp(quiet), "family": _clamp(family), "walkable": _clamp(walkable)}


class _Grid:
    """Bucket points by ~0.01 deg cells for fast radius queries."""

    def __init__(self, pts, cell=0.01):
        self.cell = cell
        self.b: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for p in pts:
            self.b.setdefault((int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))), []).append(p)

    def around(self, lat, lon, radius_m):
        dl = radius_m / 111000 / self.cell
        dlo = radius_m / (111000 * max(0.1, math.cos(math.radians(lat)))) / self.cell
        i0, j0 = int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))
        for i in range(i0 - int(dl) - 1, i0 + int(dl) + 2):
            for j in range(j0 - int(dlo) - 1, j0 + int(dlo) + 2):
                yield from self.b.get((i, j), ())


def district_for(lat: float, lon: float, feats: Features, centre: tuple[float, float],
                 grids: dict | None = None) -> dict:
    """Counts, nearest distances and scores for one hotel location."""
    counts: dict[str, int] = {}
    nearest: dict[str, float] = {}
    grids = grids if grids is not None else {}
    for cat, pts in feats.points.items():
        g = grids.get(cat)
        if g is None:
            g = grids[cat] = _Grid(pts)
        n = 0
        for p in g.around(lat, lon, RADIUS_M):
            if haversine_m(lat, lon, p[0], p[1]) <= RADIUS_M:
                n += 1
        if cat in POI_CATEGORIES:
            counts[cat] = counts.get(cat, 0) + n
        if cat in ("beach", "station", "stop"):
            best = min((haversine_m(lat, lon, p[0], p[1]) for p in pts), default=None)
            if best is not None:
                nearest[cat] = min(nearest.get(cat, math.inf), best)
    for cat, lines in feats.lines.items():
        best = math.inf
        n_within = 0
        for pts in lines:
            # quick reject by bounding box (> 3 km away)
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            if (lat < min(lats) - 0.03 or lat > max(lats) + 0.03 or lon < min(lons) - 0.04
                    or lon > max(lons) + 0.04) and best < 3000:
                continue
            d = point_polyline_m(lat, lon, pts)
            best = min(best, d)
            if d <= RADIUS_M:
                n_within += 1
        if cat == "pedestrian":
            counts["pedestrian"] = counts.get("pedestrian", 0) + n_within
        if cat == "beach":
            counts["beach"] = counts.get("beach", 0) + n_within
            if best < math.inf:
                nearest["beach"] = min(nearest.get("beach", math.inf), best)
        elif cat in ("major_road", "railway") and best < math.inf:
            nearest[cat] = best
    nearest["centre"] = haversine_m(lat, lon, centre[0], centre[1])
    nearest = {k: int(round(v)) for k, v in nearest.items() if v < math.inf}
    counts = {k: counts.get(k, 0) for k in POI_CATEGORIES}
    return {"poi_counts": counts, "nearest_m": nearest, **scores(counts, nearest, feats.has_beach())}
