"""Build an export from the bought offers when cart upgrades changed the selected variant."""
from datetime import datetime


def for_order(ctx, order, original):
    items = order.get("items") or []
    paid_ids = {item["offer_id"] for item in items}
    if paid_ids == {step.offer_id for step in original.steps} and order["total_rub"] == original.totals.rub:
        return original

    from . import steps as S

    destination = ctx.session.brief.destination.value
    results = {"destination": {"id": original.destination_id, "name": destination.name if destination else ""},
               "flights_out": [], "flights_back": [], "trains_out": [], "trains_back": [],
               "hotels": [], "transfers_out": {}, "transfers_back": {}, "scope": []}
    transport = sorted((item for item in items if item["kind"] in ("flight", "train")),
                       key=lambda item: datetime.fromisoformat(item["start"]))
    for item in items:
        details = item.get("details") or {}
        snapshot = details.get("offer")
        if not snapshot:
            raise ValueError("paid offer snapshot is missing")
        offer = {**snapshot, "offer_id": item["offer_id"]}
        kind = item["kind"]
        if kind in ("flight", "train"):
            direction = "out" if item is transport[0] else "back"
            offer["price_rub"] = item["price_rub"]
            results[f"{kind}s_{direction}"].append(offer)
            results["scope"].append(f"{kind}s")
        elif kind == "hotel":
            offer["price_total_rub"] = item["price_rub"]
            results["hotels"].append(offer)
            results["scope"].append("stay")
            results["check_in"], results["check_out"] = details["params"]["ci"], details["params"]["co"]
        elif kind == "transfer":
            params = details["params"]
            point = params["fr"]
            offer.update(price_rub=item["price_rub"], point=point.get("iata") or point.get("station"),
                         point_ref=point, hotel_id=params["h"])
            direction = "back" if params["rv"] else "out"
            results[f"transfers_{direction}"].setdefault(params["h"], []).append(offer)
            results["scope"].append("transfers")

    districts = dict(ctx.districts)
    for hotel in results["hotels"]:
        osm_id = hotel.get("osm_id")
        if osm_id and osm_id not in districts:
            profile = S.od(ctx, "district_profile", osm_id)
            if profile is not None:
                districts[osm_id] = profile
    built = S.build(ctx, ctx.session.brief, results, districts, pin=False)
    paid = next((variant for variant in built if variant.totals.rub == order["total_rub"]
                 and {step.offer_id for step in variant.steps} == paid_ids), None)
    if paid is None:
        raise ValueError("paid offers do not form an exact exportable plan")
    titles = {item["offer_id"]: item["title"] for item in items}
    paid.steps = [step.model_copy(update={"title": titles[step.offer_id]}) for step in paid.steps]
    return paid.model_copy(update={"id": original.id, "title": original.title})
