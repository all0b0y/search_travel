"""Own level-2 examples of unsupported overnight circuits."""
from evals.authoring.cases_l3fix import a, case, pred, understand_input

MESSAGES = [
    'Владимир — Суздаль — Ярославль, по ночи в каждом',
    'Хочу по две ночи в Казани и Самаре',
    'Ночёвка в Сочи, потом в Казани ещё ночёвка',
    'Посетить три города: Казань, Самара, Сочи, с ночёвками',
    'Объехать три города Золотого кольца за неделю',
]


def cases():
    return [case(f'multi_city_{i}', 'edge', 'understand', understand_input(text),
            [a('path_equals', path='/intent', value='out_of_scope'), pred('patch_empty')],
            'Own overnight circuit: do not replace the trip with one city. Code also intercepts a mistaken model patch.')
            for i, text in enumerate(MESSAGES, 1)]
