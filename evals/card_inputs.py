"""Binding shapes of card inputs (AGENT_PLAN.md "Card inputs").

    validate_card_input("present.variants", payload) -> [error strings]   # [] when valid

The top-level key sets are exact (no missing, no extra keys); nested objects are checked for
their documented keys. `brief` must be a valid `Brief.values()` result. This module is the
single place evals encode the card-input contract; tests run every case through it.
"""
import jsonschema

from agent.state import (ALLOWED_PATCH_PATHS, DISTRICT_LABELS, SHOWN_KINDS, STAGES, Brief)

COMMON_KEYS = ("today", "stage", "brief", "assumed", "assumptions")
CARD_KEYS = {
    "understand": ("shown", "pending_questions", "history", "message", "has_image"),
    "present.destinations": ("items", "questions", "image", "insight"),
    "present.variants": ("items", "recommended_hint", "questions", "upsell_candidates"),
    "present.update": ("items", "recommended_hint", "questions", "upsell_candidates", "changes", "rebuilt", "request"),
    "present.compare": ("items", "dimensions"),
    "present.info": ("question", "facts"),
    "present.aftercare": ("order", "result"),
    "present.fallback": ("message", "reason"),
}
CARDS = tuple(CARD_KEYS)
UPSELL_KINDS = ("transfer", "early_checkin", "room_upgrade", "meal_upgrade", "flexible_fare")  # decision 8


def input_keys(card):
    return set(COMMON_KEYS) | set(CARD_KEYS[card])


def _obj(required, props=None, extra=False):
    props = dict(props or {})
    for key in required:
        props.setdefault(key, {})
    return {"type": "object", "required": list(required), "properties": props, "additionalProperties": extra}


DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
DATETIME = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$"}
NUM_OR_NULL = {"type": ["number", "null"]}
NOTE = _obj(["code", "text", "data"], {"code": {"type": "string"}, "text": {"type": "string", "minLength": 1},
                                       "data": {"type": "object"}})
QUESTION_OUT = _obj(["id", "text"], {"id": {"type": "string"}, "text": {"type": "string", "minLength": 1}})
ASSUMPTION = _obj(["field", "label"], {"field": {"enum": list(ALLOWED_PATCH_PATHS)},
                                       "label": {"type": "string", "minLength": 1}})
STEP = _obj(["type", "from", "to", "start", "end", "carrier", "price_rub"], {
    "type": {"enum": ["taxi", "flight", "train", "transfer", "stay"]},
    "from": {"type": "string"}, "to": {"type": "string"}, "start": DATETIME, "end": DATETIME,
    "carrier": {"type": ["string", "null"]}, "price_rub": {"type": ["integer", "null"], "minimum": 0}})
HOTEL = _obj(["name", "stars", "rating", "district"], {
    "name": {"type": "string"}, "stars": {"type": ["integer", "null"]}, "rating": NUM_OR_NULL,
    "district": _obj(["labels", "counts"], {
        "labels": {"type": "array", "items": {"enum": list(DISTRICT_LABELS)}},
        "counts": {"type": "object", "additionalProperties": {"type": "integer"}}})})
# totals: door_to_door_hours = both ways together; out_hours / back_hours = one way each (null without that
# direction). diff_rub / diff_word: the gap to the recommended item (0 / null for it), over_budget_rub: above
# brief.budget_rub.max (null when within or no budget) — ready numbers, the card never subtracts.
VARIANT_ITEM = _obj(["id", "label", "title", "totals", "steps", "hotel", "facts", "sacrifices", "risks", "not_for",
                     "fit", "diff_rub", "diff_word", "over_budget_rub"], {
    "id": {"type": "string", "pattern": "^(cheapest|fastest|comfort|custom-[0-9]+)$"},
    "label": {"type": "string"}, "title": {"type": "string"},
    "totals": _obj(["rub", "door_to_door_hours", "out_hours", "back_hours"], {
        "rub": {"type": "integer"}, "door_to_door_hours": {"type": "number"},
        "out_hours": NUM_OR_NULL, "back_hours": NUM_OR_NULL}),
    "diff_rub": {"type": "integer", "minimum": 0}, "diff_word": {"enum": ["дешевле", "дороже", None]},
    "over_budget_rub": {"type": ["integer", "null"], "minimum": 1},
    "steps": {"type": "array", "items": STEP, "minItems": 1},
    "hotel": {"oneOf": [{"type": "null"}, HOTEL]},
    "facts": {"type": "object"},
    "sacrifices": {"type": "array", "items": NOTE}, "risks": {"type": "array", "items": NOTE},
    "not_for": {"type": "array", "items": NOTE},
    "fit": _obj(["score", "breakdown"], {"score": {"type": "number", "minimum": 0, "maximum": 1},
                                         "breakdown": {"type": "object"}})})
DESTINATION_ITEM = _obj(["id", "label", "name", "country", "price_from_rub", "nights", "party_label", "weather",
                         "travel_time_h", "match", "facts"], {
    "id": {"type": "string"}, "label": {"type": "string"}, "name": {"type": "string"},
    "country": {"type": ["string", "null"]}, "price_from_rub": {"type": ["integer", "null"]},
    "nights": {"type": "integer", "minimum": 1}, "party_label": {"type": "string"},
    "weather": {"oneOf": [{"type": "null"}, _obj(["air_max_c", "sea_c", "rain_days"], {
        "air_max_c": {"type": "number"}, "sea_c": NUM_OR_NULL, "rain_days": {"type": "number"}})]},
    "travel_time_h": NUM_OR_NULL, "match": {"type": "array", "items": {"type": "string"}},
    "facts": {"type": "object"}})
