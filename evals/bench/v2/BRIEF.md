# Task: write a sealed benchmark for a Russian travel-planning agent

You are writing test material **from the user's side** for a chat agent built for a hackathon track of Ostrovok (a Russian travel booking service). You have NOT seen the agent's code, prompts or existing tests, and you must not look for them: do not read any file outside the current directory and do not browse the web. Everything you need is below. The material is a sealed benchmark: it measures how the agent copes with requests nobody tuned it for, so be inventive, realistic and varied.

Write all user-facing text (messages, facts, wishes, behaviour) in natural Russian.

## What the agent is (public description)

- A Russian-speaking chat agent that takes a person from one message to a booked trip: transport there and back + hotel + transfers. It shows about three ready variants (e.g. «Дешевле», «Быстрее», «Комфортнее»), the user picks one, puts it in the cart, enters travellers at checkout, pays (simulated), and can then ask for after-purchase actions.
- Data comes from a mock of Ostrovok with a fixed catalog of 159 destinations (list below) and about 50 hotels each.
- Trips start from six home cities: Москва, Санкт-Петербург, Екатеринбург, Казань, Новосибирск, Самара. Not every place is reachable from every city: `routes.json` in this folder says, for each home city and each destination, whether flights exist ("direct", "connecting" = only with a change, or null = none) and whether trains run (true/false; trains run only inside Russia, including «Сапсан» between Москва and Санкт-Петербург). Transfers: taxi, private car, shuttle, public transport.
- It can: suggest places from wishes (sea, warmth in December, mountains, cheap, close by); honour dates (exact, a month, a season, flexible); party (adults, children with ages, infants); pets (hotels that accept them, carriage terms on trains and flights); a budget; meals; hotel stars; free cancellation; no hostels; a kitchen in the room; step-free access; a baby cot; direct flights only; arrive before a time; no night flights or trains; a hotel near the city centre or near a named sight or district of the destination; tell the best time to go (climate); after purchase: early check-in, change dates, add a transfer. It answers factual questions only from its data (cancellation terms, baggage, check-in times, distances, climate, pet rules).
- It cannot: visas, car rental, excursions and tours, multi-city itineraries, places outside the catalog, cruises.
- Today is 2026-10-25. Trips must fall between 2026-11-01 and 2027-06-30.

## Deliverable 1: 20 persona scenarios (end-to-end)

Write 20 files `scenarios/bench_<nn>_<slug>.json` (nn = 01..20, slug in latin letters). A simulated person (an LLM persona) plays each scenario against the agent: it sends `start_message`, answers the agent's questions from `hidden.facts`, presses buttons, and finally buys or gives up. `hidden.must` are machine-checked requirements on the final state.

Exact format (replace the angle-bracket placeholders; keep every key):

```json
{
  "id": "bench_<nn>_<slug>",
  "category": "<one word: family | couple | solo | friends | business | pets | budget | aftercare | seniors | other>",
  "persona": {
    "start_message": "<the first message, exactly as the person types it>",
    "image": null,
    "profile": {
      "name": "<first name>",
      "age": <age of the person>,
      "home_city": "<one of: Москва, Санкт-Петербург, Екатеринбург, Казань, Новосибирск, Самара>",
      "travellers": [{"name": "<Имя Фамилия>", "birth_date": "YYYY-MM-DD"}],
      "contact": {"email": "<latin>@example.com"}
    },
    "hidden": {
      "facts": {"<topic>": "<what the person knows and will say if asked>"},
      "must": [{"name": "<predicate>", "args": {<args>}}],
      "nice": ["<a soft wish, not checked by machine>"]
    },
    "behaviour": "Когда на экране есть подходящая кнопка, чип или карточка — нажимает её, а не пишет текстом. <character sketch: how this person writes and reacts>"
  },
  "max_turns": <8..12>,
  "stop_on_paid": <true, or false when the person continues after payment with an after-purchase request>
}
```

