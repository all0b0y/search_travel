"""The accessible-room filter operates on deterministic offer facts."""
from ostrovok_mcp import service, contract


def test_accessible_room_filter_preserves_only_matching_offers(mock_env):
    request = {'destination_id': 'sochi', 'check_in': '2026-11-14', 'check_out': '2026-11-21',
               'adults': 2, 'today': '2026-10-25', 'all_rooms': True, 'limit': 40}
    all_offers = service.search_hotels(request)['offers']
    filtered = service.search_hotels({**request, 'filters': {'accessible_room': True}})
    contract.SearchHotelsOut.model_validate(filtered)
    expected = {o['offer_id'] for o in all_offers if o['accessibility']['accessible_room']}
    assert {o['offer_id'] for o in filtered['offers']} == expected
    assert filtered == service.search_hotels({**request, 'filters': {'accessible_room': True}})
    assert all(o['accessibility']['step_free_route'] and o['accessibility']['synthetic'] for o in filtered['offers'])
