"""Short breaks: more time at the destination without a long detour or an extra budget."""


def short_break(brief):
    party, dates = brief.party.value, brief.dates.value
    if not party or party.segment != "couple" or party.adults != 2 or party.children:
        return False
    if not dates:
        return False
    nights = (dates.end - dates.start).days if dates.start and dates.end else dates.nights
    return nights in (2, 3)


def destination_hours(combo):
    """Time between arrival at the hotel and leaving it, including nights and time outside the room.

    Airport/station buffers and both transfers have already been applied by ranking._evaluate.
    A missing leg or transfer cannot be presented as a complete trip with known time at the hotel.
    """
    if not all((combo.out, combo.back, combo.hotel, combo.tr_out, combo.tr_back,
                combo.arrival, combo.leave)):
        return None
    hours = (combo.leave - combo.arrival).total_seconds() / 3600
    return round(hours, 1) if hours > 0 else None


def add_weekend_choice(picked, candidates, brief, rules, limit):
    """Keep cheapest / fastest; use an optional card for a longer stay on a short break for two.

    The existing comfort detour limit bounds the round-trip road time. Never exceed the stated
    budget, and do not add a card when an existing option already has as much time at the destination.
    """
    if not short_break(brief) or not candidates:
        return
    budget = brief.budget_rub.value.max if brief.budget_rub.value else None
    if not budget:
        budget = min(combo.rub for combo in candidates) * rules["comfort_price_cap_ratio"]
    affordable = [combo for combo in candidates if combo.rub <= budget and destination_hours(combo) is not None]
    if not affordable:
        return
    fastest = min(combo.hours for combo in affordable)
    pool = [combo for combo in affordable if combo.hours <= fastest + rules["comfort_max_extra_hours"]]
    if not pool:
        return
    best = max(pool, key=lambda combo: (destination_hours(combo), -combo.hours, -combo.rub, combo.comfort))
    shown_hours = [destination_hours(combo) for _, combo in picked if combo.rub <= budget]
    if destination_hours(best) <= max((hours for hours in shown_hours if hours is not None), default=0):
        return
    if len(picked) < limit:
        picked.append(("custom-3", best))
    else:
        for index in range(len(picked) - 1, -1, -1):
            if picked[index][0] not in ("cheapest", "fastest"):
                picked[index] = ("custom-3", best)
                break
