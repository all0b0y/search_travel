"""Author the level-3 scenarios v0: 25 main (evals/scenarios/) + 10 holdout (evals/holdout/).

    .venv/bin/python -m evals.authoring.scenarios_v0

Holdout scenarios use other wording, destinations and personas; tests check that no
4-word sequence of a holdout start message appears in a main scenario.
Pinned clock: 2026-10-25 12:00 MSK unless a scenario sets `now`.
"""
from evals.common import HOLDOUT_DIR, SCENARIOS_DIR, write_json

CLICKER = ("Когда на экране есть подходящая кнопка, чип или карточка — нажимает её, а не пишет текстом. ")


def t(name, birth):
    return {"name": name, "birth_date": birth}


def must(name, **args):
    return {"name": name, **({"args": args} if args else {})}


def scen(sid, category, start, profile, facts, must_list, nice, behaviour, max_turns=10, image=None, stop_on_paid=None,
         tags=None, notes=None):
    s = {"id": sid, "category": category,
         "persona": {"start_message": start, "image": image, "profile": profile,
                     "hidden": {"facts": facts, "must": must_list, "nice": nice}, "behaviour": behaviour},
         "max_turns": max_turns}
    if stop_on_paid is not None:
        s["stop_on_paid"] = stop_on_paid
    if tags:
        s["tags"] = tags
    if notes:
        s["notes"] = notes
    return s


def profile(name, age, city, travellers, email):
    return {"name": name, "age": age, "home_city": city, "travellers": travellers, "contact": {"email": email}}


