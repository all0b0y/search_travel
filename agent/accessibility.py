"""Accessible rooms require both room suitability and the route to the room."""
import re


def facts(offer):
    acc = (offer or {}).get('accessibility') or {}
    return {key: acc.get(key) for key in ('accessible_room', 'step_free_route')} | {
        'accessibility_synthetic': acc.get('synthetic')}


def suitable(offer):
    acc = (offer or {}).get('accessibility') or {}
    return acc.get('accessible_room') is True and acc.get('step_free_route') is True


def read_cues(ctx, brief):
    from .state import apply_patch
    patterns = (ctx.rules.get('accessibility_cues') or {}).get('patterns') or []
    if not any(re.search(p, ctx.text or '', re.I) for p in patterns):
        return brief
    if 'accessible' in (brief.values().get('constraints') or []):
        return brief
    new, _ = apply_patch(brief, {'set': {'constraints.add': ['accessible']}, 'unset': []})
    return new