Rules:
- `start_message`: one realistic message. Mix styles across scenarios: short, long and rambling, typos, slang, emoji, voice-to-text without punctuation, formal. The person does not need to mention everything; `hidden.facts` holds what they would answer if asked (dates, who travels, budget, deal-breakers, preferences). Facts must not contradict the start message.
- Diversity: use all six home cities; at least 16 different destinations from the catalog (avoid using only the most famous ones); segments: solo, couple, family with children of different ages (include one infant), a group of 4–6 friends, a business trip, retired people; budgets: tight, medium, none; months from November to June; both planes and trains inside Russia; special needs: a dog of a specific breed, a cat, a baby cot, a kitchen, step-free access.
- Transport must match `routes.json`: require `direct_flights_only` only where the pair is "direct", `transport_is` "flight" only where flights exist, "train" only where trains run. In at least 2 scenarios the person's FIRST transport wish does not exist for their pair (direct flights where only connecting ones exist, a plane where only trains run, or a place they cannot reach from their city at all); `hidden.facts` says which reasonable alternative they accept (a change, a train, another similar place), and `must` checks that outcome.
- Include: at least 3 scenarios that continue after payment with an after-purchase request (change dates, early check-in or add a transfer) — add `order_action_done` to `must` and set `"stop_on_paid": false`; at least 2 where the person changes their mind midway (another month, fewer nights, a cheaper hotel, a different place); at least 1 where the person asks a factual question before buying (cancellation, baggage, how far the hotel is from a named sight, check-in time); at least 1 where the first wish is unrealistic for the money and the person accepts a reasonable compromise described in `hidden.facts`.
- `travellers` must match the party: every person who travels, with birth dates that give the right ages on the trip dates (children and an infant included).
- `must`: 2–5 checks per scenario, only from the vocabulary below, with exact argument names. Include `order_paid` in every scenario. Every check must be satisfiable by a correct agent with this catalog: do not require things the agent cannot do. Prefer checks a careful agent clearly owes this person (dates, party, place, budget, deal-breakers).
- `nice`: 1–2 soft wishes.

Check vocabulary (state = the end of the dialogue; «the trip» = the variant the person chose / the cart / the order):
- order_paid(): the order is paid.
- total_le(rub): the trip's total price ≤ rub.
- budget_le(rub): the agent recorded a budget ≤ rub.
- hotel_allows_pets(): the trip's hotel accepts pets.
- direct_flights_only(): every flight of the trip is direct.
- free_cancellation(): the trip's hotel can be cancelled for free.
- district_label_in(labels): the hotel's area has one of these labels: lively, quiet, family, walkable.
- meals_at_least(level): meals level ≥ level; order: none < breakfast < half_board < full_board < all_inclusive.
- party_matches(adults, children_ages=None): the agent's party has this many adults and, if given, children of these ages (±1 year).
- destination_in(names): the trip goes to one of these places (Russian names as in the catalog).
- nights_between(min, max): number of hotel nights.
- arrival_before(hour): the outbound trip arrives before this local hour (integer, e.g. 14).
- departure_after(hour): the outbound trip leaves after this local hour.
- no_night_arrival(start="23:00", end="06:00"): the outbound trip does not arrive at night.
- stars_at_least(n): hotel stars ≥ n.
- origin_is(city): the trip starts from this city.
- transport_is(kind): every transport step is of this kind: "train" or "flight".
- dates_within(start, end): the trip lies within these ISO dates.
- stay_starts_on(day): the hotel stay starts on this ISO date.
- cart_has_kinds(kinds, exactly=False): the trip contains these kinds: flight, train, hotel, transfer.
- no_blocking_issues(): nothing blocks payment.
- order_action_done(kind): an after-purchase action reached the order: early_checkin, change_dates or add_transfer.
- agent_said(pattern, before_turn=None): some agent message matches this Python regex (case-insensitive).
- agent_never_said(pattern): no agent message matches.
- brief_traits_any(traits): the agent recorded one of these wishes: sea, beach, mountains, lake, river, island, forest, desert, warm, hot, cold, snow, tropical, city, resort, nature, village, history, culture, food, nightlife, ski, spa, shopping, architecture.
- brief_has_constraint(token): the agent recorded this constraint: pet:dog, pet:cat, accessible, crib, kitchen, no_hostel, no_dorm.
- has_transfer(): the trip includes a transfer.

