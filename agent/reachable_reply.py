"""Put complete, reachable trips before accommodation with no transport."""
from . import payloads as P


def reachable_reply(ctx):
    gap = getattr(ctx, "transport_gap", None) or {}
    places = getattr(ctx, "other_places", None) or []
    variants = list(getattr(ctx, "variants", None) or [])
    if (getattr(ctx, "present", None) not in ("present.variants", "present.update")
            or getattr(ctx, "message", None) or (getattr(ctx, "card_input", None) or {}).get("question")
            or not gap or not places or not variants or not all(v.facts.get("no_transport") for v in variants)):
        return None
    line = str(gap.get("line") or "").split(" — показываю только проживание")[0].rstrip(".")
    choices = ", ".join(
        f"{p['name']}{' поездом' if p.get('transport_ru') == 'поездом' else ''} — от {P.fmt_rub(p['price_from_rub'])}"
        for p in places[:3])
    low = min(v.totals.rub for v in variants)
    road_actions = [a['label'] for a in (getattr(ctx, 'extra_actions', None) or [])
                    if a.get('type') == 'set_chip' and a.get('label')]
    road = (' Ещё можно выбрать «' + '» или «'.join(road_actions) + '».' if road_actions else '')
    return (f"{line}. Похожие места, куда можно добраться: {choices} — кнопки ниже. "
            f"Если важнее исходное направление, отдельно есть только проживание: от {P.fmt_rub(low)} "
            f"за проживание, без дороги.{road}")
