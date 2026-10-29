"""Reject unsupported overnight circuits without changing the current trip."""
import re

EACH = re.compile(r'по\s+(?:(?:одной|две|двум|три|\d+)\s+)?ноч(?:и|ь|ей)\b|'
                  r'ноч[её]в\w*[^.!?]{0,30}(?:кажд|город)|'
                  r'(?:ночь|ноч[её]в\w*)[^.!?]{0,45}(?:затем|потом)|'
                  r'(?:затем|потом)[^.!?]{0,45}(?:ночь|ноч[её]в)', re.I)
CIRCUIT = re.compile(r'(?:объехать|посетить|маршрут)[^.!?]{0,45}'
                     r'(?:несколько|два|двух|три|тр[её]х|четыре|\d+)\s+город', re.I)


def named_places(rows, text):
    found = []
    for row in rows:
        name = row.get('name_ru') or row.get('name')
        if not name or not row.get('id'):
            continue
        names = [name] + list(row.get('aliases') or [])
        positions = []
        for alias in names:
            if not isinstance(alias, str) or len(alias) < 4:
                continue
            stem = alias[:-1] if alias[-1:].lower() in 'аьяй' else alias
            m = re.search(r'(?<!\w)' + re.escape(stem) + r'[а-яё]{0,3}(?!\w)', text, re.I)
            if m:
                positions.append(m.start())
        if positions:
            found.append((min(positions), {'id': row['id'], 'name': name}))
    return [p for _, p in sorted(found, key=lambda x: x[0])]


def read_multi_city(ctx):
    text = ctx.text or ''
    circuit = bool(CIRCUIT.search(text))
    if not circuit and not EACH.search(text):
        return False
    method = getattr(ctx.engine.opendata, 'destinations', None)
    places = named_places(method() if method else [], text)
    if not circuit and len(places) < 2:
        return False
    from .state import ShownItem
    ctx.intent = 'out_of_scope'
    ctx.patch = {'set': {}, 'unset': []}
    ctx.understand.update(intent='out_of_scope', patch=ctx.patch, unsupported_reason='multi_city')
    ctx.trace['understand'].update(routed_intent='out_of_scope', reason='multi_city', patch=ctx.patch)
    ctx.trace['notes'].append('multi_city: overnight circuit unsupported; trip brief unchanged')
    ctx.message = 'Маршрут с ночёвками в нескольких городах в демо пока не собираю.'
    choices = places[:5]
    if choices:
        ctx.message += ' Могу подобрать поездку в один город: ' + ', '.join(p['name'] for p in choices) + ' — кнопки ниже.'
    else:
        ctx.message += ' Назовите один город — подберу поездку туда.'
    existing = {p.id for p in ctx.session.shown.destinations}
    for p in choices:
        if p['id'] not in existing:
            ctx.session.shown.destinations.append(ShownItem(label=str(len(ctx.session.shown.destinations) + 1),
                kind='destination', id=p['id'], title=p['name']))
            existing.add(p['id'])
    ctx.extra_actions += [{'type': 'choose_destination', 'label': 'Поехать в ' + p['name'],
                          'payload': {'destination_id': p['id']}} for p in choices]
    return True
