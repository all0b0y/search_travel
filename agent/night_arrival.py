"""Arrival-time preference for quiet trips, families and older travellers."""
from .checks import in_clock_window, ts


def traveller_context(ctx):
    """Only a boolean enters ranking; birth dates and names stay in the profile."""
    profile = ctx.profile
    threshold = (ctx.rules.get("ranking") or {}).get("night_senior_age", 65)
    senior = profile is not None and profile.age is not None and profile.age >= threshold
    if profile is not None:
        dates = ctx.session.brief.dates.value
        on = dates.start if dates and dates.start else ctx.session.today()
        for traveller in profile.travellers:
            born = traveller.birth_date
            age = on.year - born.year - ((on.month, on.day) < (born.month, born.day))
            senior = senior or age >= threshold
    return {"_senior_travellers": bool(senior)}


def has_night_arrival(variant, window):
    """Outbound arrival and any transfer to the hotel; excludes return travel."""
    outbound = False
    for step in variant.steps:
        if step.type in ("flight", "train"):
            if outbound:
                break
            outbound = True
        elif step.type == "stay":
            break
        if outbound and step.type in ("flight", "train", "transfer") and step.end:
            if in_clock_window(ts(step.end).strftime("%H:%M"), window):
                return True
    return False


def arrival_penalty(brief, variant, rules, ranking):
    party = brief.party.value
    family = party is not None and (party.segment == "family" or party.children > 0)
    penalty = max(ranking["night_relax_penalty"] if "relax" in (brief.mood.value or []) else 0,
                  ranking["night_family_penalty"] if family else 0,
                  ranking["night_senior_penalty"] if rules.get("_senior_travellers") else 0)
    return penalty if penalty and has_night_arrival(variant, ranking["night_window"]) else 0
