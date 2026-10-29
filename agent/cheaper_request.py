"""Code interpretation and recommendation for a request to reduce trip cost."""
import re

WORDS = {'одну': 1, 'одна': 1, 'две': 2, 'два': 2, 'три': 3, 'четыре': 4, 'пять': 5,
         'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10}
CHEAPER = re.compile(r'подешевл|попроще|поскромнее|\bдорого\b|(?:можно|ещ[её]|хочу|сделай|вариант)\s+дешевле', re.I)
NIGHTS = re.compile(r'\b(\d{1,2}|' + '|'.join(WORDS) + r')\s+ноч(?:ь|и|ей)\b', re.I)


def requested(text, changes=()):
    return bool(CHEAPER.search(text or '')) or any(
        c.get('field') == 'budget_rub.max' and isinstance(c.get('before'), (int, float))
        and isinstance(c.get('after'), (int, float)) and c['after'] < c['before'] for c in changes)


def read_request(ctx):
    brief = ctx.session.brief.values()
    old = (brief.get('budget_rub') or {}).get('max')
    new = ctx.patch['set'].get('budget_rub.max')
    lower = isinstance(old, (int, float)) and isinstance(new, (int, float)) and new < old
    if not (ctx.ask_cheaper or requested(ctx.text) or lower):
        return
    ctx.ask_cheaper = True
    from .ranking import recommended_hint
    from .payloads import card_hint
    variants = ctx.session.variants
    hint = card_hint(variants, recommended_hint(variants), old)
    previous = next((v for v in variants if v.id == hint), None)
    if previous:
        ctx.trace['cheaper_reference_rub'] = previous.totals.rub
    saved = ctx.session.results.get('engine:last_recommendation')
    if saved and isinstance(saved.data, dict) and isinstance(saved.data.get('rub'), (int, float)):
        ctx.trace['cheaper_reference_rub'] = saved.data['rub']
    match = NIGHTS.search(ctx.text or '')
    nights = None
    if match:
        word = match.group(1).lower()
        nights = int(word) if word.isdigit() else WORDS[word]
    elif re.search(r'на\s+(?:одну\s+)?ночь\s+меньше', ctx.text or '', re.I):
        dates = brief.get('dates') or {}
        nights = dates.get('nights')
        if not nights and dates.get('start') and dates.get('end'):
            from datetime import date
            nights = (date.fromisoformat(dates['end']) - date.fromisoformat(dates['start'])).days
        nights = nights - 1 if nights else None
    if nights and 1 <= nights <= 90:
        ctx.patch['set']['dates.nights'] = nights
    ctx.trace['notes'].append('cheaper: recommend the cheapest valid variant')


def recommendation(ctx, default):
    if not ctx.ask_cheaper:
        return default
    from .checks import blocking, check_variant
    valid = [v for v in ctx.variants if not blocking(check_variant(v, ctx.session.brief, ctx.rules))]
    return min(valid, key=lambda v: v.totals.rub).id if valid else default


def explanation(ctx):
    old = ctx.trace.get('cheaper_reference_rub')
    hint = (ctx.card_input or {}).get('recommended_hint')
    best = next((v for v in ctx.variants if v.id == hint), None)
    if not ctx.ask_cheaper or old is None or best is None or best.totals.rub <= old:
        return ''
    from .payloads import fmt_rub
    return (f'На новых условиях дешевле прежней рекомендации за {fmt_rub(old)} не нашёл: '
            f'самый дешёвый подходящий вариант сейчас — {fmt_rub(best.totals.rub)}. ')


def remember(ctx, response):
    from .state import CachedResult
    from .steps import store_result
    rec = next((v for v in response['cards']['variants'] if v.get('recommended')), None)
    if rec:
        store_result(ctx.session, CachedResult(key='engine:last_recommendation', kind='recommendation',
            params={}, fetched_at=ctx.session.clock(), data={'id': rec['id'], 'rub': rec['totals']['rub']}))
