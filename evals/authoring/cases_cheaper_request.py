"""Own offline update-card cases for cheaper requests with shorter stays."""
import tempfile

from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _engine, _last_input, _u


def cases():
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        try:
            for suffix, text in [('two_nights', 'Можно подешевле, на две ночи и попроще'),
                                 ('one_less', 'Можно дешевле, на ночь меньше')]:
                sid = engine.new_session(now=NOW)
                backend.add('understand', _u(set_={'origin.city': 'Москва', 'destination.name': 'Казань',
                    'dates.start': '2026-11-20', 'dates.end': '2026-11-24',
                    'party.adults': 2, 'party.segment': 'couple', 'budget_rub.max': 200000}))
                engine.handle(sid, {'session_id': sid, 'text': 'В Казань на четыре ночи вдвоём'})
                backend.add('understand', _u(intent='refine'))
                engine.handle(sid, {'session_id': sid, 'text': text})
                inp = _last_input(backend, 'present.update')
                out.append(case('cheaper_' + suffix, 'C', 'present.update', inp,
                    [a('ids_subset'), a('numbers_subset'),
                     a('path_equals', path='/recommended_id', value=inp['recommended_hint']),
                     pred('russian_only'), pred('no_unbacked_claims')],
                    'Own Moscow–Kazan request: code shortens the stay and recommends the lowest valid total.'))
        finally:
            engine.close()
    return out
