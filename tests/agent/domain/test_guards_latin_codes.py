from agent.guards import latin_issues


def test_lowercase_enum_codes_from_input_are_not_names():
    card_input = {"items": [{"id": "cheapest", "hotel": {"name": "Ushba Guest house"},
                             "district": {"labels": ["walkable", "quiet"]}, "meals": "breakfast"}]}
    out = {"message": "Отель Ushba Guest house в районе walkable", "items": []}
    found = str(latin_issues(out, card_input))
    assert "walkable" in found and "Ushba" not in found and "house" not in found
