from agent.guards import latin_issues


def test_lowercase_enum_codes_from_input_are_not_names():
    card_input = {"items": [{"id": "cheapest", "hotel": {"name": "Ushba Guest house"},
                             "district": {"labels": ["walkable", "quiet"]}, "meals": "breakfast"}]}
    out = {"message": "Отель Ushba Guest house в районе walkable", "items": []}
    found = str(latin_issues(out, card_input))
    assert "walkable" in found and "Ushba" not in found and "house" not in found


def test_clock_time_inside_iso_datetime_with_seconds_is_allowed():
    from agent.guards import Allowed
    a = Allowed()
    a.add_text("2026-11-24T07:55:00+02:00")
    assert (7, 55) in a.times and (2, 0) not in a.times


def test_negative_input_number_allows_its_absolute_value_and_names_keep_words():
    from agent.guards import Allowed, latin_issues
    a = Allowed()
    a.walk({"air_max_c": -3.2})
    assert 3.2 in a.nums
    card_input = {"facts": [{"kind": "hotel", "data": {"name": "Grand Kayalar Hotel"}}]}
    out = {"message": "Заселение в Grand Kayalar Hotel с 14:00.", "items": []}
    assert "Hotel" not in str(latin_issues(out, card_input))
