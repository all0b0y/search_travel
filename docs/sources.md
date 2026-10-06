# Источники данных и бронирования

Собрано 2026-10-06 по выдержкам поисковика с официальных страниц (прямой доступ к сайтам из песочницы был закрыт). Цены, пороги и условия — снимок на эту дату: перед подключением сервиса открой его ссылку и сверь.

## Итог

- **Поиск сразу и бесплатно:** Kiwi MCP (перелёты), Frankfurter MCP (курс ЦБ), XML ЦБ РФ напрямую.
- **Настоящее бронирование без договора:** только Duffel (перелёты, Duffel Stays — по запросу). Тестовый ключ сразу; боевой режим — проверка компании (KYC), страны под санкциями исключены, оплата картой через API только в 22 странах, иначе Duffel Links (готовая страница оплаты).
- **Всё остальное** (отели, поезда, трансферы, такси) для одиночного разработчика — партнёрская ссылка, оплата на сайте сервиса. API с бронированием — только по договору, часто с порогом 50–100 тыс. активных пользователей в месяц.

## Перелёты

| Сервис | Поиск / бронь | Доступ | Ссылка |
|---|---|---|---|
| Duffel | бронь | email → тестовый ключ; боевой — KYC компании | [pricing](https://duffel.com/pricing), [payments](https://duffel.com/docs/guides/collecting-customer-card-payments), [Links](https://duffel.com/docs/guides/duffel-links) |
| Kiwi MCP `mcp.kiwi.com` | поиск + ссылка | бесплатно, без ключа | [kiwi.com/en/pages/mcp](https://www.kiwi.com/en/pages/mcp/) |
| Amadeus Self-Service | закрыт 17.07.2026; набор для Claude Code — лист ожидания | — | [developers.amadeus.com](https://developers.amadeus.com/self-service) |
| Kiwi Tequila | бронь | по приглашению | [tequila.kiwi.com](https://tequila.kiwi.com/) |
| Skyscanner Travel API | поиск | ≥100 тыс. пользователей/мес, договор | [partners.skyscanner.net](https://www.partners.skyscanner.net/product/travel-api) |
| Aviasales Data API | поиск, кэш 2–7 дней | бесплатно, Travelpayouts | [support.travelpayouts.com](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API) |

## Жильё

| Сервис | Поиск / бронь | Доступ | Ссылка |
|---|---|---|---|
| Booking.com affiliate | ссылка | бесплатно через CJ | [affiliate](https://www.booking.com/affiliate-program/v2/index.html) |
| Booking.com Demand API / MCP | поиск (бронь — отдельный договор) | Managed Affiliate Partner | [prerequisites](https://developers.booking.com/demand/docs/getting-started/prerequisites), [MCP](https://developers.booking.com/mcp-server/docs/about) |
| Duffel Stays | бронь | аккаунт Duffel + запрос доступа | [docs](https://duffel.com/docs/guides/getting-started-with-stays) |
| Expedia Rapid | бронь | одобрение партнёра, PCI | [join](https://partner.expediagroup.com/en-us/join-us/rapid-api) |
| Hotelbeds APItude | бронь | тест бесплатно, боевой — договор + сертификация | [register](https://developer.hotelbeds.com/register/) |
| Hotellook | закрыт 20.10.2025 | — | [FAQ](https://support.travelpayouts.com/hc/en-us/articles/29534131568530-FAQ-on-the-closure-of-Hotellook) |

## Поезда

| Сервис | Поиск / бронь | Доступ | Ссылка |
|---|---|---|---|
| Omio | ссылка/поиск; Booking API — бронь | партнёрка бесплатно; бронь — договор | [affiliate](https://www.omio.com/affiliate) |
| Trainline | ссылка; Global API — бронь | партнёрка; API — договор (~12 недель) | [affiliates](https://www.thetrainline.com/about-us/partnerships/affiliates) |
| 12Go | ссылка; API по согласию | партнёрка бесплатно | [agent.12go.asia](https://agent.12go.asia/) |
| Rail Europe | бронь | договор | [docs](https://docs.era.raileurope.com/) |

## Такси и трансферы

| Сервис | Поиск / бронь | Доступ | Ссылка |
|---|---|---|---|
| Uber Riders API | оценка цены; вызов — после одобрения Uber | только свои аккаунты до Full Access | [scopes](https://developer.uber.com/docs/riders/guides/scopes) |
| Bolt | публичного API нет | — | [bolt.eu](https://bolt.eu/en/support/articles/360017256060/) |
| Welcome Pickups | виджет/ссылка; API — бронь | партнёрка бесплатно; API — одобрение | [travel API](https://partner.welcomepickups.com/travel-api/) |
| Kiwitaxi | ссылка | одобрение в Travelpayouts | [offer](https://www.travelpayouts.com/en/offers/kiwitaxi-affiliate-program/) |
| GetTransfer | бронь | одобрение в Travelpayouts, ≥6 ч заранее | [API](https://support.travelpayouts.com/hc/en-us/articles/360016375920-GetTransfer-API) |

## Курс рубля

| Источник | Как | Ссылка |
|---|---|---|
| Frankfurter MCP | `claude mcp add frankfurter https://mcp.frankfurter.dev/`, провайдер `CBR` | [frankfurter.dev/mcp](https://frankfurter.dev/mcp/) |
| ЦБ РФ `XML_daily.asp` | бесплатно, без ключа; учитывай поле `Nominal` (курс за 10/100 единиц) | [cbr.ru/development/sxml](https://cbr.ru/development/sxml/) |

## Не проверено

Принимает ли Duffel ИП и из каких стран выходит в боевой режим; требования Expedia; сеть партнёрки Trainline; нужна ли IATA для Rail Europe; есть ли API Kiwitaxi/Intui для небольших партнёров; блокирует ли cbr.ru зарубежные IP; доступен ли Uber из Claude Code.
