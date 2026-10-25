"""Named places inside a destination (opendata `pois`): «рядом с Айя-Софией», «в районе Султанахмет».

    index = PoiIndex(rows)                 # rows of one destination (OpenData.pois_rows), built once and cached
    index.find("Айя-Софией")               # -> the best poi record or None
    near_phrases(text, cues)               # «рядом с X», «в районе X» -> ["X", ...] (the words after a cue)

Matching (strict on purpose: a wrong place is worse than none):
- every text is normalised like opendata.norm_name (case, ё/е, punctuation, stress marks, hyphens);
- words match when equal, when they share a stem after Russian case endings («Софией» ~ «София»,
  «Кремлём» ~ «Кремль», «дворца» ~ «дворец») or when their Latin keys match (a Cyrillic word transliterated,
  diacritics dropped, ph→f, y/j→i, doubled letters collapsed: «Галата» ~ «Galata»);
- a whole phrase also matches by its joined key («Айя-София» ~ «Ayasofya», «Султанахмет» ~ «Sultan Ahmet»),
  case endings allowed at the end («Айя-Софией»);
- a query may name only part of a long name («Эрмитаж» ~ «Государственный Эрмитаж») when it has a distinctive
  word; one generic word («собор», «мечеть», «центр», «парк») never matches, several generic words only as a
  whole name («Большого театра» ~ «Большой театр», never «старого города» ~ anything partial);
- the best score wins, ties go to the poi with more Wikidata sitelinks (notability), then the lower rank.
"""
from __future__ import annotations

import json
import re
import unicodedata

MATCH_MIN = 0.7
SUBSET_SCORE = 0.82
SUBSET_EXTRA_PENALTY = 0.03

_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "",
        "э": "e", "ю": "iu", "я": "ia"}
# Russian case endings (longest first) for place names: nouns, adjectives, «-ия» words
_ENDINGS = ("ией", "иям", "иях", "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями", "ой", "ей", "ий", "ый",
            "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ом", "ем", "ах", "ях", "ам", "ям", "ым", "им", "ию",
            "ия", "ии", "ью", "ья", "ье", "ов", "ев", "а", "я", "е", "у", "ю", "и", "ы", "ь", "й", "о")
# words that name a kind of place, never a place on their own («рядом с собором» is no sight)
GENERIC_WORDS = {
    "собор", "собора", "соборы", "церковь", "церкви", "храм", "мечеть", "мечети", "синагога", "часовня", "капелла",
    "монастырь", "лавра", "музей", "музеи", "галерея", "парк", "сад", "сквер", "площадь", "улица", "проспект",
    "бульвар", "переулок", "набережная", "мост", "башня", "дворец", "замок", "крепость", "театр", "цирк",
    "стадион", "арена", "памятник", "монумент", "фонтан", "рынок", "базар", "вокзал", "аэропорт", "порт",
    "станция", "метро", "остановка", "отель", "гостиница", "хостел", "пляж", "море", "озеро", "река", "гора",
    "холм", "остров", "залив", "бухта", "район", "квартал", "центр", "город", "старый", "новый", "святой",
    "святого", "святая", "святых", "большой", "малый", "главный", "центральный", "городской", "государственный",
    "национальный", "исторический", "исторического", "знаменитый", "известный", "достопримечательность",
    "достопримечательности", "место", "места", "здание", "дом", "башни", "ворота", "аквапарк", "зоопарк",
    "church", "cathedral", "mosque", "museum", "park", "garden", "square", "street", "avenue", "palace", "castle",
    "tower", "bridge", "market", "station", "airport", "beach", "old", "new", "saint", "st", "great", "grand",
    "national", "state", "the", "of", "de", "la", "le", "del", "di", "camii", "cami", "sarayi", "meydani",
    "kulesi", "muzesi", "parki", "koprusu", "caddesi", "sokak", "mahallesi",
}
_FILLERS = {"самой", "самого", "самим", "самым", "самих", "этой", "этого", "этим", "тем", "той", "того", "нашей",
            "нашего", "знаменитой", "знаменитого", "известной", "известного"}
# a phrase after a cue ends at punctuation, a number or one of these words
_STOP_WORDS = {"и", "а", "но", "или", "на", "в", "во", "с", "со", "за", "до", "по", "к", "ко", "от", "для",
               "без", "из", "чтобы", "чтоб", "где", "если", "когда", "как", "пожалуйста", "плиз", "бы", "же",
               "ли", "можно", "хотим", "хочу", "хотелось", "было", "был", "была", "были", "есть", "там", "тоже",
               "ещё", "еще", "лучше", "пешком", "минут", "минуты", "км", "метров", "м", "не", "или", "то", "это",
               "чем", "что", "который", "которая", "которые", "отель", "отели", "отеля", "гостиница", "жить"}
