"""A cheaper request must follow the code's cheapest valid recommendation."""
from agent.cheaper_request import requested


def check_cheaper(card, card_input, output, rules):
    if card not in ('present.variants', 'present.update') or not requested(
            card_input.get('request'), card_input.get('changes') or []):
        return []
    hint = card_input.get('recommended_hint')
    if not hint or output.get('recommended_id') == hint:
        return []
    return [{'code': 'cheaper_recommendation', 'severity': 'block',
             'message': 'При просьбе дешевле рекомендуйте самый дешёвый подходящий вариант из recommended_hint.',
             'data': {'recommended_hint': hint}}]
