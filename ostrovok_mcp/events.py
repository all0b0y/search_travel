"""Synthetic schedules and prices at named OSM venues; no network or wall clock."""
from datetime import date, timedelta

from . import db, geo, rng

VENUE_KINDS = {
    "theatre": ("concert", "show"), "concert_hall": ("concert",),
    "arts_centre": ("show", "concert"), "stadium": ("sport",),
    "park": ("festival",), "square": ("festival",), "entertainment": ("show",),
}
TOUR_TAGS = {"museum": "history", "castle": "history", "monument": "history",
             "attraction": "history", "street": "gastro", "landmark": "industrial",
             "viewpoint": "rooftops", "park": "nature", "nature": "nature"}
TITLES = {"concert": "Концерт", "show": "Представление", "sport": "Спортивное событие",
          "festival": "Фестиваль", "history": "Историческая экскурсия", "gastro": "Гастроэкскурсия",
          "industrial": "Индустриальная экскурсия", "rooftops": "Экскурсия по обзорным площадкам",
          "nature": "Прогулка с гидом", "night": "Ночная экскурсия"}


def _event(destination, venue, day, kind, tag=None):
    seed = (venue["osm_id"], day.isoformat(), kind)
    night = tag == "night"
    hour = rng.rint(19, 21, *seed, "hour") if kind != "excursion" or night else rng.rint(10, 16, *seed, "hour")
    start = geo.local_dt(day, hour * 60, geo.destination_tz(destination))
    duration = rng.pick((60, 90, 120, 150), *seed, "duration")
    title = TITLES[tag or kind]
    return {"event_id": "ev_" + rng.hexid(destination["id"], *seed),
            "destination_id": destination["id"], "venue_poi_id": venue["osm_id"],
            "venue_name": venue.get("name_ru") or venue["name"], "kind": kind,
            "title_ru": f"{title}: «{venue.get('name_ru') or venue['name']}» (синтетика)",
            "starts_at": geo.iso(start), "ends_at": geo.iso(start + timedelta(minutes=duration)),
            "duration_min": duration, "price_from_rub": rng.rint(4, 50, *seed, "price") * 100,
            "niche_tags": [tag] if tag else [], "source": "synthetic"}


def search(destination_id, date_from, date_to, kinds=None, tags=None, limit=20):
    destination = geo.destination(destination_id)
    if destination is None:
        return []
    venues = db.opendata().query("pois", "destination_id = ?", (destination_id,),
                                key=("destination_id", "osm_id"), order="rank, osm_id")
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    out = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        for venue in venues:
            if not venue.get("name") or not str(venue.get("source", "")).startswith(("overpass", "osm")):
                continue
            seed = (venue["osm_id"], day.isoformat())
            events = []
            choices = VENUE_KINDS.get(venue["kind"])
            if choices and rng.rint(0, 2, *seed, "scheduled"):
                events.append(_event(destination, venue, day, rng.pick(choices, *seed, "kind")))
            tag = TOUR_TAGS.get(venue["kind"])
            if tag:
                if rng.rint(0, 3, *seed, "night") == 0:
                    tag = "night"
                events.append(_event(destination, venue, day, "excursion", tag))
            out.extend(e for e in events if (not kinds or e["kind"] in kinds)
                       and (not tags or set(tags) & set(e["niche_tags"])))
        if len(out) >= limit:
            break
    return sorted(out, key=lambda e: (e["starts_at"], e["event_id"]))[:limit]