UPSELL_CANDIDATE = _obj(["candidate_id", "variant_id", "kind", "label", "price_rub", "reason"], {
    "candidate_id": {"type": "string"}, "variant_id": {"type": "string"},
    "kind": {"enum": list(UPSELL_KINDS)}, "label": {"type": "string"},
    "price_rub": {"type": ["integer", "null"]}, "reason": {"type": "string", "minLength": 1}})

_COMMON_PROPS = {
    "today": DATE,
    "stage": {"enum": list(STAGES)},
    "brief": {"type": "object"},
    "assumed": {"type": "array", "items": {"type": "string"}},
    "assumptions": {"type": "array", "items": ASSUMPTION},
}
_CARD_PROPS = {
    "understand": {
        "shown": {"type": "array", "items": _obj(["label", "kind", "id", "title"], {
            "label": {"type": "string"}, "kind": {"enum": list(SHOWN_KINDS)}, "id": {"type": "string"},
            "title": {"type": "string"}})},
        "pending_questions": {"type": "array", "items": _obj(["id", "field", "text", "choices"], {
            "id": {"type": "string"}, "field": {"enum": list(ALLOWED_PATCH_PATHS)}, "text": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}}})},
        "history": {"type": "array", "items": _obj(["role", "text"], {"role": {"enum": ["user", "agent"]},
                                                                      "text": {"type": "string"}})},
        "message": {"type": "string"},
        "has_image": {"type": "boolean"},
    },
    "present.destinations": {
        "items": {"type": "array", "items": DESTINATION_ITEM},
        "questions": {"type": "array", "items": QUESTION_OUT, "maxItems": 2},
        "image": {"type": ["object", "null"]},
        "insight": {"oneOf": [{"type": "null"}, _obj(["popular_ids"], {"popular_ids": {"type": "array"}})]},
    },
    "present.variants": {
        "items": {"type": "array", "items": VARIANT_ITEM},
        "recommended_hint": {"type": ["string", "null"]},
        "questions": {"type": "array", "items": QUESTION_OUT, "maxItems": 2},
        "upsell_candidates": {"type": "array", "items": UPSELL_CANDIDATE},
    },
    "present.compare": {
        "items": {"type": "array", "items": VARIANT_ITEM, "minItems": 2, "maxItems": 3},
        "dimensions": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "present.info": {"question": {"type": "string", "minLength": 1}, "facts": {"type": ["array", "object"]}},
    "present.aftercare": {"order": {"type": "object"}, "result": {"type": "object"}},
    "present.fallback": {"message": {"type": "string"},
                         "reason": {"enum": ["smalltalk", "out_of_scope", "unknown"]}},
}
# changes rows: brief paths (dates.shift_days, dates.start, party.pets, ...) and per-variant totals
# `totals.<variant_id>.rub` (after null = the variant is gone); `rebuilt` lists what was searched again.
CHANGE_FIELD = {"type": "string", "pattern": r"^([a-z_]+(\.[a-z_]+)?|totals\.(cheapest|fastest|comfort|custom-[0-9]+)\.rub)$"}
_CARD_PROPS["present.update"] = dict(_CARD_PROPS["present.variants"], changes={
    "type": "array", "items": _obj(["field", "before", "after"], {"field": CHANGE_FIELD})},
    rebuilt={"type": "array", "items": {"enum": ["flights", "trains", "stay", "transfers"]}},
    request={"type": "string"})   # the user's words (a wish that is no filter is named from them)


def card_input_schema(card):
    if card not in CARD_KEYS:
        raise KeyError(f"unknown card: {card}")
    props = dict(_COMMON_PROPS)
    props.update(_CARD_PROPS[card])
    return _obj(sorted(input_keys(card)), props)


def _check_brief(values):
    errors = []
    if not isinstance(values, dict):
        return ["brief must be an object (Brief.values())"]
    fields = set(Brief.model_fields)
    if set(values) != fields:
        errors.append(f"brief keys must be exactly {sorted(fields)}; got {sorted(values)}")
        return errors
    try:
        Brief.model_validate({k: {"value": v} for k, v in values.items()})
    except Exception as exc:  # pydantic ValidationError
        errors.append(f"brief values invalid: {str(exc).splitlines()[0]} ...")
    return errors


def validate_card_input(card, payload):
    if card not in CARD_KEYS:
        return [f"unknown card: {card}"]
    if not isinstance(payload, dict):
        return ["input must be an object"]
    errors = []
    want, got = input_keys(card), set(payload)
    if want - got:
        errors.append(f"missing keys: {sorted(want - got)}")
    if got - want:
        errors.append(f"extra keys: {sorted(got - want)}")
    validator = jsonschema.Draft7Validator(card_input_schema(card))
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))[:10]:
        where = "/".join(str(p) for p in err.absolute_path) or "$"
        if err.validator in ("required", "additionalProperties") and where == "$":
            continue  # already reported as missing/extra keys
        errors.append(f"{where}: {err.message}")
    if "brief" in payload:
        errors += _check_brief(payload["brief"])
    for path in payload.get("assumed") or []:
        top = str(path).partition(".")[0]
        if top not in Brief.model_fields:
            errors.append(f"assumed: unknown brief path {path}")
    return errors
