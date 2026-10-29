"""A cost reduction request changes nights and the actual recommendation."""
import json

import pytest

from agent.claims.cheaper import check_cheaper
from .fakes import u
from .test_scopes import live  # noqa: F401


@pytest.mark.parametrize('text,nights,patch', [('Можно подешевле, на две ночи и попроще', 2, {}),
                                        ('Можно дешевле, на ночь меньше', 3, {}),
                                        ('Уложи в сто пятьдесят тысяч', 4, {'budget_rub.max': 150000})])
def test_cheaper_request_reconciles_dates_and_recommends_lowest(live, text, nights, patch):
    h = live
    h.understand(u(set={'origin.city': 'Москва', 'destination.name': 'Казань',
                       'dates.start': '2026-11-20', 'dates.end': '2026-11-24',
                       'party.adults': 2, 'party.segment': 'couple', 'budget_rub.max': 200000}))
    assert not h.turn('Казань на четыре ночи вдвоём').get('error')
    h.understand(u(intent='refine', set=patch))
    r = h.turn(text)
    assert not r.get('error'), r['message']
    inp = json.loads([q for q in h.backend.requests if q.card == 'present.update'][-1].user)
    vs = h.session().variants
    picked = next(v for v in vs if v.id == inp['recommended_hint'])
    assert picked.totals.rub == min(v.totals.rub for v in vs)
    assert [v['id'] for v in r['cards']['variants'] if v['recommended']] == [picked.id]
    dates = h.session().brief.values()['dates']
    assert dates['nights'] == nights
    assert dates['end'] == f'2026-11-{20+nights}'


def test_cheaper_claim_guard_rejects_other_recommendation():
    inp = {'request': 'Можно подешевле', 'recommended_hint': 'cheapest'}
    assert check_cheaper('present.update', inp, {'recommended_id': 'comfort'}, {})
    assert not check_cheaper('present.update', inp, {'recommended_id': 'cheapest'}, {})
    assert not check_cheaper('present.update', {**inp, 'request': 'Другой отель'}, {'recommended_id': 'comfort'}, {})


def test_higher_total_gets_a_code_explanation():
    from types import SimpleNamespace
    from agent.cheaper_request import explanation
    ctx = SimpleNamespace(ask_cheaper=True, trace={'cheaper_reference_rub': 19000},
                          card_input={'recommended_hint': 'cheapest'},
                          variants=[SimpleNamespace(id='cheapest', totals=SimpleNamespace(rub=21000))])
    text = explanation(ctx).replace('\u00a0', ' ')
    assert 'не нашёл' in text and '19 000' in text and '21 000' in text
    ctx.variants[0].totals.rub = 18000
    assert not explanation(ctx)
