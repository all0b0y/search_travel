"""Own offline cases: reachable alternatives before accommodation without a road."""
import tempfile

from evals.authoring.cases_l3fix import a, case, pred
from evals.authoring.cases_location import NOW, _engine, _last_input, _u


def cases():
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        engine, backend = _engine(tmp)
        try:
            for origin, dest in [('Самара', 'Суздаль'), ('Новосибирск', 'Анапа')]:
                sid = engine.new_session(now=NOW)
                backend.add('understand', _u(set_={'origin.city': origin, 'destination.name': dest,
                    'dates.start': '2026-11-20', 'dates.end': '2026-11-27',
                    'party.segment': 'couple', 'party.adults': 2}))
                engine.handle(sid, {'session_id': sid, 'text': f'В {dest} вдвоём на неделю, нужна поездка с дорогой'})
                inp = _last_input(backend, 'present.variants')
                assert len(inp['other_places']) >= 2
                out.append(case('reachable_' + ('samara' if origin == 'Самара' else 'novosibirsk'),
                    'C', 'present.variants', inp,
                    [a('ids_subset'), a('numbers_subset'), pred('russian_only'), pred('no_unbacked_claims')],
                    'Own route to Suzdal: name reachable alternatives before the explicitly stay-only option.'))
        finally:
            engine.close()
    return out
