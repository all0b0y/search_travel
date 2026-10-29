"""Accessible entrance alone is insufficient; use room and route facts."""
import pytest

from agent.accessibility import suitable
from agent.claims.accessibility import check_accessibility
from ostrovok_mcp.hotels import accessibility
from .fakes import u
from .test_scopes import live, TRAVELLERS1  # noqa: F401


def test_accessibility_is_deterministic_and_room_requires_route():
    rows = [{'hotel_id': f'own-access-{i}', 'kind': 'hotel', 'stars': 4} for i in range(100)]
    values = [accessibility(h) for h in rows]
    assert values == [accessibility(dict(h)) for h in rows]
    assert any(a['accessible_room'] for a in values)
    assert any(a['step_free'] and not a['accessible_room'] for a in values)
    assert all(a['synthetic'] and (not a['accessible_room'] or a['step_free_route']) for a in values)
    assert not suitable({'accessibility': {'step_free': True, 'lift': True}})
    assert not accessibility({'hotel_id': 'hostel', 'kind': 'hostel'})['accessible_room']


@pytest.mark.parametrize('text', ['Папа на инвалидной коляске, нужен подходящий номер',
                                 'Еду с ходунками, нужен подходящий номер',
                                 'Маме тяжело с лестницами', 'Он не может по лестнице',
                                 'Нужен номер без порогов'])
def test_accessibility_cues_after_selection(live, text):
    h = live
    h.understand(u(set={'destination.name': 'Санкт-Петербург', 'scope': ['stay'],
                       'dates.start': '2026-11-20', 'dates.end': '2026-11-23',
                       'party.adults': 1, 'party.segment': 'solo'}))
    assert not h.turn('Петербург на три ночи, только отель').get('error')
    h.understand(u(intent='refine'))
    r = h.turn(text)
    assert not r.get('error'), r['message']
    assert 'accessible' in h.session().brief.values()['constraints']
    assert all(v.facts['accessible_room'] and v.facts['step_free_route'] for v in h.session().variants)
    v = h.session().variants[0]
    h.act('choose_variant', variant_id=v.id)
    r = h.act('checkout', travellers=TRAVELLERS1, contact={'email': 'access@example.com'})
    assert not r.get('error'), r['message']
    r = h.act('pay', order_id=r['order']['order_id'])
    assert r['order']['status'] == 'paid'


def test_room_and_route_claims_require_separate_evidence():
    output = {'message': 'Есть доступный номер и путь до номера без ступенек.'}
    assert check_accessibility('present.variants', {'items': [{'facts': {'step_free': True}}]}, output, {})
    good = {'items': [{'facts': {'accessible_room': True, 'step_free_route': True}}]}
    assert not check_accessibility('present.variants', good, output, {})
    assert check_accessibility('present.variants', {'items': [{'facts': {'accessible_room': True}}]}, output, {})
