"""Jev decisions of the engine (decision 12; flags jev_routing, jev_ask, jev_guard).

Questions are built from skill data (questions.json texts and choices, segments.json `jev_criteria`,
rules.json `jev` thresholds); nothing here is hard-wired to particular question texts.

    route_short_reply(decide, session, text, skill, trace) -> {"type": action_type, "payload": {...}} | None
    ask_or_assume(decide, brief, questions, skill, trace)    -> (ask: [Question], assumed: [(field, value)])
    guard_card_output(decide, card, card_input, output, skill, trace) -> [Issue]

`decide` has agent.jev.decide's signature (`state, questions, timeout_s`). Every call is fail-open
(None, an exception, or low confidence -> the caller's rule path) and leaves a record in `trace`
(`{use, ok, answer, confidence, accepted, latency_ms, cost_usd}`).
"""
import time

from .jev import choice, choice_answer, noul, noul_answer

NONE_OPTION = "none"
DEFAULT_SHORT_REPLY_WORDS = 4


def _jev_rules(skill):
    rules = dict(skill.rules.get("jev") or {})
    rules.setdefault("routing", 0.8)
    rules.setdefault("ask_or_assume", 0.8)
    rules.setdefault("guard_fail", 0.5)
    rules.setdefault("timeout_s", 1.5)
    return rules


def _call(decide, state, questions, timeout_s, trace, use):
    started = time.perf_counter()
    record = {"use": use, "ok": False, "questions": sorted(questions)}
    try:
        result = decide(state, questions, timeout_s=timeout_s)
    except Exception as exc:  # fail-open by contract
        result = None
        record["error"] = f"{type(exc).__name__}: {exc}"
    meta = (result or {}).get("meta") or {}
    record["ok"] = result is not None
    record["latency_ms"] = meta.get("latency_ms", round((time.perf_counter() - started) * 1000, 1))
    record["cost_usd"] = meta.get("cost_usd")
    record["model"] = meta.get("model")
    record["answers"] = (result or {}).get("answers")   # raw Jev answers (probabilities) for the debug panel
    if trace is not None:
        trace.append(record)
    return result, record


def is_short_reply(text, skill):
    limit = int(_jev_rules(skill).get("short_reply_max_words", DEFAULT_SHORT_REPLY_WORDS))
    words = (text or "").split()
    return 0 < len(words) <= limit


def _segment_criteria(skill, value):
    seg = (skill.segments or {}).get(value) or {}
    return seg.get("jev_criteria")


def route_short_reply(decide, session, text, skill, trace=None):
    """Resolve a short reply to a pending question or a reference to a shown item without Flash.

    Returns the equivalent UI action ({"type": "answer_question"|"choose_destination"|"choose_variant",
    "payload": {...}}) when Jev picks one option with confidence >= rules.jev.routing, else None.
    """
    if decide is None or not is_short_reply(text, skill):
        return None
    options, mapping = {}, {}
    for q in session.pending_questions:
        for i, c in enumerate(q.choices):
            key = f"{q.id}__{i}"
            crit = None
            if q.field == "party.segment":
                crit = _segment_criteria(skill, c.value)
            options[key] = (f"The reply answers the question '{q.text}' with '{c.label}'."
                            + (f" {crit}" if crit else ""))
            mapping[key] = {"type": "answer_question", "payload": {"question_id": q.id, "value": c.value}}
    for kind, items, action, field in (("destination", session.shown.destinations, "choose_destination",
                                        "destination_id"),
                                       ("variant", session.shown.variants, "choose_variant", "variant_id")):
        for item in items:
            key = f"{kind}__{item.label}"
            options[key] = f"The reply picks the shown {kind} number {item.label}: {item.title}."
            mapping[key] = {"type": action, "payload": {field: item.id}}
    if not options:
        return None
    options[NONE_OPTION] = ("None of the above: the reply asks something, adds new wishes, changes the request, "
                            "or is unclear.")
    rules = _jev_rules(skill)
    state = {"stage": session.stage, "reply": text,
             "pending_questions": [{"id": q.id, "text": q.text, "choices": [c.label for c in q.choices]}
                                   for q in session.pending_questions],
             "shown": [{"kind": i.kind, "label": i.label, "title": i.title}
                       for i in session.shown.destinations + session.shown.variants]}
    questions = {"route": choice("Which option does the user's short reply select? Choose 'none' unless the "
                                 "reply clearly selects exactly one option.", options)}
    result, record = _call(decide, state, questions, rules["timeout_s"], trace, "routing")
    picked, confidence = choice_answer(result, "route")
    accepted = picked is not None and picked != NONE_OPTION and confidence >= rules["routing"]
    record.update(answer=picked, confidence=confidence, threshold=rules["routing"], accepted=accepted)
    return mapping[picked] if accepted else None


def recommended_choice(question):
    return next((c for c in question.choices if c.recommended), None)


def ask_or_assume(decide, brief, questions, skill, trace=None):
    """For each candidate question with a recommended choice ask Jev whether assuming it is safe.

    P(safe) >= rules.jev.ask_or_assume -> the question is dropped and (field, value) returned for an
    assumption (shown as a chip). Jev unavailable -> every question is asked (rules path).
    """
    candidates = [q for q in questions if recommended_choice(q) is not None]
    if decide is None or not candidates:
        return list(questions), []
    rules = _jev_rules(skill)
    specs = {}
    for q in candidates:
        rec = recommended_choice(q)
        specs[q.id] = noul(
            f"The assistant wants to ask the user '{q.text}' (options: {', '.join(c.label for c in q.choices)}). "
            f"Is it safe to skip the question and assume '{rec.label}', showing it as an editable chip?",
            "The request implies this answer, or a wrong guess would barely change the offered trips.",
            "The answer is unclear from the request and a wrong guess would noticeably change prices, "
            "dates or suitability.")
    state = {"brief": brief.values()}
    result, record = _call(decide, state, specs, rules["timeout_s"], trace, "ask_or_assume")
    if result is None:
        record.update(accepted=False, fallback="ask_all")
        return list(questions), []
    ask, assumed, answers = [], [], {}
    for q in questions:
        p = noul_answer(result, q.id) if q.id in specs else None
        answers[q.id] = p
        if p is not None and p >= rules["ask_or_assume"]:
            assumed.append((q.field, recommended_choice(q).value))
        else:
            ask.append(q)
    record.update(answer=answers, threshold=rules["ask_or_assume"], accepted=bool(assumed))
    return ask, assumed


def guard_card_output(decide, card, card_input, output, skill, trace=None):
    """Semantic guard on a present card: P(unsupported or contradicting) >= rules.jev.guard_fail -> block."""
    if decide is None:
        return []
    rules = _jev_rules(skill)
    specs = {"unsupported": noul(
        "The assistant reply below was written only from the JSON input. Does it state facts, numbers, "
        "places, dates, amenities or promises that the input does not support, or contradict the input?",
        "The reply invents or contradicts something.",
        "Everything in the reply is supported by the input.")}
    state = {"card": card, "input": card_input, "reply": output}
    result, record = _call(decide, state, specs, rules["timeout_s"], trace, "guard")
    p = noul_answer(result, "unsupported")
    failed = p is not None and p >= rules["guard_fail"]
    record.update(answer=p, threshold=rules["guard_fail"], accepted=not failed)
    if not failed:
        return []
    return [{"code": "jev_unsupported", "severity": "block",
             "message": "The reply states something the input does not support; stick to the input facts.",
             "data": {"p": p}}]
