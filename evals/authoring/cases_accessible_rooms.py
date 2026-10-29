"""Own offline examples of accessible rooms after the first selection."""
import tempfile

from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _engine, _last_input, _u


def cases():
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        try:
            for suffix, text in [('walker', 'Еду с ходунками, нужен подходящий номер'),
                                 ('threshold', 'Нужен номер без порогов')]:
                sid = engine.new_session(now=NOW)
                backend.add('understand', _u(set_={'destination.name': 'Санкт-Петербург', 'scope': ['stay'],
                    'dates.start': '2026-11-20', 'dates.end': '2026-11-23',
                    'party.adults': 1, 'party.segment': 'solo'}))
                engine.handle(sid, {'session_id': sid, 'text': 'Петербург на три ночи, только отель'})
                backend.add('understand', _u(intent='refine'))
                engine.handle(sid, {'session_id': sid, 'text': text})
                inp = _last_input(backend, 'present.update')
                assert all(i['facts']['accessible_room'] and i['facts']['step_free_route'] for i in inp['items'])
                out.append(case('accessible_room_' + suffix, 'C', 'present.update', inp,
                    [a('ids_subset'), a('numbers_subset'), pred('russian_only'), pred('no_unbacked_claims')],
                    'Own example: accessible entrance alone is insufficient. Room and route facts are synthetic.'))
        finally:
            engine.close()
    return out