def main_scenarios():
    S = []
    S.append(scen("s01_family_warm_sea", "A", "Хотим к тёплому морю в ноябре с ребёнком, недорого",
        profile("Анна", 34, "Москва", [t("Анна Соколова", "1992-03-14"), t("Дмитрий Соколов", "1990-07-02"),
                                       t("Миша Соколов", "2022-05-10")], "anna.sokolova@example.com"),
        {"child_age": 4, "budget_rub_max": 250000, "nights": 7, "meals": "хотя бы завтраки",
         "flight_limit": "ребёнок плохо переносит перелёты дольше 5 часов", "adults": 2},
        [must("order_paid"), must("party_matches", adults=2, children_ages=[4]), must("total_le", rub=250000),
         must("meals_at_least", level="breakfast")],
        ["перелёт не дольше 5 часов", "спокойный или семейный район"],
        CLICKER + "Пишет коротко. Возраст ребёнка и бюджет называет, только когда спросят или когда видит цены выше бюджета."))

    S.append(scen("s02_solo_mountains", "A", "хочу в горы на недельку в декабре, один, покататься",
        profile("Игорь", 29, "Екатеринбург", [t("Игорь Лебедев", "1997-01-21")], "igor.lebedev@example.com"),
        {"budget_rub_max": 120000, "start_after": "2026-12-10", "skill": "катается уверенно, нужен подъёмник",
         "nights": 7},
        [must("order_paid"), must("party_matches", adults=1), must("total_le", rub=120000),
         must("brief_traits_any", traits=["mountains", "ski", "snow"])],
        ["подъёмники рядом с отелем", "даты после 10 декабря"],
        CLICKER + "Отвечает односложно, без приветствий. Если спрашивают даты — «после 10-го»."))

    S.append(scen("s03_couple_food_weekend", "A", "Хочется на выходные куда-нибудь вкусно поесть и погулять, вдвоём",
        profile("Ольга", 31, "Санкт-Петербург", [t("Ольга Петрова", "1995-09-09"), t("Кирилл Петров", "1993-11-30")],
                "olga.petrova@example.com"),
        {"weekend": "20–22 ноября", "budget_rub_max": 60000, "transport": "лучше поезд, самолёты не любят"},
        [must("order_paid"), must("party_matches", adults=2), must("total_le", rub=60000), must("nights_between", min=1, max=3)],
        ["поезд вместо самолёта", "отель в пешей доступности от ресторанов"],
        CLICKER + "Болтливая, рассказывает, что любит грузинскую кухню. Про поезд говорит, когда видит перелёт."))

    S.append(scen("s04_photo_landmark", "A_photo", "Хочу сюда! Когда лучше поехать?",
        profile("Марина", 27, "Москва", [t("Марина Орлова", "1999-04-18"), t("Артём Орлов", "1998-02-03")],
                "marina.orlova@example.com"),
        {"when": "в начале декабря", "nights": "4–5", "party": "вдвоём с мужем", "budget_rub_max": 150000},
        [must("order_paid"), must("destination_in", names=["istanbul", "Стамбул"]), must("nights_between", min=4, max=5)],
        ["отель недалеко от исторического центра"],
        CLICKER + "Прислала фото Айя-Софии и не знает, как место называется. Если агент угадал Стамбул — радуется.",
        image="evals/images/landmark_hagia_sophia.jpg"))

    S.append(scen("s05_photo_coast", "A_photo", "Хочу туда, где вот так же. Только лететь не больше пяти часов",
        profile("Светлана", 38, "Москва", [t("Светлана Миронова", "1988-06-25"), t("Павел Миронов", "1986-12-12")],
                "svetlana.mironova@example.com"),
        {"when": "в мае", "nights": 7, "party": "вдвоём с мужем", "budget_rub_max": 200000, "max_flight_hours": 5},
        [must("order_paid"), must("brief_traits_any", traits=["sea", "beach"]), must("party_matches", adults=2),
         must("total_le", rub=200000)],
        ["скалы и песчаный пляж", "без пересадок"],
        CLICKER + "Прислала фото скалистого побережья. На вопросы отвечает спокойно, бюджет называет, если спросят.",
        image="evals/images/coast_lagos_portugal.jpg"))

    S.append(scen("s06_istanbul_may", "B", "хочу в Стамбул на майские",
        profile("Алексей", 41, "Москва", [t("Алексей Ким", "1985-08-08"), t("Елена Ким", "1987-10-10")],
                "alexey.kim@example.com"),
        {"direct_only": "только прямые рейсы — жена боится пересадок", "budget_rub_max": 150000,
         "district": "хочется гулять пешком по старому городу", "party": "вдвоём с женой"},
        [must("order_paid"), must("destination_in", names=["istanbul", "Стамбул"]), must("direct_flights_only"),
         must("total_le", rub=150000)],
        ["пешая доступность до старого города"],
        CLICKER + "Про прямые рейсы говорит сразу, как увидит пересадку или если спросят про перелёт."))

    S.append(scen("s07_kaliningrad_summer", "B", "Калининград, недельку летом, с мужем",
        profile("Татьяна", 45, "Москва", [t("Татьяна Егорова", "1981-05-05"), t("Сергей Егоров", "1979-03-03")],
                "tatiana.egorova@example.com"),
        {"when": "начало июля", "free_cancellation": "обязательно бесплатная отмена — могут не отпустить с работы",
         "nights": 7},
        [must("order_paid"), must("destination_in", names=["kaliningrad", "Калининград"]), must("free_cancellation"),
         must("nights_between", min=6, max=8)],
        ["рядом с морем (Светлогорск или Зеленоградск тоже подойдут)"],
        CLICKER + "Аккуратная, переспрашивает условия отмены перед оплатой."))

    S.append(scen("s08_sochi_exact", "C", "Сочи, 12–19 ноября, двое взрослых",
        profile("Роман", 36, "Москва", [t("Роман Зайцев", "1990-02-14"), t("Юлия Зайцева", "1991-07-19")],
                "roman.zaitsev@example.com"),
        {"meals": "нужен завтрак", "cancellation": "хорошо бы бесплатная отмена, но не обязательно"},
        [must("order_paid"), must("stay_starts_on", day="2026-11-12"), must("nights_between", min=7, max=7),
         must("meals_at_least", level="breakfast")],
        ["бесплатная отмена"],
        CLICKER + "Знает, чего хочет, отвечает быстро. Про завтрак говорит, если в варианте его нет."))

    S.append(scen("s09_kazan_train", "C", "Казань на поезде, 4–7 декабря, едем вдвоём с подругой",
        profile("Ксения", 24, "Москва", [t("Ксения Белова", "2002-01-15"), t("Дарья Фомина", "2001-12-01")],
                "ksenia.belova@example.com"),
        {"transport": "только поезд", "budget_rub_max": 70000, "district": "центр, чтобы гулять пешком"},
        [must("order_paid"), must("transport_is", kind="train"), must("destination_in", names=["kazan", "Казань"]),
         must("stay_starts_on", day="2026-12-04"), must("party_matches", adults=2)],
        ["отель в центре"],
        CLICKER + "Весёлая, пишет с восклицательными знаками. На самолёт не соглашается."))

    S.append(scen("s10_hotel_only_kazan", "D", "Нужен отель в Казани на 3 ночи с 20 ноября, билеты уже есть",
        profile("Олег", 50, "Самара", [t("Олег Морозов", "1976-04-04")], "oleg.morozov@example.com"),
        {"party": "один", "need": "только отель", "budget_rub_max": 25000},
        [must("order_paid"), must("cart_has_kinds", kinds=["hotel"], exactly=True), must("nights_between", min=3, max=3),
         must("stay_starts_on", day="2026-11-20")],
        ["тихо, чтобы выспаться"],
        CLICKER + "Раздражается, если предлагают перелёт: «билеты есть, сказал же»."))

    S.append(scen("s11_flights_only_ekb", "D", "только билеты Москва — Екатеринбург на 20 ноября, обратно 23-го, я один",
        profile("Вадим", 33, "Москва", [t("Вадим Никитин", "1993-06-06")], "vadim.nikitin@example.com"),
        {"need": "только перелёт", "meeting": "встреча в Екатеринбурге в 16:00 — прилететь до 14:00"},
        [must("order_paid"), must("cart_has_kinds", kinds=["flight"], exactly=True), must("arrival_before", hour=14)],
        ["багаж не нужен"],
        CLICKER + "Про встречу в 16:00 говорит, если рейс прилетает позже 14:00 или если спросят про время."))

    S.append(scen("s12_replan_shift", "E", "Сочи, 12–19 ноября, вдвоём",
        profile("Никита", 30, "Москва", [t("Никита Волков", "1996-03-03"), t("Алина Волкова", "1997-08-08")],
                "nikita.volkov@example.com"),
        {"shift": "после первых вариантов вспоминает про работу и просит на 2 дня позже (14–21 ноября)"},
        [must("order_paid"), must("stay_starts_on", day="2026-11-14"), must("nights_between", min=7, max=7)],
        ["цена после сдвига не сильно выше"],
        "После первого ответа с вариантами пишет текстом: «а если на 2 дня позже?». Дальше " + CLICKER.lower()))

    S.append(scen("s13_replan_dog", "E", "Калининград, 5–8 декабря, вдвоём",
        profile("Вера", 37, "Москва", [t("Вера Савина", "1989-09-19"), t("Глеб Савин", "1988-01-01")],
                "vera.savina@example.com"),
        {"pet": "корги, 12 кг, едет с нами", "pet_reveal": "вспоминает про собаку на втором ходу"},
        [must("order_paid"), must("hotel_allows_pets"), must("brief_has_constraint", token="pet:dog")],
        ["рядом парк для прогулок с собакой"],
        "На втором ходу пишет: «ой, кстати, мы с собакой, корги». Дальше " + CLICKER.lower()))

    S.append(scen("s14_replan_budget", "E", "Сочи на неделю в конце ноября, вдвоём",
        profile("Максим", 28, "Москва", [t("Максим Голубев", "1998-10-10"), t("София Голубева", "1999-11-11")],
                "maxim.golubev@example.com"),
        {"budget_rub_max": 90000, "budget_reveal": "называет бюджет, только увидев цены: «дороговато, давай до 90 тысяч»"},
        [must("order_paid"), must("total_le", rub=90000), must("budget_le", rub=90000)],
        ["честно сказать, чем жертвуем ради цены"],
        "Увидев первые варианты, пишет: «дороговато, давай уложимся в 90 тысяч». Дальше " + CLICKER.lower()))

    S.append(scen("s15_after_early_checkin", "F",
        "Анталья, 14–21 ноября, двое взрослых и ребёнок 4 лет, всё включено",
        profile("Наталья", 35, "Москва", [t("Наталья Козлова", "1991-02-02"), t("Андрей Козлов", "1989-05-05"),
                                          t("Лиза Козлова", "2022-03-03")], "natalia.kozlova@example.com"),
        {"after_purchase": "после оплаты спрашивает, можно ли заселиться пораньше — самолёт прилетает утром",
         "child_age": 4},
        [must("order_paid"), must("order_action_done", kind="early_checkin"), must("meals_at_least", level="all_inclusive")],
        ["честный статус раннего заезда (по запросу / платно)"],
        CLICKER + "Оплатив, пишет текстом: «а можно заселиться пораньше? мы прилетаем утром». Получив ответ, завершает.",
        stop_on_paid=False, max_turns=12))

    S.append(scen("s16_after_change_dates", "F", "Калининград, 20–23 ноября, я один",
        profile("Евгений", 44, "Москва", [t("Евгений Тихонов", "1982-07-07")], "evgeny.tikhonov@example.com"),
        {"after_purchase": "после оплаты просит сдвинуть всю поездку на день позже",
         "accepts_surcharge_up_to_rub": 5000},
        [must("order_paid"), must("order_action_done", kind="change_dates")],
        ["агент называет доплату до подтверждения"],
        CLICKER + "Оплатив, пишет: «слушай, а можно всё сдвинуть на день позже?». Соглашается, если доплата до 5 000 ₽.",
        stop_on_paid=False, max_turns=12))

    S.append(scen("s17_family_toddler_ai", "family", "Летим в Анталью в ноябре: мы с женой и малыш полтора года, хотим всё включено",
        profile("Илья", 33, "Москва", [t("Илья Смирнов", "1993-01-11"), t("Мария Смирнова", "1994-04-14"),
                                       t("Тимофей Смирнов", "2025-05-01")], "ilya.smirnov@example.com"),
        {"child_age": 1, "nights": 7, "no_night_flights": "никаких ночных прилётов с малышом", "budget_rub_max": 260000},
        [must("order_paid"), must("party_matches", adults=2, children_ages=[1]), must("meals_at_least", level="all_inclusive"),
         must("no_night_arrival")],
        ["детская кроватка в номере", "трансфер от аэропорта"],
        CLICKER + "Переживает за малыша, спрашивает про кроватку. Про ночные рейсы говорит, когда видит прилёт ночью."))

    S.append(scen("s18_friends_nightlife", "friends", "Едем вчетвером с друзьями в Питер на выходные 27–29 ноября, хотим потусить",
        profile("Денис", 26, "Москва", [t("Денис Яковлев", "2000-02-20"), t("Артур Гусев", "2000-06-16"),
                                        t("Лёша Федоров", "1999-09-29"), t("Влад Титов", "2001-03-13")],
                "denis.yakovlev@example.com"),
        {"party": "4 парня", "district": "центр, бары рядом", "budget_per_person_rub": 20000, "transport": "Сапсан ок"},
        [must("order_paid"), must("party_matches", adults=4), must("district_label_in", labels=["lively"]),
         must("total_le", rub=80000)],
        ["рядом с Рубинштейна или Думской"],
        CLICKER + "Пишет сленгом («го», «норм»), бюджет называет на человека, если спросят."))

    S.append(scen("s19_solo_business", "solo", "Командировка в Нижний Новгород 9–11 декабря, нужен отель в центре и дорога туда-обратно",
        profile("Галина", 48, "Москва", [t("Галина Широкова", "1978-12-08")], "galina.shirokova@example.com"),
        {"district": "центр, пешком до офиса на Большой Покровской", "meals": "завтрак обязателен",
         "transport": "любой, лишь бы утром быть на месте"},
        [must("order_paid"), must("district_label_in", labels=["walkable"]), must("meals_at_least", level="breakfast"),
         must("party_matches", adults=1)],
        ["утренний приезд 9 декабря"],
        CLICKER + "Деловая, без эмоций. Про завтрак говорит, если в варианте его нет."))

    S.append(scen("s20_couple_anniversary", "couple", "Годовщина свадьбы! Хотим куда-то красиво и тепло на 4 ночи в середине декабря",
        profile("Константин", 39, "Москва", [t("Константин Лавров", "1987-03-21"), t("Ирина Лаврова", "1988-08-30")],
                "konstantin.lavrov@example.com"),
        {"stars_min": 4, "budget_rub_max": 300000, "sea": "хочется моря", "nights": 4},
        [must("order_paid"), must("stars_at_least", n=4), must("total_le", rub=300000), must("nights_between", min=4, max=4)],
        ["номер с видом", "тихий район"],
        CLICKER + "Романтичный, рад шуткам, но про деньги спрашивает серьёзно."))

    S.append(scen("s21_pets_cat_nature", "pets", "С котом можно куда-нибудь на природу недалеко от Питера на 3 дня?",
        profile("Полина", 32, "Санкт-Петербург", [t("Полина Ершова", "1994-10-04")], "polina.ershova@example.com"),
        {"pet": "кот, переноска", "transport": "без самолёта", "nights": 3, "budget_rub_max": 40000},
        [must("order_paid"), must("hotel_allows_pets"), must("nights_between", min=2, max=4)],
        ["поезд или электричка", "озеро или лес рядом"],
        CLICKER + "Спокойная. Если вариант без указания, что с животными можно, — переспрашивает."))

    S.append(scen("s22_tight_budget_students", "budget", "Студенты, нас двое, 35 тысяч на всё, хотим куда-нибудь на 2–3 дня из Москвы в ноябре",
        profile("Егор", 20, "Москва", [t("Егор Кузнецов", "2006-02-02"), t("Аня Кузнецова", "2006-05-05")],
                "egor.kuznetsov@example.com"),
        {"budget_rub_max": 35000, "flexible": "даты любые в ноябре", "transport": "поезд или автобус ок"},
        [must("order_paid"), must("total_le", rub=35000), must("party_matches", adults=2)],
        ["честно сказать, если в бюджет не уложиться"],
        CLICKER + "Экономный, спрашивает «а дешевле можно?» один раз, если цена близко к 35 000 ₽."))

    S.append(scen("s23_out_of_catalog_tromso", "out_of_catalog", "Хочу в Тромсё смотреть северное сияние в январе, вдвоём",
        profile("Лидия", 30, "Москва", [t("Лидия Панова", "1996-01-26"), t("Борис Панов", "1995-11-15")],
                "lidia.panova@example.com"),
        {"goal": "северное сияние", "alternative": "согласна на российский север, если агент честно скажет, что Тромсё нет",
         "nights": 4, "budget_rub_max": 160000},
        [must("agent_said", pattern=r"Тромс"), must("order_paid"), must("total_le", rub=160000)],
        ["честное «Тромсё в демо нет» + похожие варианты (Мурманск, Териберка, Кировск)"],
        CLICKER + "Если агент молча предлагает другое место — спрашивает: «а Тромсё что, нельзя?»."))

    S.append(scen("s24_arrival_before_lunch", "C", "Минск, 13–16 декабря, лечу с мамой (ей 68), прилететь до обеда, ночные рейсы не предлагать",
        profile("Анастасия", 40, "Москва", [t("Анастасия Рябова", "1986-06-13"), t("Людмила Рябова", "1958-04-27")],
                "anastasia.ryabova@example.com"),
        {"arrival": "до 14:00", "no_night": "без ночных рейсов", "accessibility": "маме тяжело с лестницами"},
        [must("order_paid"), must("arrival_before", hour=14), must("no_night_arrival"), must("party_matches", adults=2)],
        ["отель с лифтом", "трансфер от аэропорта"],
        CLICKER + "Вежливая, обстоятельная. Про лестницы говорит, если спросят про отель."))

    S.append(scen("s25_changes_mind_nights", "B", "Три подруги, хотим в Питер на 3 ночи в начале декабря, жить в центре и с завтраками",
        profile("Алёна", 29, "Москва", [t("Алёна Громова", "1997-07-07"), t("Катя Лосева", "1997-03-12"),
                                        t("Ира Белкина", "1996-09-01")], "alena.gromova@example.com"),
        {"change": "на втором ходу передумывает: «давайте всё-таки 2 ночи»", "meals": "завтраки обязательно"},
        [must("order_paid"), must("party_matches", adults=3), must("nights_between", min=2, max=2),
         must("meals_at_least", level="breakfast"), must("district_label_in", labels=["walkable", "lively"])],
        ["номер на троих или два номера рядом"],
        "На втором ходу пишет: «давайте всё-таки 2 ночи». Дальше " + CLICKER.lower()))
    return S


