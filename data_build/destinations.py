"""Curated destination list (~150) and the `destinations` stage.

Curated by hand (source = 'curated'): ids, names, country, kind, tags, stations, the one-sentence
description and the popularity estimate (1..100, stored as 0..1 because the schema column is
0..1). Coordinates come from Nominatim (1 req/s, polite User-Agent, cached); if Nominatim gives
nothing for a query, the centre falls back to the nearest airport city coordinates from
OurAirports (source 'curated+ourairports'). Airports: see `nearest_airports` (scheduled airports
within 75 km, plus the nearest scheduled large airport within 150 km when none of them is large).
Politically contested territories are deliberately not included.
Tags use only agent.state.DESTINATION_TRAITS (checked by tests and at build time).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .common import (HttpxClient, RateLimiter, cache_path, cached_text, get_meta, haversine_m,
                     jdump, log, open_db, OPENDATA_DB)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
AIRPORT_RADIUS_KM = 150
AIRPORT_NEAR_KM = 75
MAX_AIRPORTS = 4
# Curated corrections to OurAirports' scheduled_service flag (it lags reality for these):
# Gorno-Altaysk (RGK) has had scheduled flights since 2011.
SERVED_OVERRIDES = frozenset({"RGK"})


@dataclass(frozen=True)
class Dest:
    id: str
    name_ru: str
    name_en: str
    country: str
    country_code: str
    kind: str                      # city | resort | nature | village
    tags: tuple[str, ...]
    popularity: int                # curated estimate 1..100
    description_ru: str
    query: str = ""                # Nominatim free-text query (defaults to name_en)
    stations: tuple[str, ...] = ()
    half_km: float = 4.0           # half side of the Overpass bounding box
    coastal: bool | None = None    # default: 'sea' in tags

    @property
    def is_coastal(self) -> bool:
        return ("sea" in self.tags) if self.coastal is None else self.coastal

    @property
    def q(self) -> str:
        return self.query or self.name_en


def _d(id, name_ru, name_en, country, cc, kind, tags, pop, desc, query="", stations=(), half_km=4.0, coastal=None):
    return Dest(id, name_ru, name_en, country, cc, kind, tuple(tags.split()), pop, desc, query,
                tuple(stations), half_km, coastal)


TR, EG, AE, TH, VN = "Турция", "Египет", "ОАЭ", "Таиланд", "Вьетнам"
RU = "Россия"

DESTINATIONS: list[Dest] = [
    # ---------------------------------------------------------------- Turkey
    _d("antalya", "Анталья", "Antalya", TR, "TR", "resort", "sea beach warm city resort history", 95,
       "Крупный курортный город на Средиземном море со старым городом Калеичи и пляжами Лара и Коньяалты."),
    _d("alanya", "Аланья", "Alanya", TR, "TR", "resort", "sea beach warm resort history", 85,
       "Курорт на востоке Анталийского побережья с крепостью на скале и пляжем Клеопатры.", "Alanya, Antalya"),
    _d("kemer", "Кемер", "Kemer", TR, "TR", "resort", "sea beach mountains warm resort", 80,
       "Курорт у подножия Таврских гор с галечными пляжами.", "Kemer, Antalya"),
    _d("side", "Сиде", "Side", TR, "TR", "resort", "sea beach warm resort history", 78,
       "Курорт с античными руинами, в том числе храмом Аполлона, и песчаными пляжами.", "Side, Manavgat"),
    _d("belek", "Белек", "Belek", TR, "TR", "resort", "sea beach warm resort", 75,
       "Курортная зона с крупными отелями, песчаными пляжами и полями для гольфа.", "Belek, Serik", half_km=5.0),
    _d("bodrum", "Бодрум", "Bodrum", TR, "TR", "resort", "sea beach warm resort nightlife history", 60,
       "Курорт на Эгейском побережье с крепостью Святого Петра и яхтенной мариной.", "Bodrum, Muğla"),
    _d("marmaris", "Мармарис", "Marmaris", TR, "TR", "resort", "sea beach warm resort nightlife", 55,
       "Курорт в бухте на стыке Эгейского и Средиземного морей."),
    _d("fethiye", "Фетхие", "Fethiye", TR, "TR", "resort", "sea beach warm resort nature", 55,
       "Город на Ликийском побережье рядом с лагуной Олюдениз.", "Fethiye, Muğla"),
    _d("istanbul", "Стамбул", "Istanbul", TR, "TR", "city", "city sea history culture food shopping architecture", 92,
       "Крупнейший город Турции на берегах Босфора с собором Святой Софии и Гранд-базаром.",
       "Sultanahmet, Istanbul"),
    _d("cappadocia", "Каппадокия (Гёреме)", "Cappadocia (Goreme)", TR, "TR", "village", "nature history village", 60,
       "Посёлок в центральной Турции среди скальных образований, известный полётами на воздушных шарах.",
       "Göreme, Nevşehir"),
    # ---------------------------------------------------------------- Egypt
    _d("hurghada", "Хургада", "Hurghada", EG, "EG", "resort", "sea beach hot resort", 90,
       "Курорт на Красном море, известный коралловыми рифами и дайвингом.", half_km=5.0),
    _d("sharm-el-sheikh", "Шарм-эш-Шейх", "Sharm El Sheikh", EG, "EG", "resort", "sea beach hot resort nightlife", 85,
       "Курорт на юге Синайского полуострова рядом с национальным парком Рас-Мохаммед.", "Naama Bay", half_km=5.0),
    _d("marsa-alam", "Марса-Алам", "Marsa Alam", EG, "EG", "resort", "sea beach hot resort nature", 45,
       "Курорт на юге египетского побережья Красного моря с коралловыми рифами."),
    _d("cairo", "Каир", "Cairo", EG, "EG", "city", "city history culture desert", 50,
       "Столица Египта; на окраине Большого Каира расположены пирамиды Гизы."),
    # ---------------------------------------------------------------- UAE
    _d("dubai", "Дубай", "Dubai", AE, "AE", "city", "city sea beach hot shopping nightlife architecture", 93,
       "Крупный город на побережье Персидского залива с небоскрёбом Бурдж-Халифа.", "Dubai Marina", half_km=5.0),
    _d("abu-dhabi", "Абу-Даби", "Abu Dhabi", AE, "AE", "city", "city sea beach hot culture architecture", 65,
       "Столица ОАЭ с мечетью шейха Зайда и музеем Лувр Абу-Даби."),
    _d("ras-al-khaimah", "Рас-эль-Хайма", "Ras Al Khaimah", AE, "AE", "resort", "sea beach hot resort mountains", 55,
       "Северный эмират ОАЭ с пляжами на Персидском заливе и горами Хаджар.", "Ras Al Khaimah city"),
    # ---------------------------------------------------------------- Thailand
    _d("phuket", "Пхукет", "Phuket", TH, "TH", "resort", "sea beach tropical island resort nightlife", 88,
       "Крупнейший остров Таиланда в Андаманском море; центр курортной жизни — пляж Патонг.", "Patong, Kathu, Phuket"),
    _d("pattaya", "Паттайя", "Pattaya", TH, "TH", "resort", "sea beach tropical resort nightlife", 75,
       "Курортный город на берегу Сиамского залива."),
    _d("bangkok", "Бангкок", "Bangkok", TH, "TH", "city", "city hot food culture shopping nightlife", 70,
       "Столица Таиланда с храмами Ват Пхо и Ват Арун.", "Phra Nakhon, Bangkok"),
    _d("koh-samui", "Самуи", "Koh Samui", TH, "TH", "resort", "sea beach tropical island resort", 60,
       "Остров в Сиамском заливе; самый оживлённый пляж — Чавенг.", "Chaweng"),
    _d("krabi", "Краби (Ао Нанг)", "Krabi (Ao Nang)", TH, "TH", "resort", "sea beach tropical resort nature", 50,
       "Курорт на Андаманском побережье среди известняковых скал.", "Ao Nang Beach"),
    _d("chiang-mai", "Чиангмай", "Chiang Mai", TH, "TH", "city", "city culture history mountains food", 30,
       "Город на севере Таиланда со старым городом и сотнями буддийских храмов."),
    # ---------------------------------------------------------------- Vietnam
    _d("nha-trang", "Нячанг", "Nha Trang", VN, "VN", "resort", "sea beach tropical resort city", 80,
       "Курортный город на берегу Южно-Китайского моря."),
    _d("phu-quoc", "Фукуок", "Phu Quoc", VN, "VN", "resort", "sea beach tropical island resort", 70,
       "Крупнейший остров Вьетнама в Сиамском заливе; главный город — Зыонгдонг.", "Phú Quốc"),
    _d("da-nang", "Дананг", "Da Nang", VN, "VN", "city", "sea beach tropical city", 50,
       "Город в центральном Вьетнаме с пляжем Микхе."),
    _d("hanoi", "Ханой", "Hanoi", VN, "VN", "city", "city culture food history", 40,
       "Столица Вьетнама со Старым кварталом и озером Хоанкьем.", "Hoan Kiem, Hanoi"),
    _d("ho-chi-minh", "Хошимин", "Ho Chi Minh City", VN, "VN", "city", "city food shopping hot", 35,
       "Крупнейший город Вьетнама на юге страны.", "District 1, Ho Chi Minh City"),
    # ---------------------------------------------------------------- Sri Lanka, Maldives, Indonesia
    _d("hikkaduwa", "Хиккадува", "Hikkaduwa", "Шри-Ланка", "LK", "resort", "sea beach tropical resort", 50,
       "Курортный город на юго-западном побережье Шри-Ланки с коралловым рифом."),
    _d("bentota", "Бентота", "Bentota", "Шри-Ланка", "LK", "resort", "sea beach tropical resort river", 45,
       "Курорт на юго-западном побережье Шри-Ланки в устье реки Бентота."),
    _d("maldives-male", "Мальдивы (Мале)", "Maldives (Male)", "Мальдивы", "MV", "resort",
       "sea beach tropical island resort", 70,
       "Столица Мальдивской Республики; отсюда добираются до курортных островов.", "Malé", half_km=3.0),
    _d("bali", "Бали", "Bali", "Индонезия", "ID", "resort", "sea beach tropical island resort culture", 85,
       "Остров в Индонезии с индуистскими храмами, рисовыми террасами и пляжами; курортная зона — Кута и Семиньяк.",
       "Kuta, Badung"),
    # ---------------------------------------------------------------- China
    _d("sanya", "Санья (Хайнань)", "Sanya (Hainan)", "Китай", "CN", "resort", "sea beach tropical island resort", 65,
       "Курортный город на юге острова Хайнань.", "Sanya, Hainan"),
    _d("beijing", "Пекин", "Beijing", "Китай", "CN", "city", "city history culture architecture", 50,
       "Столица Китая с Запретным городом и площадью Тяньаньмэнь.", "Dongcheng, Beijing"),
    _d("shanghai", "Шанхай", "Shanghai", "Китай", "CN", "city", "city architecture shopping food", 45,
       "Крупнейший город Китая с набережной Вайтань.", "Huangpu, Shanghai"),
    # ---------------------------------------------------------------- Cyprus, Greece
    _d("limassol", "Лимасол", "Limassol", "Кипр", "CY", "city", "sea beach warm city resort", 60,
       "Второй по величине город Кипра на южном побережье острова."),
    _d("larnaca", "Ларнака", "Larnaca", "Кипр", "CY", "city", "sea beach warm city", 50,
       "Город на юго-восточном побережье Кипра с набережной Финикудес."),
    _d("ayia-napa", "Айя-Напа", "Ayia Napa", "Кипр", "CY", "resort", "sea beach warm resort nightlife", 55,
       "Курорт на юго-востоке Кипра с песчаными пляжами."),
    _d("paphos", "Пафос", "Paphos", "Кипр", "CY", "city", "sea warm history city", 45,
       "Город на западе Кипра с археологическим парком, включённым в список ЮНЕСКО."),
    _d("heraklion", "Крит (Ираклион)", "Crete (Heraklion)", "Греция", "GR", "city", "sea beach island warm history", 60,
       "Крупнейший город Крита; рядом находится дворец Кносс.", "Heraklion"),
    _d("rhodes", "Родос", "Rhodes", "Греция", "GR", "city", "sea beach island warm history", 50,
       "Главный город острова Родос со средневековым Старым городом.", "Rhodes town"),
    _d("athens", "Афины", "Athens", "Греция", "GR", "city", "city history culture architecture food", 50,
       "Столица Греции с Акрополем."),
    _d("santorini", "Санторини (Фира)", "Santorini (Fira)", "Греция", "GR", "village", "sea island beach architecture", 45,
       "Вулканический остров в Эгейском море; Фира — его главный город на краю кальдеры.", "Fira", half_km=3.0),
    # ---------------------------------------------------------------- Spain, Italy, Portugal, France
    _d("barcelona", "Барселона", "Barcelona", "Испания", "ES", "city",
       "city sea beach architecture culture food nightlife", 70,
       "Столица Каталонии с постройками Гауди и городскими пляжами."),
    _d("madrid", "Мадрид", "Madrid", "Испания", "ES", "city", "city culture food history", 45,
       "Столица Испании с музеем Прадо."),
    _d("tenerife", "Тенерифе", "Tenerife", "Испания", "ES", "resort", "sea beach island warm nature mountains", 45,
       "Крупнейший из Канарских островов с вулканом Тейде; курортная зона — Лос-Кристианос.", "Los Cristianos"),
    _d("mallorca", "Майорка (Пальма)", "Mallorca (Palma)", "Испания", "ES", "city", "sea beach island warm city", 40,
       "Пальма — главный город острова Майорка с готическим собором.", "Palma"),
    _d("lloret-de-mar", "Льорет-де-Мар", "Lloret de Mar", "Испания", "ES", "resort", "sea beach resort nightlife", 40,
       "Курорт на побережье Коста-Брава в Каталонии."),
    _d("rome", "Рим", "Rome", "Италия", "IT", "city", "city history culture architecture food", 72,
       "Столица Италии с Колизеем, Пантеоном и Ватиканом.", "Rome, Lazio"),
    _d("milan", "Милан", "Milan", "Италия", "IT", "city", "city shopping architecture food", 45,
       "Город на севере Италии с Миланским собором и театром Ла Скала."),
    _d("venice", "Венеция", "Venice", "Италия", "IT", "city", "city history architecture culture", 55,
       "Город на островах лагуны с каналами вместо улиц.", "Venice, Veneto", half_km=3.0),
    _d("florence", "Флоренция", "Florence", "Италия", "IT", "city", "city history culture architecture food", 50,
       "Столица Тосканы с собором Санта-Мария-дель-Фьоре и галереей Уффици."),
    _d("rimini", "Римини", "Rimini", "Италия", "IT", "resort", "sea beach resort nightlife", 40,
       "Курорт на Адриатическом побережье Италии."),
    _d("lisbon", "Лиссабон", "Lisbon", "Португалия", "PT", "city", "city sea history food architecture", 40,
       "Столица Португалии на берегу устья реки Тежу."),
    _d("paris", "Париж", "Paris", "Франция", "FR", "city", "city culture architecture food shopping history", 65,
       "Столица Франции с Эйфелевой башней и Лувром."),
    _d("nice", "Ницца", "Nice", "Франция", "FR", "city", "sea beach city warm", 40,
       "Город на Лазурном берегу с Английской набережной."),
    # ---------------------------------------------------------------- North Africa, islands, Americas
    _d("sousse", "Сусс", "Sousse", "Тунис", "TN", "resort", "sea beach warm resort history", 50,
       "Курортный город на восточном побережье Туниса с мединой, включённой в список ЮНЕСКО."),
    _d("hammamet", "Хаммамет", "Hammamet", "Тунис", "TN", "resort", "sea beach warm resort", 45,
       "Курорт на берегу залива Хаммамет на северо-востоке Туниса."),
    _d("djerba", "Джерба", "Djerba", "Тунис", "TN", "resort", "sea beach island warm resort", 40,
       "Остров у юго-восточного побережья Туниса.", "Midoun"),
    _d("goa", "Гоа", "Goa", "Индия", "IN", "resort", "sea beach tropical resort nightlife", 65,
       "Штат Индии на побережье Аравийского моря; популярная курортная зона — Калангут на севере.", "Calangute"),
    _d("mauritius", "Маврикий", "Mauritius", "Маврикий", "MU", "resort", "sea beach tropical island resort", 35,
       "Островное государство в Индийском океане; Гран-Бе — курортный посёлок на севере острова.", "Grand Baie"),
    _d("seychelles", "Сейшелы (Маэ)", "Seychelles (Mahe)", "Сейшелы", "SC", "resort", "sea beach tropical island resort nature", 35,
       "Архипелаг в Индийском океане; пляж Бо-Валлон — на главном острове Маэ.", "Beau Vallon"),
    _d("zanzibar", "Занзибар", "Zanzibar", "Танзания", "TZ", "resort", "sea beach tropical island history", 45,
       "Остров у побережья Танзании со Стоун-Тауном, внесённым в список ЮНЕСКО.", "Zanzibar City"),
    _d("varadero", "Варадеро", "Varadero", "Куба", "CU", "resort", "sea beach tropical resort", 45,
       "Курорт на полуострове Икакос на северном побережье Кубы.", half_km=5.0),
    _d("havana", "Гавана", "Havana", "Куба", "CU", "city", "city history architecture culture sea", 40,
       "Столица Кубы со Старой Гаваной, включённой в список ЮНЕСКО.", "La Habana Vieja"),
    _d("punta-cana", "Пунта-Кана", "Punta Cana", "Доминикана", "DO", "resort", "sea beach tropical resort", 45,
       "Курортная зона на восточной оконечности Доминиканской Республики.", "Bávaro", half_km=5.0),
    _d("cancun", "Канкун", "Cancun", "Мексика", "MX", "resort", "sea beach tropical resort nightlife", 45,
       "Курорт на полуострове Юкатан на берегу Карибского моря.", "Zona Hotelera, Cancún", half_km=5.0),
    # ---------------------------------------------------------------- Balkans, Central Europe
    _d("budva", "Будва", "Budva", "Черногория", "ME", "resort", "sea beach warm resort history nightlife", 50,
       "Курорт на Адриатическом побережье Черногории со Старым городом."),
    _d("kotor", "Котор", "Kotor", "Черногория", "ME", "city", "sea history mountains architecture", 35,
       "Средневековый город в Которском заливе, внесённый в список ЮНЕСКО.", half_km=3.0),
    _d("belgrade", "Белград", "Belgrade", "Сербия", "RS", "city", "city river food nightlife history", 45,
       "Столица Сербии у слияния Савы и Дуная."),
    _d("dubrovnik", "Дубровник", "Dubrovnik", "Хорватия", "HR", "city", "sea history architecture", 30,
       "Город на Адриатике с крепостными стенами вокруг Старого города."),
    _d("prague", "Прага", "Prague", "Чехия", "CZ", "city", "city history architecture culture food", 55,
       "Столица Чехии с Пражским градом и Карловым мостом."),
    _d("karlovy-vary", "Карловы Вары", "Karlovy Vary", "Чехия", "CZ", "city", "spa history forest", 30,
       "Курортный город на западе Чехии с минеральными источниками."),
    _d("budapest", "Будапешт", "Budapest", "Венгрия", "HU", "city", "city spa history architecture nightlife river", 50,
       "Столица Венгрии на Дунае с термальными купальнями."),
    _d("vienna", "Вена", "Vienna", "Австрия", "AT", "city", "city culture architecture history", 45,
       "Столица Австрии с дворцами Хофбург и Шёнбрунн."),
    _d("berlin", "Берлин", "Berlin", "Германия", "DE", "city", "city culture history nightlife", 35,
       "Столица Германии с Бранденбургскими воротами и Музейным островом."),
    _d("amsterdam", "Амстердам", "Amsterdam", "Нидерланды", "NL", "city", "city culture architecture nightlife", 35,
       "Столица Нидерландов с кольцом каналов XVII века."),
    _d("london", "Лондон", "London", "Великобритания", "GB", "city", "city culture history shopping", 35,
       "Столица Великобритании с Тауэром и Британским музеем.", "City of Westminster"),
    # ---------------------------------------------------------------- Caucasus, Central Asia, Belarus
    _d("tbilisi", "Тбилиси", "Tbilisi", "Грузия", "GE", "city", "city food culture history architecture", 80,
       "Столица Грузии со Старым городом и серными банями."),
    _d("batumi", "Батуми", "Batumi", "Грузия", "GE", "city", "sea beach city resort nightlife", 70,
       "Город на Черноморском побережье Грузии с длинным приморским бульваром."),
    _d("kutaisi", "Кутаиси", "Kutaisi", "Грузия", "GE", "city", "city history nature", 30,
       "Город в Имеретии рядом с монастырём Гелати и пещерой Прометея."),
    _d("gudauri", "Гудаури", "Gudauri", "Грузия", "GE", "resort", "mountains ski snow resort", 40,
       "Горнолыжный курорт на Военно-Грузинской дороге.", "Gudauri", half_km=3.0),
    _d("stepantsminda", "Казбеги (Степанцминда)", "Stepantsminda", "Грузия", "GE", "village", "mountains nature village", 35,
       "Посёлок у подножия Казбека с храмом Гергети.", half_km=3.0),
    _d("yerevan", "Ереван", "Yerevan", "Армения", "AM", "city", "city food culture history", 70,
       "Столица Армении с видом на гору Арарат."),
    _d("dilijan", "Дилижан", "Dilijan", "Армения", "AM", "village", "mountains forest nature", 25,
       "Курортный город в лесистых горах на севере Армении.", half_km=3.0),
    _d("sevan", "Севан", "Sevan", "Армения", "AM", "nature", "lake mountains nature beach", 25,
       "Город на берегу высокогорного озера Севан.", "Sevan", half_km=3.0, coastal=False),
    _d("baku", "Баку", "Baku", "Азербайджан", "AZ", "city", "city sea architecture food history", 60,
       "Столица Азербайджана на Каспийском море с Ичери-шехер — старым городом в крепостных стенах."),
    _d("gabala", "Габала", "Gabala", "Азербайджан", "AZ", "nature", "mountains nature forest", 25,
       "Город у подножия Большого Кавказа на севере Азербайджана.", "Qabala", half_km=3.0),
    _d("samarkand", "Самарканд", "Samarkand", "Узбекистан", "UZ", "city", "city history architecture culture", 55,
       "Древний город на Великом шёлковом пути с площадью Регистан."),
    _d("tashkent", "Ташкент", "Tashkent", "Узбекистан", "UZ", "city", "city food culture", 45,
       "Столица Узбекистана."),
    _d("bukhara", "Бухара", "Bukhara", "Узбекистан", "UZ", "city", "city history architecture", 45,
       "Город с историческим центром, внесённым в список ЮНЕСКО, и минаретом Калян.", half_km=3.0),
    _d("khiva", "Хива", "Khiva", "Узбекистан", "UZ", "city", "history architecture desert", 25,
       "Город с внутренней крепостью Ичан-Кала, внесённой в список ЮНЕСКО.", half_km=3.0),
    _d("almaty", "Алматы", "Almaty", "Казахстан", "KZ", "city", "city mountains food", 55,
       "Крупнейший город Казахстана у подножия Заилийского Алатау."),
    _d("astana", "Астана", "Astana", "Казахстан", "KZ", "city", "city architecture", 30,
       "Столица Казахстана с современной архитектурой."),
    _d("issyk-kul", "Иссык-Куль (Чолпон-Ата)", "Issyk-Kul (Cholpon-Ata)", "Киргизия", "KG", "resort",
       "lake mountains beach nature resort", 35,
       "Курортный город на северном берегу горного озера Иссык-Куль.", "Cholpon-Ata", half_km=3.0, coastal=False),
    _d("minsk", "Минск", "Minsk", "Беларусь", "BY", "city", "city culture food", 55,
       "Столица Беларуси."),
    # ---------------------------------------------------------------- Asia, Middle East
    _d("tokyo", "Токио", "Tokyo", "Япония", "JP", "city", "city food culture shopping architecture", 50,
       "Столица Японии.", "Chiyoda, Tokyo"),
    _d("kyoto", "Киото", "Kyoto", "Япония", "JP", "city", "city history culture architecture", 35,
       "Бывшая столица Японии с сотнями буддийских храмов и синтоистских святилищ."),
    _d("seoul", "Сеул", "Seoul", "Южная Корея", "KR", "city", "city food shopping culture", 45,
       "Столица Южной Кореи.", "Jung-gu, Seoul"),
    _d("kuala-lumpur", "Куала-Лумпур", "Kuala Lumpur", "Малайзия", "MY", "city", "city shopping food architecture hot", 40,
       "Столица Малайзии с башнями Петронас."),
    _d("langkawi", "Лангкави", "Langkawi", "Малайзия", "MY", "resort", "sea beach tropical island resort", 30,
       "Остров в Андаманском море; основной курортный пляж — Пантай-Ценанг.", "Pantai Cenang"),
    _d("singapore", "Сингапур", "Singapore", "Сингапур", "SG", "city", "city food shopping architecture tropical", 40,
       "Город-государство на юге Малаккского полуострова.", "Downtown Core, Singapore"),
    _d("doha", "Доха", "Doha", "Катар", "QA", "city", "city sea hot architecture shopping", 35,
       "Столица Катара на берегу Персидского залива."),
    _d("muscat", "Маскат", "Muscat", "Оман", "OM", "city", "city sea beach mountains hot history", 30,
       "Столица Омана между Оманским заливом и горами Хаджар."),
    _d("salalah", "Салала", "Salalah", "Оман", "OM", "resort", "sea beach warm nature", 30,
       "Город на юге Омана, где летом идут муссонные дожди (сезон хариф)."),
    _d("aqaba", "Акаба", "Aqaba", "Иордания", "JO", "resort", "sea beach hot desert resort", 30,
       "Единственный морской порт Иордании на Красном море."),
    _d("marrakech", "Марракеш", "Marrakesh", "Марокко", "MA", "city", "city history culture desert", 30,
       "Город в Марокко с мединой и площадью Джемаа-эль-Фна."),
    _d("new-york", "Нью-Йорк", "New York", "США", "US", "city", "city culture shopping architecture food", 30,
       "Крупнейший город США.", "Midtown Manhattan"),
    # ---------------------------------------------------------------- Russia: sea
    _d("sochi", "Сочи", "Sochi", RU, "RU", "resort", "sea beach mountains warm resort city", 92,
       "Город-курорт на Черноморском побережье Краснодарского края.", "Сочи, Краснодарский край",
       ("Сочи",)),
    _d("adler", "Адлер (Сириус)", "Adler (Sirius)", RU, "RU", "resort", "sea beach warm resort", 70,
       "Курортный район Сочи рядом с аэропортом и федеральной территорией Сириус с Олимпийским парком.",
       "Адлер, Сочи", ("Адлер", "Имеретинский курорт")),
    _d("krasnaya-polyana", "Красная Поляна", "Krasnaya Polyana", RU, "RU", "resort", "mountains ski snow resort nature", 80,
       "Горный курорт в Сочи с комплексами Роза Хутор, Газпром и Красная Поляна.", "Эсто-Садок",
       ("Роза Хутор",), 5.0),
    _d("anapa", "Анапа", "Anapa", RU, "RU", "resort", "sea beach warm resort", 75,
       "Курорт на Черноморском побережье с песчаными пляжами, популярный у семей с детьми.", "Анапа",
       ("Анапа",)),
    _d("gelendzhik", "Геленджик", "Gelendzhik", RU, "RU", "resort", "sea beach warm resort mountains", 70,
       "Курортный город на берегу Геленджикской бухты Чёрного моря.", "Геленджик", ("Новороссийск",)),
    _d("lazarevskoe", "Лазаревское", "Lazarevskoye", RU, "RU", "resort", "sea beach warm resort", 35,
       "Курортный посёлок в Лазаревском районе Сочи.", "Лазаревское, Сочи", ("Лазаревская",), 3.0),
    _d("tuapse", "Туапсе", "Tuapse", RU, "RU", "resort", "sea beach warm", 25,
       "Портовый город на Черноморском побережье Краснодарского края.", "Туапсе", ("Туапсе",), 3.0),
    _d("kaliningrad", "Калининград", "Kaliningrad", RU, "RU", "city", "city history architecture", 70,
       "Областной центр на Балтике с островом Канта и Кафедральным собором.", "Калининград",
       ("Калининград-Южный",), coastal=False),
    _d("zelenogradsk", "Зеленоградск", "Zelenogradsk", RU, "RU", "resort", "sea beach resort", 40,
       "Курортный город на Балтийском море у начала Куршской косы.", "Зеленоградск", ("Зеленоградск-Новый",), 3.0),
    _d("svetlogorsk", "Светлогорск", "Svetlogorsk", RU, "RU", "resort", "sea beach resort forest", 45,
       "Курортный город на берегу Балтийского моря с довоенной застройкой.", "Светлогорск", ("Светлогорск-2",), 3.0),
    _d("vladivostok", "Владивосток", "Vladivostok", RU, "RU", "city", "city sea food", 45,
       "Город на берегу Японского моря с Русским и Золотым мостами.", "Владивосток", ("Владивосток",)),
    _d("derbent", "Дербент", "Derbent", RU, "RU", "city", "city history sea", 40,
       "Один из древнейших городов России на Каспийском море с крепостью Нарын-Кала.", "Дербент",
       ("Дербент",), 3.0),
    _d("makhachkala", "Махачкала", "Makhachkala", RU, "RU", "city", "city sea mountains food", 35,
       "Столица Дагестана на берегу Каспийского моря.", "Махачкала", ("Махачкала",)),
    # ---------------------------------------------------------------- Russia: cities
    _d("moscow", "Москва", "Moscow", RU, "RU", "city",
       "city culture history architecture food shopping nightlife", 90,
       "Столица России с Кремлём и Красной площадью.", "Красная площадь, Москва",
       ("Ленинградский вокзал", "Казанский вокзал", "Курский вокзал", "Ярославский вокзал", "Киевский вокзал",
        "Павелецкий вокзал", "Белорусский вокзал"), coastal=False),
    _d("saint-petersburg", "Санкт-Петербург", "Saint Petersburg", RU, "RU", "city",
       "city culture history architecture food river nightlife", 92,
       "Город на Неве с Эрмитажем, разводными мостами и белыми ночами.", "Дворцовая площадь, Санкт-Петербург",
       ("Московский вокзал", "Ладожский вокзал", "Витебский вокзал", "Финляндский вокзал"), coastal=False),
    _d("kazan", "Казань", "Kazan", RU, "RU", "city", "city history culture food river architecture", 85,
       "Столица Татарстана на Волге с Казанским кремлём.", "Казань", ("Казань-Пассажирская",)),
    _d("nizhny-novgorod", "Нижний Новгород", "Nizhny Novgorod", RU, "RU", "city", "city river history architecture food", 55,
       "Город у слияния Оки и Волги с кремлём и Чкаловской лестницей.", "Нижний Новгород",
       ("Нижний Новгород (Московский вокзал)",)),
    _d("yaroslavl", "Ярославль", "Yaroslavl", RU, "RU", "city", "city history river architecture", 45,
       "Город на Волге, исторический центр которого внесён в список ЮНЕСКО.", "Ярославль",
       ("Ярославль-Главный",)),
    _d("suzdal", "Суздаль", "Suzdal", RU, "RU", "village", "history architecture village", 45,
       "Небольшой город Золотого кольца с белокаменными храмами и монастырями.", "Суздаль", (), 3.0),
    _d("vladimir", "Владимир", "Vladimir", RU, "RU", "city", "city history architecture", 30,
       "Город Золотого кольца с Успенским собором и Золотыми воротами.", "Владимир", ("Владимир",)),
    _d("kostroma", "Кострома", "Kostroma", RU, "RU", "city", "city history river", 25,
       "Город Золотого кольца на Волге с Ипатьевским монастырём.", "Кострома", ("Кострома-Новая",)),
    _d("plyos", "Плёс", "Plyos", RU, "RU", "village", "village river history nature", 30,
       "Небольшой город на Волге, связанный с художником Исааком Левитаном.", "Плёс, Ивановская область", (), 3.0),
    _d("veliky-novgorod", "Великий Новгород", "Veliky Novgorod", RU, "RU", "city", "city history architecture river", 30,
       "Один из древнейших городов России с Новгородским детинцем.", "Великий Новгород",
       ("Великий Новгород",)),
    _d("pskov", "Псков", "Pskov", RU, "RU", "city", "city history architecture river", 30,
       "Древний город с Псковским кремлём у слияния рек Великой и Псковы.", "Псков", ("Псков",)),
    _d("kolomna", "Коломна", "Kolomna", RU, "RU", "city", "city history", 30,
       "Город в Московской области с кремлём XVI века.", "Коломна", ("Коломна",)),
    _d("tula", "Тула", "Tula", RU, "RU", "city", "city history", 25,
       "Областной центр с кремлём; рядом музей-усадьба Ясная Поляна.", "Тула", ("Тула (Московский вокзал)",)),
    _d("yekaterinburg", "Екатеринбург", "Yekaterinburg", RU, "RU", "city", "city culture", 50,
       "Крупный город на Урале.", "Екатеринбург", ("Екатеринбург-Пассажирский",)),
    _d("novosibirsk", "Новосибирск", "Novosibirsk", RU, "RU", "city", "city river culture", 30,
       "Крупнейший город Сибири на реке Обь.", "Новосибирск", ("Новосибирск-Главный",)),
    _d("samara", "Самара", "Samara", RU, "RU", "city", "city river beach", 30,
       "Город на Волге с одной из самых длинных набережных в России.", "Самара", ("Самара",)),
    _d("volgograd", "Волгоград", "Volgograd", RU, "RU", "city", "city history river", 25,
       "Город на Волге с мемориальным комплексом на Мамаевом кургане.", "Волгоград", ("Волгоград-1",)),
    _d("rostov-on-don", "Ростов-на-Дону", "Rostov-on-Don", RU, "RU", "city", "city river food", 30,
       "Крупный город на юге России на реке Дон.", "Ростов-на-Дону", ("Ростов-Главный",)),
    _d("krasnodar", "Краснодар", "Krasnodar", RU, "RU", "city", "city", 30,
       "Столица Краснодарского края с парком «Краснодар» (парк Галицкого).", "Краснодар", ("Краснодар-1",)),
    _d("irkutsk", "Иркутск", "Irkutsk", RU, "RU", "city", "city history river", 40,
       "Город на Ангаре в 70 км от Байкала.", "Иркутск", ("Иркутск-Пассажирский",)),
    _d("petrozavodsk", "Карелия (Петрозаводск)", "Karelia (Petrozavodsk)", RU, "RU", "city", "lake forest nature city", 55,
       "Столица Карелии на берегу Онежского озера.", "Петрозаводск", ("Петрозаводск",)),
    _d("murmansk", "Мурманск", "Murmansk", RU, "RU", "city", "city cold snow sea nature", 40,
       "Крупнейший город за Полярным кругом; зимой отсюда ездят смотреть северное сияние.", "Мурманск",
       ("Мурманск",)),
    # ---------------------------------------------------------------- Russia: nature, mountains, spa
    _d("altai", "Алтай (Чемал)", "Altai (Chemal)", RU, "RU", "nature", "mountains river nature forest", 60,
       "Село на реке Катунь — один из центров туризма в Республике Алтай.", "Чемал", (), 4.0),
    _d("belokurikha", "Белокуриха", "Belokurikha", RU, "RU", "resort", "spa mountains resort forest", 30,
       "Бальнеологический курорт в предгорьях Алтая.", "Белокуриха", (), 3.0),
    _d("listvyanka", "Байкал (Листвянка)", "Baikal (Listvyanka)", RU, "RU", "village", "lake nature mountains forest", 55,
       "Посёлок на берегу Байкала у истока Ангары.", "Листвянка", (), 3.0, coastal=False),
    _d("olkhon", "Байкал (Ольхон, Хужир)", "Baikal (Olkhon, Khuzhir)", RU, "RU", "village", "lake island nature", 35,
       "Посёлок на крупнейшем острове Байкала рядом со скалой Шаманка.", "Хужир", (), 3.0, coastal=False),
    _d("kamchatka", "Камчатка (Петропавловск-Камчатский)", "Kamchatka (Petropavlovsk-Kamchatsky)", RU, "RU", "nature",
       "nature mountains sea cold", 40,
       "Город на берегу Авачинской бухты с видом на вулканы.", "Петропавловск-Камчатский"),
    _d("teriberka", "Териберка", "Teriberka", RU, "RU", "village", "village nature sea cold snow", 35,
       "Село на берегу Баренцева моря, куда ездят за северным сиянием и видами Арктики.", "Териберка", (), 3.0),
    _d("kirovsk", "Хибины (Кировск)", "Khibiny (Kirovsk)", RU, "RU", "resort", "mountains ski snow cold nature", 35,
       "Город у горнолыжных склонов Хибин за Полярным кругом.", "Кировск, Мурманская область", ("Апатиты",), 3.0),
    _d("sortavala", "Сортавала", "Sortavala", RU, "RU", "nature", "lake forest nature history", 40,
       "Город на берегу Ладожского озера рядом с горным парком «Рускеала».", "Сортавала", ("Сортавала",), 3.0,
       coastal=False),
    _d("kislovodsk", "Кисловодск", "Kislovodsk", RU, "RU", "resort", "spa mountains resort nature", 50,
       "Курорт Кавказских Минеральных Вод с нарзанными источниками и большим курортным парком.", "Кисловодск",
       ("Кисловодск",)),
    _d("pyatigorsk", "Пятигорск", "Pyatigorsk", RU, "RU", "city", "spa mountains city history", 35,
       "Курортный город у горы Машук, связанный с Лермонтовым.", "Пятигорск", ("Пятигорск",)),
    _d("elbrus", "Приэльбрусье (Терскол)", "Elbrus area (Terskol)", RU, "RU", "resort", "mountains ski snow nature", 45,
       "Посёлок у подножия Эльбруса, высочайшей вершины России.", "Терскол", (), 3.0),
    _d("dombay", "Домбай", "Dombay", RU, "RU", "resort", "mountains ski snow nature", 35,
       "Горнолыжный посёлок в Тебердинском национальном парке.", "Домбай", (), 3.0),
    _d("arkhyz", "Архыз", "Arkhyz", RU, "RU", "resort", "mountains ski snow nature", 30,
       "Всесезонный горный курорт в Карачаево-Черкесии.", "Романтик, Архыз", (), 4.0),
    _d("sheregesh", "Шерегеш", "Sheregesh", RU, "RU", "resort", "mountains ski snow resort", 45,
       "Горнолыжный курорт в Кемеровской области на горе Зелёная.", "Шерегеш", (), 3.0),
    _d("abrau-durso", "Абрау-Дюрсо", "Abrau-Dyurso", RU, "RU", "village", "lake village food nature", 25,
       "Посёлок у озера Абрау с винодельней, основанной в XIX веке.", "Абрау-Дюрсо", (), 3.0, coastal=False),
    _d("ostashkov", "Селигер (Осташков)", "Seliger (Ostashkov)", RU, "RU", "nature", "lake nature forest", 25,
       "Город на полуострове озера Селигер.", "Осташков", ("Осташков",), 3.0, coastal=False),
]

BY_ID = {d.id: d for d in DESTINATIONS}

# Curated colloquial / alternative names (destinations.aliases). Only names people actually use
# for that one place: slang ("Питер", "Екб"), common alternative spellings ("Анталия", "Пукет"),
# and the plain names hidden behind "Region (Town)" titles ("Байкал", "Хибины"). A name that
# would point at several catalog places is given to one place only (e.g. "КМВ" -> Kislovodsk).
# Destinations not listed get [].
ALIASES: dict[str, list[str]] = {
    "saint-petersburg": ["Питер", "СПб", "Петербург", "Ленинград"],
    "moscow": ["Мск"],
    "yekaterinburg": ["Екб", "Екат"],
    "nizhny-novgorod": ["Нижний"],
    "novosibirsk": ["Новосиб"],
    "vladivostok": ["Владик"],
    "rostov-on-don": ["Ростов"],
    "veliky-novgorod": ["Новгород"],
    "kaliningrad": ["Кёнигсберг"],
    "antalya": ["Анталия"],
    "alanya": ["Алания"],
    "fethiye": ["Фетхийе"],
    "cappadocia": ["Каппадокия", "Гёреме", "Гереме"],
    "hurghada": ["Хургхада"],
    "sharm-el-sheikh": ["Шарм", "Шарм-эль-Шейх"],
    "marsa-alam": ["Марса Алам"],
    "dubai": ["Дубаи"],
    "abu-dhabi": ["Абу Даби"],
    "ras-al-khaimah": ["Рас-аль-Хайма", "РАК"],
    "phuket": ["Пукет", "Патонг"],
    "pattaya": ["Паттайа"],
    "koh-samui": ["Ко Самуи"],
    "krabi": ["Краби", "Ао Нанг"],
    "nha-trang": ["Нья Чанг"],
    "phu-quoc": ["Фу Куок"],
    "da-nang": ["Да Нанг"],
    "ho-chi-minh": ["Сайгон"],
    "maldives-male": ["Мальдивы", "Мале"],
    "sanya": ["Санья", "Хайнань"],
    "heraklion": ["Крит", "Ираклион"],
    "santorini": ["Санторини", "Фира"],
    "mallorca": ["Майорка", "Пальма-де-Майорка"],
    "lloret-de-mar": ["Льорет"],
    "seychelles": ["Сейшелы", "Маэ"],
    "punta-cana": ["Пунта Кана"],
    "karlovy-vary": ["Карловы-Вары"],
    "almaty": ["Алма-Ата"],
    "astana": ["Нур-Султан"],
    "issyk-kul": ["Иссык-Куль", "Чолпон-Ата"],
    "stepantsminda": ["Казбеги", "Степанцминда"],
    "kuala-lumpur": ["Куала Лумпур"],
    "new-york": ["Нью Йорк"],
    "adler": ["Адлер", "Сириус", "Имеретинка"],
    "krasnaya-polyana": ["Поляна", "Роза Хутор"],
    "lazarevskoe": ["Лазаревская"],
    "abrau-durso": ["Абрау"],
    "kislovodsk": ["КавМинВоды", "КМВ", "Минводы"],
    "elbrus": ["Эльбрус", "Приэльбрусье", "Терскол"],
    "altai": ["Алтай", "Горный Алтай", "Чемал"],
    "listvyanka": ["Байкал", "Листвянка"],
    "olkhon": ["Ольхон", "Хужир"],
    "kamchatka": ["Камчатка", "Петропавловск-Камчатский"],
    "kirovsk": ["Хибины", "Кировск"],
    "petrozavodsk": ["Карелия", "Петрозаводск"],
    "ostashkov": ["Селигер", "Осташков"],
}


def nominatim_search(http, query: str, country_code: str | None = None, limiter: RateLimiter | None = None,
                     cache=True) -> list[dict]:
    """One Nominatim /search call (cached by query). Returns the parsed list."""
    key = f"{query}|{country_code or ''}"
    path = cache_path("nominatim", f"{_slug(key)}.json")

    def fetch():
        if limiter:
            limiter.wait()
        params = {"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1, "namedetails": 1}
        if country_code:
            params["countrycodes"] = country_code.lower()
        r = http.get(NOMINATIM_URL, params=params, timeout=30)
        if r.status != 200:
            raise RuntimeError(f"Nominatim HTTP {r.status}")
        return r.text
    if cache:
        text, _ = cached_text(path, fetch)
    else:
        text = fetch()
    return json.loads(text)


def _slug(s: str) -> str:
    import hashlib
    import re
    base = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-")[:40].lower()
    return f"{base}-{hashlib.sha256(s.encode()).hexdigest()[:10]}"


def nearest_airports(con, lat: float, lon: float, scheduled: set[str] | None = None,
                     radius_km: float = AIRPORT_RADIUS_KM, limit: int = MAX_AIRPORTS,
                     country_code: str | None = None, exclude: set[str] | frozenset = frozenset(),
                     near_km: float = AIRPORT_NEAR_KM) -> list[str]:
    """Airports that really serve the destination, closest first.

    Rule: every scheduled airport within `near_km` (75 km); if none of those is a large_airport,
    also the nearest scheduled large_airport within `radius_km` (150 km) — so Alanya keeps AYT
    after GZP, while Antalya gets only AYT (not Isparta or Denizli). The rule is applied to the
    destination's own country first; when it yields nothing, the nearest scheduled domestic
    airport of any type within 150 km; foreign airports only when there is no domestic one
    (avoids Savonlinna for Sortavala across a closed border). `exclude` drops airports of
    contested territories. At most `limit` codes.
    """
    dlat = radius_km / 111.0
    rows = con.execute("SELECT iata, lat, lon, country_code, type FROM airports WHERE iata IS NOT NULL"
                       " AND lat BETWEEN ? AND ?", (lat - dlat, lat + dlat)).fetchall()
    cands = []
    for iata, alat, alon, cc, typ in rows:
        if iata in exclude:
            continue
        if scheduled is not None and iata not in scheduled and iata not in SERVED_OVERRIDES:
            continue
        d = haversine_m(lat, lon, alat, alon) / 1000
        if d <= radius_km:
            cands.append((round(d, 3), iata, cc, typ))

    def pick(group):
        group = sorted(group)
        near = [c for c in group if c[0] <= near_km]
        if not any(c[3] == "large_airport" for c in near):
            large = [c for c in group if c[3] == "large_airport"]
            if large:
                near = sorted(near + large[:1])
        return [c[1] for c in near[:limit]]

    dom = [c for c in cands if country_code is None or c[2] == country_code]
    # fallback when the rule yields nothing domestic: the nearest scheduled domestic airport of
    # any type within radius_km (Sheregesh -> NOZ, 123 km)
    return pick(dom) or [c[1] for c in sorted(dom)[:1]] or pick([c for c in cands if c not in dom])


def validate_destinations(dests=DESTINATIONS) -> list[str]:
    from agent.state import DESTINATION_TRAITS
    errors = []
    ids = set()
    for d in dests:
        if d.id in ids:
            errors.append(f"duplicate id {d.id}")
        ids.add(d.id)
        bad = [t for t in d.tags if t not in DESTINATION_TRAITS]
        if bad:
            errors.append(f"{d.id}: unknown tags {bad}")
        if d.kind not in ("city", "resort", "nature", "village"):
            errors.append(f"{d.id}: bad kind {d.kind}")
        if not (1 <= d.popularity <= 100):
            errors.append(f"{d.id}: popularity out of range")
        if len(d.country_code) != 2:
            errors.append(f"{d.id}: bad country code")
    seen: dict[str, str] = {}
    for d in dests:
        for n in (d.name_ru, d.name_en):
            seen.setdefault(n.casefold(), d.id)
    for did, names in ALIASES.items():
        if did not in ids:
            errors.append(f"aliases for unknown destination {did}")
        for n in names:
            owner = seen.setdefault(n.casefold(), did)
            if owner != did:
                errors.append(f"alias {n!r} of {did} already names {owner}")
    return errors


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().replace("ı", "i").strip()


def pick_nominatim(results: list[dict], country_code: str, query: str = "") -> dict | None:
    """Best hit in the right country: a hit whose own name equals the first part of the query
    first (Nominatim ranks the city of Antalya above the town of Alanya for "Alanya, Antalya"),
    then Nominatim's order with settlements and administrative areas before other objects, and
    rivers, lakes, roads and natural features (the river Chemal, Lake Sevan) last."""
    head = _norm(query.split(",")[0])

    def name_match(r):
        names = [r.get("name") or ""] + [v for k, v in (r.get("namedetails") or {}).items()
                                         if k in ("name", "name:en", "name:ru", "int_name", "name:tr")]
        return bool(head) and any(_norm(n) == head for n in names)
    def rank(r):
        cat = r.get("category")
        if cat == "place" or (cat == "boundary" and r.get("type") == "administrative"):
            return 0
        if cat in ("water", "waterway", "highway", "natural", "boundary", "landuse"):
            return 2
        return 1
    hits = [r for r in results
            if ((r.get("address") or {}).get("country_code") or "").upper() == country_code.upper()]
    if not hits:
        return None
    def too_big(r):  # provinces and regions share the city's name (Chiang Mai, Paphos)
        return r.get("addresstype") in ("state", "province", "state_district", "region", "county", "country")
    return sorted(enumerate(hits), key=lambda x: (too_big(x[1]), not name_match(x[1]), rank(x[1]), x[0]))[0][1]


def run_destinations(db_path=OPENDATA_DB, http=None, only: list[str] | None = None, limit: int | None = None,
                     sleep=time.sleep) -> dict:
    errs = validate_destinations()
    if errs:
        raise RuntimeError("; ".join(errs))
    http = http or HttpxClient()
    limiter = RateLimiter(1.1, sleep=sleep)
    con = open_db(db_path, "opendata")
    if not con.execute("SELECT 1 FROM airports LIMIT 1").fetchone():
        raise RuntimeError("destinations stage needs the airports stage first")
    scheduled = set(get_meta(con, "airports.scheduled_iata", []) or [])
    contested = set(get_meta(con, "airports.contested_iata", []) or [])
    dests = [d for d in DESTINATIONS if not only or d.id in only]
    if limit:
        dests = dests[:limit]
    stats = {"rows": 0, "fallback_coords": [], "no_airports": []}
    for d in dests:
        source = "curated+nominatim"
        lat = lon = None
        try:
            res = nominatim_search(http, d.q, d.country_code, limiter)
            hit = pick_nominatim(res, d.country_code, d.q)
            if hit:
                lat, lon = float(hit["lat"]), float(hit["lon"])
        except Exception as e:  # network trouble: fall back to OurAirports coordinates
            log(f"destinations: nominatim failed for {d.id}: {e}")
        if lat is None:
            row = con.execute("SELECT lat, lon FROM airports WHERE country_code=? AND (city=? OR city LIKE ?) "
                              "ORDER BY type LIMIT 1", (d.country_code, d.name_en, d.name_en + "%")).fetchone()
            if not row:
                log(f"destinations: no coordinates for {d.id}; skipped")
                stats["fallback_coords"].append(f"{d.id}:none")
                continue
            lat, lon = row
            source = "curated+ourairports"
            stats["fallback_coords"].append(d.id)
        airports = nearest_airports(con, lat, lon, scheduled or None, country_code=d.country_code,
                                    exclude=contested)
        from .timezones import tz_for
        tz, tz_method = tz_for(d.id, d.country_code, lon)
        if tz_method in ("longitude", "unknown"):
            stats.setdefault("tz_not_curated", []).append(f"{d.id}:{tz_method}")
        if not airports:
            stats["no_airports"].append(d.id)
        fetched_at = _now()
        with con:
            con.execute(
                "INSERT INTO destinations(id, name_ru, name_en, country, country_code, lat, lon, kind, tags, airports,"
                " stations, aliases, timezone, description_ru, popularity, source, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name_ru=excluded.name_ru, name_en=excluded.name_en,"
                " country=excluded.country, country_code=excluded.country_code, lat=excluded.lat, lon=excluded.lon,"
                " kind=excluded.kind, tags=excluded.tags, airports=excluded.airports, stations=excluded.stations,"
                " timezone=excluded.timezone, aliases=excluded.aliases,"
                " description_ru=excluded.description_ru, popularity=excluded.popularity, source=excluded.source,"
                " fetched_at=excluded.fetched_at",
                (d.id, d.name_ru, d.name_en, d.country, d.country_code, round(lat, 6), round(lon, 6), d.kind,
                 jdump(list(d.tags)), jdump(airports), jdump(list(d.stations)), jdump(ALIASES.get(d.id, [])), tz,
                 d.description_ru,
                 round(d.popularity / 100, 2), source, fetched_at))
        stats["rows"] += 1
    with con:
        from .common import set_meta
        set_meta(con, "source.destinations", {
            "curated": "data_build/destinations.py (names, kind, tags, stations, description, popularity estimate)",
            "coords": "Nominatim (OSM) search, cached; fallback OurAirports city coordinates",
            "popularity": "curated estimate 1..100 stored as 0..1",
            "aliases": "curated ALIASES in data_build/destinations.py",
            "timezone": "offline table data_build/timezones.py (country / curated region table / longitude bands)",
            "fetched_at": _now()})
    con.close()
    log(f"destinations: {stats['rows']} rows; coord fallbacks {stats['fallback_coords']}; "
        f"no airports {stats['no_airports']}")
    return stats


def _now():
    from .common import now_iso
    return now_iso()
