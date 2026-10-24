"""Canonical engine step names (AGENT_PLAN.md sections 4-5).

The engine's step registry (agent/steps.py, built later) implements exactly these
names; the skill loader rejects routes that use any other name.
"""

STEP_DOCS = {
    "apply_patch": "Apply the understand patch or the action's deterministic patch to the brief, with provenance.",
    "resolve_refs": "Resolve references ('второй', '1', ids) to shown destinations, variants, hotels or questions.",
    "image_match": "Use the understand image output: verify a recognized place by geocoder, else map picture traits to brief traits.",
    "find_destinations": "Pick candidate destinations from open data by traits, month, origin, budget, travel time, segment.",
    "quote_destinations": "Get cheapest flight+stay estimates for candidate destinations from the mock (quote_destinations).",
    "search_flights": "Search flights for the brief through the mock.",
    "search_trains": "Search trains for the brief through the mock (domestic routes only).",
    "search_transfers": "Search airport/station <-> hotel transfers through the mock.",
    "search_hotels": "Search hotel offers for the brief through the mock.",
    "search": "Run every search the brief's scope needs (flights/trains, stay, transfers), using prefetched results.",
    "build_variants": "Build the cheapest / fastest / comfort variants with totals, facts, sacrifices, risks, not_for, fit.",
    "check": "Run the validation hooks (agent/checks.py: chain, buffers, budget, dates, segment fit); one rerun on failure.",
    "sensitivity": "Decide questions (<= 2) and chips: ask only when an answer would change the result.",
    "ask_or_assume": "Jev ask-or-assume decision for candidate questions (stage 3+); falls back to rules.",
    "upsell": "Match upsell.json rules against the brief and variants; attach candidates with reasons.",
    "compare": "Build the comparison table for the referenced variants (or all shown).",
    "info_lookup": "Answer an info question from tools only (climate, district, hotel policies, order data).",
    "cart_add": "Put the chosen variant's offers (plus chosen extras) into the mock cart.",
    "checkout": "Create the order from the cart with travellers and contact; run the after_travellers hook.",
    "pay": "Pay the order with the mock card.",
    "post_purchase": "Run an order action (early check-in, change dates, add transfer) through the mock.",
    "log_event": "Write an anonymous analytics event (insights_log_event).",
}

STEP_NAMES = tuple(STEP_DOCS)