def holdout_scenarios():
    H = []
    H.append(scen("h01_sun_from_novosibirsk", "A", "Устали от слякоти. Солнца бы в декабре, мы с мужем, бюджет скромный",
        profile("Людмила", 52, "Новосибирск", [t("Людмила Карпова", "1974-02-11"), t("Виктор Карпов", "1972-09-23")],
                "lyudmila.karpova@example.com"),
        {"budget_rub_max": 180000, "nights": 8, "heat": "не жарче 30 градусов"},
        [must("order_paid"), must("total_le", rub=180000), must("party_matches", adults=2),
         must("origin_is", city="Новосибирск")],
        ["вылет из Новосибирска без лишних пересадок"],
        CLICKER + "Пишет длинно и обстоятельно, просит объяснить простыми словами."))

    H.append(scen("h02_photo_mountains", "A_photo", "Где можно так отдохнуть? Думаю дней на пять в феврале",
        profile("Тимур", 35, "Казань", [t("Тимур Галиев", "1991-10-05")], "timur.galiev@example.com"),
        {"party": "один", "activity": "сноуборд", "budget_rub_max": 110000},
        [must("order_paid"), must("brief_traits_any", traits=["mountains", "ski", "snow"]), must("nights_between", min=4, max=6)],
        ["Кавказ (Домбай, Архыз, Эльбрус) или похожее"],
        CLICKER + "Прислал фото заснеженных гор. Немногословен.",
        image="evals/images/mountains_dombay.jpg"))

    H.append(scen("h03_tbilisi_dream", "B", "Давно мечтаю о Тбилиси. Когда там хорошо? Можно в начале декабря",
        profile("Элина", 28, "Москва", [t("Элина Гарипова", "1998-04-22"), t("Руслан Гарипов", "1996-08-17")],
                "elina.garipova@example.com"),
        {"party": "вдвоём с мужем", "nights": 5, "meals": "завтраки"},
        [must("order_paid"), must("destination_in", names=["tbilisi", "Тбилиси"]), must("nights_between", min=4, max=6)],
        ["старый город пешком"],
        CLICKER + "Мечтательная, задаёт вопросы про погоду."))

    H.append(scen("h04_minsk_solo_exact", "C", "Минск, с 5 по 8 декабря, я один",
        profile("Григорий", 57, "Москва", [t("Григорий Осипов", "1969-11-03")], "grigory.osipov@example.com"),
        {"meals": "без разницы", "district": "поближе к вокзалу"},
        [must("order_paid"), must("stay_starts_on", day="2026-12-05"), must("nights_between", min=3, max=3),
         must("party_matches", adults=1)],
        ["недорогой отель у центра"],
        CLICKER + "Пожилой, пишет без смайликов, ценит краткость."))

    H.append(scen("h05_room_only_ekb", "D", "Мне только номер в Екатеринбурге 14–16 ноября, командировка, перелёт уже купил",
        profile("Станислав", 42, "Москва", [t("Станислав Дьячков", "1984-05-19")], "stas.dyachkov@example.com"),
        {"need": "только проживание", "district": "рядом с Ельцин-центром"},
        [must("order_paid"), must("cart_has_kinds", kinds=["hotel"], exactly=True), must("stay_starts_on", day="2026-11-14"),
         must("nights_between", min=2, max=2)],
        ["завтрак включён"],
        CLICKER + "Если предлагают перелёт — коротко отказывается."))

    H.append(scen("h06_grandma_joins", "E", "Сочи 2–9 января, двое взрослых и сын 9 лет",
        profile("Артём", 38, "Москва", [t("Артём Баранов", "1988-12-24"), t("Ольга Баранова", "1989-06-06"),
                                        t("Саша Баранов", "2017-03-15"), t("Нина Баранова", "1960-01-30")],
                "artem.baranov@example.com"),
        {"change": "на третьем ходу: «ой, с нами ещё бабушка поедет»", "child_age": 9},
        [must("order_paid"), must("party_matches", adults=3, children_ages=[9])],
        ["номер на четверых или два смежных"],
        "На третьем ходу пишет: «ой, с нами ещё бабушка поедет». Дальше " + CLICKER.lower()))

    H.append(scen("h07_after_add_transfer", "F", "Ереван, 10–14 декабря, вдвоём",
        profile("Карина", 31, "Москва", [t("Карина Арутюнян", "1995-05-25"), t("Давид Арутюнян", "1994-02-28")],
                "karina.arutyunyan@example.com"),
        {"after_purchase": "после оплаты просит добавить трансфер из аэропорта"},
        [must("order_paid"), must("order_action_done", kind="add_transfer")],
        ["цена трансфера до подтверждения"],
        CLICKER + "Оплатив, пишет: «добавьте, пожалуйста, трансфер из аэропорта». Получив ответ, завершает.",
        stop_on_paid=False, max_turns=12))

    H.append(scen("h08_new_year_kids", "family", "На новогодние в тепло с детьми 7 и 11 лет, полупансион, вылетаем из Петербурга",
        profile("Ярослава", 39, "Санкт-Петербург", [t("Ярослава Котова", "1987-08-12"), t("Пётр Котов", "1985-04-01"),
                                                    t("Вика Котова", "2019-06-10"), t("Ваня Котов", "2015-02-20")],
                "yaroslava.kotova@example.com"),
        {"children_ages": [7, 11], "nights": 9, "budget_rub_max": 400000},
        [must("order_paid"), must("party_matches", adults=2, children_ages=[7, 11]),
         must("meals_at_least", level="half_board"), must("origin_is", city="Санкт-Петербург")],
        ["анимация для детей"],
        CLICKER + "Многодетная мама, практичная, считает деньги."))

    H.append(scen("h09_corgi_sapsan", "pets", "С корги на выходные в Петербург на Сапсане — реально?",
        profile("Мирон", 34, "Москва", [t("Мирон Белоусов", "1992-12-02")], "miron.belousov@example.com"),
        {"pet": "корги", "dates": "ближайшие выходные после 1 ноября", "transport": "только поезд"},
        [must("order_paid"), must("hotel_allows_pets"), must("transport_is", kind="train")],
        ["честно про правила провоза собак"],
        CLICKER + "Скептичный, переспрашивает, точно ли с собакой пустят."))

    H.append(scen("h10_student_25k", "budget", "Студент, 25 тысяч на всё, куда можно смотаться на 3 дня из Екатеринбурга?",
        profile("Арсений", 19, "Екатеринбург", [t("Арсений Пахомов", "2007-03-09")], "arseny.pakhomov@example.com"),
        {"budget_rub_max": 25000, "when": "в ноябре, даты гибкие"},
        [must("order_paid"), must("total_le", rub=25000), must("party_matches", adults=1),
         must("origin_is", city="Екатеринбург")],
        ["честно, что входит в 25 тысяч"],
        CLICKER + "Пишет с маленькой буквы, без знаков препинания."))
    return H


def main():
    for folder, items in ((SCENARIOS_DIR, main_scenarios()), (HOLDOUT_DIR, holdout_scenarios())):
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.json"):
            old.unlink()
        for s in items:
            write_json(folder / f"{s['id']}.json", s)
        print(f"wrote {len(items)} scenarios to {folder}")


if __name__ == "__main__":
    main()
