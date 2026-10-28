"""Private profile history in runtime storage; never sent to insights or prompts."""
from datetime import date

from ostrovok_mcp import db

from .profiles import PastTrip

SCHEMA = """
CREATE TABLE IF NOT EXISTS views (
    profile_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('destination', 'hotel', 'variant')),
    ref_id TEXT NOT NULL,
    at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('shown', 'opened', 'carted'))
);
CREATE INDEX IF NOT EXISTS views_profile_at ON views(profile_id, at);
"""


def record(profile_id, events, at):
    if not profile_id or not events:
        return
    with db.state() as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO views(profile_id, kind, ref_id, at, source) VALUES (?, ?, ?, ?, ?)",
                         [(profile_id, kind, ref, at, source) for kind, ref, source in sorted(set(events))])


def for_profile(profile_id):
    with db.state() as conn:
        conn.executescript(SCHEMA)
        return [dict(row) for row in conn.execute(
            "SELECT profile_id, kind, ref_id, at, source FROM views WHERE profile_id = ? ORDER BY rowid",
            (profile_id,))]


def _variant_events(session, ids, source):
    events = []
    for vid in ids:
        variant = session.variant(vid)
        if variant is None:
            continue
        events.append(("variant", variant.id, source))
        if variant.destination_id:
            events.append(("destination", variant.destination_id, source))
        if variant.hotel:
            events.append(("hotel", variant.hotel.hotel_id, source))
    return events


def _paid_trip(ctx):
    order, s = ctx.order, ctx.session
    if not order or order.get("status") != "paid" or "pay" not in ctx.trace["steps"]:
        return
    brief = s.brief.values()
    dates, destination = brief.get("dates") or {}, brief.get("destination") or {}
    variant = s.variant(s.chosen_variant_id) if s.chosen_variant_id else None
    dest_id = variant.destination_id if variant else destination.get("id")
    lookup = getattr(ctx.engine.opendata, "destination", None)
    place = lookup(dest_id) if lookup and dest_id else None
    modes = {st.type for st in variant.steps if st.type in ("flight", "train")} if variant else set()
    start = date.fromisoformat(dates["start"]) if dates.get("start") else None
    paid_at = next((e["at"] for e in order.get("events") or [] if e.get("kind") == "paid"), s.clock())
    trip = PastTrip(order_id=order["order_id"], destination_id=dest_id,
                    destination_name=(place or {}).get("name_ru") or destination.get("name"),
                    month=start.month if start else dates.get("month"), year=start.year if start else None,
                    segment=(brief.get("party") or {}).get("segment"), nights=dates.get("nights"),
                    price_rub=order.get("total_rub"), party=brief.get("party"),
                    stars=variant.hotel.stars if variant and variant.hotel else None,
                    transport="plane" if modes == {"flight"} else "train" if modes == {"train"} else
                              "mixed" if modes else None,
                    destination_kind=(place or {}).get("kind"), paid_at=paid_at)
    ctx.engine.profiles.add_paid_trip(s.profile_id, trip)


def record_turn(ctx):
    """Analytics hook: successful shown/opened/carted events and confirmed payment."""
    s = ctx.session
    if ctx.error or ctx.profile is None:
        return
    events = []
    if ctx.present == "present.destinations":
        events += [("destination", item["id"], "shown") for item in ctx.destination_items]
    if ctx.present in ("present.variants", "present.update") or ctx.comparison:
        events += _variant_events(s, [v.id for v in ctx.variants], "shown")
    if ctx.intent in ("info", "compare", "choose") or ctx.action:
        events += _variant_events(s, ctx.variant_ids, "opened")
        events += [("hotel", hid, "opened") for hid in ctx.hotel_refs]
        if ctx.destination_hint:
            events.append(("destination", ctx.destination_hint, "opened"))
    if "cart_add" in ctx.trace["steps"] and s.chosen_variant_id:
        events += _variant_events(s, [s.chosen_variant_id], "carted")
    record(s.profile_id, events, s.clock().isoformat())
    _paid_trip(ctx)
