"""Room suitability and a step-free route must be backed by separate facts."""
import re

ROOM = re.compile(r'доступн\w*\s+номер|адаптирован\w*\s+номер|номер\w*\s+для\s+(?:коляс|гост\w*\s+с\s+огранич)|номер[^.!?]{0,15}без\s+порог', re.I)
ROUTE = re.compile(r'(?:путь|маршрут|до\s+номер)[^.!?]{0,35}без\s+(?:ступен|лестниц)', re.I)


def check_accessibility(card, card_input, output, rules):
    from agent.guards import _texts, _sentences, CLAIM_NEG_RE, CLAIM_ALL_RE
    if card not in ('present.variants', 'present.update', 'present.info', 'present.compare'):
        return []
    items = card_input.get('items') or []
    pool = [i.get('facts') or {} for i in items]
    pool += [f['stay'] for f in card_input.get('facts') or [] if isinstance(f, dict) and f.get('stay')]
    own = {i.get('id'): i.get('facts') or {} for i in items}
    texts = [(output.get('message') or '', pool)]
    for item in output.get('items') or []:
        texts += [(t, [own.get(item.get('id'), {})]) for key in ('why', 'risks', 'sacrifices', 'not_for')
                  for t in _texts(item.get(key))]
    for text, evidence in texts:
        for sentence in _sentences(text):
            if CLAIM_NEG_RE.search(sentence):
                continue
            for rx, key in ((ROOM, 'accessible_room'), (ROUTE, 'step_free_route')):
                supported = [f.get(key) is True for f in evidence]
                ok = bool(supported) and (all(supported) if CLAIM_ALL_RE.search(sentence) else any(supported))
                if rx.search(sentence) and not ok:
                    return [{'code': 'accessibility_claim', 'severity': 'block',
                             'message': 'Доступность номера или пути к нему не подтверждена данными.',
                             'data': {'field': key}}]
    return []