## Deliverable 2: 40 understanding cases (one message → structured reading)

Write one file `cases.json`: a JSON list of 40 objects. Each tests how the agent's understanding step reads ONE user message. That step outputs JSON like `{"intent": "...", "patch": {"set": {"<path>": <value>}, "unset": ["<path>"]}}`.

```json
{
  "id": "bu_<nn>_<slug>",
  "context": "new" | "selection",
  "base": null,
  "message": "<the user's message in Russian>",
  "expect": [<assertions>]
}
```

- `context` "new": the first message of a conversation (`base` null). 26 cases.
- `context` "selection": the person already sees three variants for a trip and writes a change or a question. 14 cases. `base` describes that trip: `{"destination": "<catalog name>", "origin": "<home city>", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "adults": <n>, "children_ages": [<ages>]}`.

Assertions:
- `{"intent": "<new_trip | refine | answer | choose | compare | info | cart | checkout | post_purchase | smalltalk | out_of_scope>"}` — "new_trip" for a first request, "refine" for a change of the current trip, "info" for a factual question that changes nothing, "out_of_scope" for things the agent cannot do.
- `{"set": "<path>", "equals": <value>}` — the patch sets this path to exactly this value.
- `{"set": "<path>", "in": [<values>]}` — the patch sets this path to one of these values.
- `{"set": "<path>", "present": true}` — the patch sets this path (any value).
- `{"absent": "<path>"}` — the patch does not set this path.
- `{"constraint": "<token>"}` — `constraints.add` contains this token.
- `{"trait": "<trait>"}` — `traits.add` contains this trait.

Patch paths and values:
- destination.name: the place as the user named it (string); origin.city: string.
- dates.start / dates.end: ISO date; dates.month: 1..12; dates.nights: integer; dates.shift_days: integer (move the trip by N days); dates.flexible_days: integer.
- party.adults, party.children: integers; party.children_ages: list of integers; party.pets: list of "dog" / "cat"; party.segment: solo | couple | family | friends | other.
- budget_rub.max: integer rubles for the whole trip.
- prefs.meals: none | breakfast | half_board | full_board | all_inclusive; prefs.free_cancellation: true; prefs.stars_min: 1..5; prefs.direct_only: true; prefs.max_travel_hours: number; prefs.district: lively | quiet | family | walkable; prefs.beach_max_m: metres; prefs.center_max_m: metres from the city centre; prefs.near: the sight or district the user wants to stay near, in nominative, as named; prefs.transport: plane | train; prefs.arrive_before: "HH:MM"; prefs.no_night_flights: true; prefs.domestic: true (only inside Russia).
- traits.add: list from the trait list above; mood.add: relax | party | culture | active | romantic | kids_fun.
- constraints.add: pet:dog, pet:cat, accessible, crib, kitchen, no_hostel, no_dorm.
- scope: list of flights | trains | stay | transfers (what the user wants booked, e.g. only a hotel).

Rules: test robustness to wording. The same meanings in unusual words, synonyms, typos, word order, slang, numbers in words («тыщ сорок», «полтинник»), relative dates («на майские», «после Нового года»), different cities, sights and pet breeds than the obvious ones. Expect only what any careful reader would infer from the message; prefer `in` or `present` when several values are fine, and use `absent` to check that something was NOT invented. 2–4 assertions per case.

## Catalog (159 destinations: Russian name, country, kind, id)

