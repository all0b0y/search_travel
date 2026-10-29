"""An unsupported circuit never silently replaces an existing single-city trip."""
import pytest

from agent.multi_city import named_places
from .fakes import u
from .test_scopes import live  # noqa: F401


@pytest.mark.parametrize('live', ['stage1', 'stage1,guide'], indirect=True)
@pytest.mark.parametrize('text', [
    'Владимир — Суздаль — Ярославль, по ночи в каждом',
    'Хочу по две ночи в Казани и Самаре',
    'Ночёвка в Сочи, потом в Казани ещё ночёвка',
    'Посетить три города: Казань, Самара, Сочи, с ночёвками',
    'Объехать три города Золотого кольца за неделю',
    'Владимир — Суздаль — Ярославль, по ночи в каждом, без хостелов',
])
def test_multi_city_preserves_trip_and_explains_limit(live, text):
    h = live
    h.understand(u(set={'destination.name': 'Санкт-Петербург', 'scope': ['stay'],
                       'dates.start': '2026-11-20', 'dates.end': '2026-11-23',
                       'party.adults': 2, 'party.segment': 'couple'}))
    assert not h.turn('В Петербург вдвоём, только отель на три ночи').get('error')
    before = h.session().brief.values()
    h.understand(u(intent='new_trip', set={'destination.name': 'Казань', 'dates.nights': 1}))
    r = h.turn(text)
    assert not r.get('error'), r['message']
    assert 'в демо пока не собираю' in r['message']
    assert not any(c['card'] == 'guide' for c in r['debug']['calls'])
    assert h.session().brief.values() == before
    assert any('multi_city:' in n for n in h.session().trace[-1]['notes'])
    if 'Золотого кольца' not in text:
        choices = [a for a in r['actions'] if a['type'] == 'choose_destination']
        assert len(choices) >= 2
        r = h.act('choose_destination', destination_id=choices[0]['payload']['destination_id'])
        assert not r.get('error'), r['message']
        assert h.session().brief.values()['dates'] == before['dates']


@pytest.mark.parametrize('text', ['В Казань на три ночи, перелёт с пересадкой в Самаре подходит',
                                 'Маршрут Москва — Казань через два города с пересадками, ночёвки только в Казани'])
def test_single_trip_with_connection_stays_supported(live, text):
    h = live
    h.understand(u(set={'origin.city': 'Москва', 'destination.name': 'Казань',
                       'dates.start': '2026-11-20', 'dates.end': '2026-11-23',
                       'party.adults': 2, 'party.segment': 'couple'}))
    r = h.turn(text)
    assert not r.get('error') and r['cards']['variants']
    assert not any('multi_city:' in n for n in h.session().trace[-1]['notes'])


def test_named_places_keep_message_order_and_inflections():
    rows = [{'id': 'samara', 'name_ru': 'Самара'}, {'id': 'kazan', 'name_ru': 'Казань'}]
    assert [p['id'] for p in named_places(rows, 'по ночи в Казани и Самаре')] == ['kazan', 'samara']


def test_multi_city_reading_has_reason_and_empty_patch():
    from types import SimpleNamespace
    from agent.multi_city import read_multi_city
    ctx = SimpleNamespace(text='По ночи в Казани и Самаре',
        engine=SimpleNamespace(opendata=SimpleNamespace(destinations=lambda: [
            {'id': 'kazan', 'name_ru': 'Казань'}, {'id': 'samara', 'name_ru': 'Самара'}])),
        session=SimpleNamespace(shown=SimpleNamespace(destinations=[])),
        intent='new_trip', patch={'set': {'destination.name': 'Казань'}, 'unset': []},
        understand={}, trace={'understand': {}, 'notes': []}, extra_actions=[])
    assert read_multi_city(ctx)
    assert ctx.intent == 'out_of_scope' and ctx.understand['unsupported_reason'] == 'multi_city'
    assert ctx.patch == {'set': {}, 'unset': []}