_PHRASE_MAX_WORDS = 5


def _strip_marks(text):
    return str(text or "").replace("\u0301", "").replace("\u0300", "")


def norm(text):
    """Lower case, ё -> е, stress marks / punctuation / hyphens dropped (like opendata.norm_name, no prepositions)."""
    s = _strip_marks(text).replace("ё", "е").replace("Ё", "Е").casefold()
    s = re.sub(r"[\-‐–—_/'’`.]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def _latin(text):
    out = []
    for ch in text:
        out.append(_CYR.get(ch, ch))
    s = unicodedata.normalize("NFKD", "".join(out).replace("ı", "i"))
    return "".join(c for c in s if not unicodedata.combining(c))


def key(text):
    """Latin sound key of a normalised text: transliterated, letters only, ph→f, kh→h, w→v, q→k, x→ks, y/j→i,
    doubled letters collapsed («Айя-София» → aiasofia, «Ayasofya» → aiasofia)."""
    s = re.sub(r"[^a-z]", "", _latin(norm(text)))
    for a, b in (("ph", "f"), ("kh", "h"), ("ck", "k"), ("w", "v"), ("q", "k"), ("x", "ks"), ("y", "i"), ("j", "i")):
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def stems(word):
    """The word and its forms without one or two Russian endings; «-ец» / «-ок» also lose the fleeting vowel
    («дворец» ~ «дворца», «замок» ~ «замка»)."""
    out = {word}
    for w in list(out):
        if len(w) >= 5 and w.endswith("ец"):
            out.add(w[:-2] + "ц")
        if len(w) >= 5 and w.endswith("ок"):
            out.add(w[:-2] + "к")
    for _ in range(2):
        for w in list(out):
            for end in _ENDINGS:
                if w.endswith(end) and len(w) - len(end) >= 3:
                    out.add(w[: -len(end)])
    return out


_GENERIC_FORMS = None


def _generic_forms():
    global _GENERIC_FORMS
    if _GENERIC_FORMS is None:
        forms = set()
        for g in GENERIC_WORDS:
            forms.add(g)
            forms |= {s for s in stems(g) if len(s) >= 4}
        _GENERIC_FORMS = forms
    return _GENERIC_FORMS


def is_generic(word):
    return bool(({word} | {s for s in stems(word) if len(s) >= 4}) & _generic_forms())


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class _Word:
    __slots__ = ("text", "stems", "key")

    def __init__(self, text):
        self.text = text
        self.stems = stems(text)
        self.key = key(text)


def _wmatch(q, n, multi):
    """A query word ~ a name word (equal, a shared stem, the same Latin key, or the key plus an ending)."""
    if q.text == n.text:
        return True
    if len(q.text) < 3 or len(n.text) < 3:
        return False
    shared = q.stems & n.stems
    if shared:
        best = max(len(s) for s in shared)
        if best >= (3 if multi else 4) and best >= len(n.text) - 3 and best >= len(q.text) - 3:
            return True
    if len(q.key) >= 4 and q.key == n.key:
        return True
    c = _common_prefix(q.key, n.key)
    return c >= max(4, len(n.key) - 2) and len(q.key) - c <= 3 and len(n.key) - c <= 2


class _Name:
    __slots__ = ("text", "words", "key")

    def __init__(self, text):
        self.text = norm(text)
        self.words = [_Word(w) for w in self.text.split()]
        self.key = key(text)


def _prefixes(words, joined):
    out = {w.key[:3] for w in words if len(w.key) >= 3}
    if len(joined) >= 3:
        out.add(joined[:3])
    return out


def name_match(query, name):
    """0..1: how well a query (_Name) names a place name (_Name); 0 below MATCH_MIN."""
    if not query.words or not name.words:
        return 0.0
    if query.text == name.text:
        return 1.0
    if len(query.key) >= 5 and query.key == name.key:
        return 0.97
    multi = len(name.words) > 1
    if len(query.words) == len(name.words) and all(_wmatch(q, n, multi) for q, n in zip(query.words, name.words)):
        return 0.95
    c = _common_prefix(query.key, name.key)
    if len(name.key) >= 5 and c >= max(5, len(name.key) - 2) and len(query.key) - c <= 3 and len(name.key) - c <= 2:
        return 0.9
    if len(query.words) < len(name.words):
        distinctive = [q for q in query.words if len(q.text) >= 4 and not is_generic(q.text)]
        if distinctive:
            free = list(name.words)
            for q in query.words:
                hit = next((n for n in free if _wmatch(q, n, True)), None)
                if hit is None:
                    return 0.0
                free.remove(hit)
            return round(max(MATCH_MIN, SUBSET_SCORE - SUBSET_EXTRA_PENALTY * len(free)), 3)
    return 0.0


def _cyrillic(text):
    return bool(re.search(r"[а-яё]", str(text or ""), re.I))


def _names_of(row):
    names = row.get("names")
    if isinstance(names, str):
        try:
            names = json.loads(names)
        except ValueError:
            names = []
    out = [row.get("name"), row.get("name_ru"), row.get("name_en")] + list(names or [])
    return list(dict.fromkeys(n for n in out if n and str(n).strip()))


class PoiIndex:
    """Every name of one destination's pois, normalised once; `find` scores only the names that share a
    3-letter key prefix with the query (a few dozen of ~2 500)."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.names = []                    # (row index, _Name)
        self.by_prefix = {}
        for i, row in enumerate(self.rows):
            for raw in _names_of(row):
                nm = _Name(raw)
                if not nm.words:
                    continue
                j = len(self.names)
                self.names.append((i, nm))
                for p in _prefixes(nm.words, nm.key):
                    self.by_prefix.setdefault(p, []).append(j)
        self._memo = {}

    def find(self, text):
        """The poi a text names -> record {name, name_osm, kind, lat, lon, dist_center_m, wikidata, sitelinks,
        osm_id, destination_id, score, matched} or None."""
        q = norm(text)
        if q in self._memo:
            return self._memo[q]
        found = self._find(q, text)
        if len(self._memo) > 512:
            self._memo.clear()
        self._memo[q] = found
        return found

    def _find(self, q, text):
        query = _Name(q)
        if not query.words or len(query.key) < 3:
            return None
        # generic words only: never one word («собор»), several only as a whole name («Большого театра»)
        generic = all(is_generic(w.text) for w in query.words)
        if generic and len(query.words) < 2:
            return None
        cand = set()
        for p in _prefixes(query.words, query.key):
            cand.update(self.by_prefix.get(p, ()))
        best = None
        for j in cand:
            i, nm = self.names[j]
            s = name_match(query, nm)
            if s < (0.95 if generic else MATCH_MIN):
                continue
            row = self.rows[i]
            rank_key = (round(s, 2), int(row.get("sitelinks") or 0), -int(row.get("rank") or 10 ** 6))
            if best is None or rank_key > best[0]:
                best = (rank_key, i, nm, s)
        if best is None:
            return None
        _, i, nm, s = best
        row = self.rows[i]
        display = row.get("name") if _cyrillic(row.get("name")) else (
            row.get("name_ru") or (str(text).strip() if _cyrillic(text) else row.get("name")))
        return {"name": display, "name_osm": row.get("name"), "kind": row.get("kind"), "lat": float(row["lat"]),
                "lon": float(row["lon"]), "dist_center_m": int(row.get("dist_center_m") or 0),
                "wikidata": row.get("wikidata"), "sitelinks": row.get("sitelinks"), "osm_id": row.get("osm_id"),
                "destination_id": row.get("destination_id"), "score": round(s, 3), "matched": nm.text}


def _tokens(text):
    """Words and stops of a message: [(word | None)], None for punctuation / a number (a phrase boundary)."""
    out = []
    for m in re.finditer(r"[\w\u0301-]+|[^\w\s]", _strip_marks(text)):
        t = m.group(0)
        if re.fullmatch(r"[^\w]+", t) or any(ch.isdigit() for ch in t):
            out.append(None)
        else:
            out.append(t)
    return out


def near_phrases(text, cues):
    """The words after each cue («рядом с», «в районе», «у»…) up to punctuation, a number or a stop word,
    without leading fillers («самой») -> phrases in message order (as typed)."""
    toks = _tokens(text)
    low = [norm(t) if t else None for t in toks]
    cue_words = sorted({tuple(norm(c).split()) for c in cues or [] if norm(c)}, key=len, reverse=True)
    out = []
    i = 0
    while i < len(low):
        hit = None
        for cw in cue_words:
            if tuple(low[i:i + len(cw)]) == cw:
                hit = cw
                break
        if hit is None:
            i += 1
            continue
        j = i + len(hit)
        while j < len(low) and low[j] in _FILLERS:
            j += 1
        words = []
        while j < len(low) and low[j] is not None and low[j] not in _STOP_WORDS and len(words) < _PHRASE_MAX_WORDS:
            words.append(toks[j])
            j += 1
        if words:
            out.append(" ".join(words))
        i = max(j, i + 1)
    return out