- Вена (Австрия, city; id vienna)
- Баку (Азербайджан, city; id baku)
- Габала (Азербайджан, nature; id gabala)
- Дилижан (Армения, village; id dilijan)
- Ереван (Армения, city; id yerevan)
- Севан (Армения, nature; id sevan)
- Минск (Беларусь, city; id minsk)
- Лондон (Великобритания, city; id london)
- Будапешт (Венгрия, city; id budapest)
- Дананг (Вьетнам, city; id da-nang)
- Нячанг (Вьетнам, resort; id nha-trang)
- Фукуок (Вьетнам, resort; id phu-quoc)
- Ханой (Вьетнам, city; id hanoi)
- Хошимин (Вьетнам, city; id ho-chi-minh)
- Берлин (Германия, city; id berlin)
- Афины (Греция, city; id athens)
- Крит (Ираклион) (Греция, city; id heraklion)
- Родос (Греция, city; id rhodes)
- Санторини (Фира) (Греция, village; id santorini)
- Батуми (Грузия, city; id batumi)
- Гудаури (Грузия, resort; id gudauri)
- Казбеги (Степанцминда) (Грузия, village; id stepantsminda)
- Кутаиси (Грузия, city; id kutaisi)
- Тбилиси (Грузия, city; id tbilisi)
- Пунта-Кана (Доминикана, resort; id punta-cana)
- Каир (Египет, city; id cairo)
- Марса-Алам (Египет, resort; id marsa-alam)
- Хургада (Египет, resort; id hurghada)
- Шарм-эш-Шейх (Египет, resort; id sharm-el-sheikh)
- Гоа (Индия, resort; id goa)
- Бали (Индонезия, resort; id bali)
- Акаба (Иордания, resort; id aqaba)
- Барселона (Испания, city; id barcelona)
- Льорет-де-Мар (Испания, resort; id lloret-de-mar)
- Мадрид (Испания, city; id madrid)
- Майорка (Пальма) (Испания, city; id mallorca)
- Тенерифе (Испания, resort; id tenerife)
- Венеция (Италия, city; id venice)
- Милан (Италия, city; id milan)
- Рим (Италия, city; id rome)
- Римини (Италия, resort; id rimini)
- Флоренция (Италия, city; id florence)
- Алматы (Казахстан, city; id almaty)
- Астана (Казахстан, city; id astana)
- Доха (Катар, city; id doha)
- Айя-Напа (Кипр, resort; id ayia-napa)
- Ларнака (Кипр, city; id larnaca)
- Лимасол (Кипр, city; id limassol)
- Пафос (Кипр, city; id paphos)
- Иссык-Куль (Чолпон-Ата) (Киргизия, resort; id issyk-kul)
- Пекин (Китай, city; id beijing)
- Санья (Хайнань) (Китай, resort; id sanya)
- Шанхай (Китай, city; id shanghai)
- Варадеро (Куба, resort; id varadero)
- Гавана (Куба, city; id havana)
- Маврикий (Маврикий, resort; id mauritius)
- Куала-Лумпур (Малайзия, city; id kuala-lumpur)
- Лангкави (Малайзия, resort; id langkawi)
- Мальдивы (Мале) (Мальдивы, resort; id maldives-male)
- Марракеш (Марокко, city; id marrakech)
- Канкун (Мексика, resort; id cancun)
- Амстердам (Нидерланды, city; id amsterdam)
- Абу-Даби (ОАЭ, city; id abu-dhabi)
- Дубай (ОАЭ, city; id dubai)
- Рас-эль-Хайма (ОАЭ, resort; id ras-al-khaimah)
- Маскат (Оман, city; id muscat)
- Салала (Оман, resort; id salalah)
- Лиссабон (Португалия, city; id lisbon)
- Абрау-Дюрсо (Россия, village; id abrau-durso)
- Адлер (Сириус) (Россия, resort; id adler)
- Алтай (Чемал) (Россия, nature; id altai)
- Анапа (Россия, resort; id anapa)
- Архыз (Россия, resort; id arkhyz)
- Байкал (Листвянка) (Россия, village; id listvyanka)
- Байкал (Ольхон, Хужир) (Россия, village; id olkhon)
- Белокуриха (Россия, resort; id belokurikha)
- Великий Новгород (Россия, city; id veliky-novgorod)
- Владивосток (Россия, city; id vladivostok)
- Владимир (Россия, city; id vladimir)
- Волгоград (Россия, city; id volgograd)
- Геленджик (Россия, resort; id gelendzhik)
- Дербент (Россия, city; id derbent)
- Домбай (Россия, resort; id dombay)
- Екатеринбург (Россия, city; id yekaterinburg)
- Зеленоградск (Россия, resort; id zelenogradsk)
- Иркутск (Россия, city; id irkutsk)
- Казань (Россия, city; id kazan)
- Калининград (Россия, city; id kaliningrad)
- Камчатка (Петропавловск-Камчатский) (Россия, nature; id kamchatka)
- Карелия (Петрозаводск) (Россия, city; id petrozavodsk)
- Кисловодск (Россия, resort; id kislovodsk)
- Коломна (Россия, city; id kolomna)
- Кострома (Россия, city; id kostroma)
- Красная Поляна (Россия, resort; id krasnaya-polyana)
- Краснодар (Россия, city; id krasnodar)
- Лазаревское (Россия, resort; id lazarevskoe)
- Махачкала (Россия, city; id makhachkala)
- Москва (Россия, city; id moscow)
- Мурманск (Россия, city; id murmansk)
- Нижний Новгород (Россия, city; id nizhny-novgorod)
- Новосибирск (Россия, city; id novosibirsk)
- Плёс (Россия, village; id plyos)
- Приэльбрусье (Терскол) (Россия, resort; id elbrus)
- Псков (Россия, city; id pskov)
- Пятигорск (Россия, city; id pyatigorsk)
- Ростов-на-Дону (Россия, city; id rostov-on-don)
- Самара (Россия, city; id samara)
- Санкт-Петербург (Россия, city; id saint-petersburg)
- Светлогорск (Россия, resort; id svetlogorsk)
- Селигер (Осташков) (Россия, nature; id ostashkov)
- Сортавала (Россия, nature; id sortavala)
- Сочи (Россия, resort; id sochi)
- Суздаль (Россия, village; id suzdal)
- Териберка (Россия, village; id teriberka)
- Туапсе (Россия, resort; id tuapse)
- Тула (Россия, city; id tula)
- Хибины (Кировск) (Россия, resort; id kirovsk)
- Шерегеш (Россия, resort; id sheregesh)
- Ярославль (Россия, city; id yaroslavl)
- Нью-Йорк (США, city; id new-york)
- Сейшелы (Маэ) (Сейшелы, resort; id seychelles)
- Белград (Сербия, city; id belgrade)
- Сингапур (Сингапур, city; id singapore)
- Бангкок (Таиланд, city; id bangkok)
- Краби (Ао Нанг) (Таиланд, resort; id krabi)
- Паттайя (Таиланд, resort; id pattaya)
- Пхукет (Таиланд, resort; id phuket)
- Самуи (Таиланд, resort; id koh-samui)
- Чиангмай (Таиланд, city; id chiang-mai)
- Занзибар (Танзания, resort; id zanzibar)
- Джерба (Тунис, resort; id djerba)
- Сусс (Тунис, resort; id sousse)
- Хаммамет (Тунис, resort; id hammamet)
- Аланья (Турция, resort; id alanya)
- Анталья (Турция, resort; id antalya)
- Белек (Турция, resort; id belek)
- Бодрум (Турция, resort; id bodrum)
- Каппадокия (Гёреме) (Турция, village; id cappadocia)
- Кемер (Турция, resort; id kemer)
- Мармарис (Турция, resort; id marmaris)
- Сиде (Турция, resort; id side)
- Стамбул (Турция, city; id istanbul)
- Фетхие (Турция, resort; id fethiye)
- Бухара (Узбекистан, city; id bukhara)
- Самарканд (Узбекистан, city; id samarkand)
- Ташкент (Узбекистан, city; id tashkent)
- Хива (Узбекистан, city; id khiva)
- Ницца (Франция, city; id nice)
- Париж (Франция, city; id paris)
- Дубровник (Хорватия, city; id dubrovnik)
- Будва (Черногория, resort; id budva)
- Котор (Черногория, city; id kotor)
- Карловы Вары (Чехия, city; id karlovy-vary)
- Прага (Чехия, city; id prague)
- Бентота (Шри-Ланка, resort; id bentota)
- Хиккадува (Шри-Ланка, resort; id hikkaduwa)
- Сеул (Южная Корея, city; id seoul)
- Киото (Япония, city; id kyoto)
- Токио (Япония, city; id tokyo)

## Output

Create `scenarios/` with the 20 files and `cases.json` in the current directory. Check every transport requirement against `routes.json`. Validate that every file is valid JSON and follows the formats exactly. Finish with a short list of what you wrote.
