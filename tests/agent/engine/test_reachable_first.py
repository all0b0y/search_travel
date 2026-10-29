"""Own routes without transport: complete alternatives precede stay-only prices."""
import json

import pytest

from .fakes import u
from .test_scopes import live  # noqa: F401


@pytest.mark.parametrize('origin,dest', [('Самара', 'Суздаль'), ('Новосибирск', 'Анапа')])
def test_reachable_places_precede_stay_only_and_can_be_chosen(live, origin, dest):
    h = live
    h.understand(u(set={'origin.city': origin, 'destination.name': dest,
                       'dates.start': '2026-11-20', 'dates.end': '2026-11-27',
                       'party.segment': 'couple', 'party.adults': 2}))
    r = h.turn(f'В {dest} вдвоём на неделю, нужна поездка с дорогой')
    assert not r.get('error'), r['message']
    inp = json.loads([q for q in h.backend.requests if q.card == 'present.variants'][-1].user)
    places = inp['other_places']
    assert len(places) >= 2
    text = r['message']
    assert text.index('Похожие места') < text.index('только проживание')
    assert 'без дороги' in text
    offered = [a['payload']['destination_id'] for a in r['actions'] if a['type'] == 'choose_destination']
    assert all(p['id'] in offered and p['name'] in text for p in places)
    r = h.act('choose_destination', destination_id=places[0]['id'])
    assert not r.get('error'), r['message']
    assert any(s.type in ('flight', 'train') for v in h.session().variants for s in v.steps)
