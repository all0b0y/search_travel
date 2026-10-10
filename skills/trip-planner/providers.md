# Known providers

How to reach services users commonly name, as of 2026-10. Access and terms change: when a service matters and its row is old, check its developer pages.

| Category | Service | Access | Notes |
|---|---|---|---|
| flights | Kiwi.com | mcp `https://mcp.kiwi.com` | free, no key; search with a booking link |
| flights | Duffel | api_key `https://api.duffel.com` | real tickets; test key on signup, live needs company KYC; signup https://duffel.com |
| flights | Aviasales / Travelpayouts Data API | api_key | free key, cached prices 2–7 days old; signup https://www.travelpayouts.com |
| flights | Skyscanner, Google Flights | site_only | Skyscanner API needs ≥100k monthly users |
| stays | Booking.com | site_only | Demand API and MCP for approved partners only |
| stays | Duffel Stays | api_key | on request in a Duffel account |
| stays | Airbnb, Ostrovok, Hotels.com | site_only | |
| trains | Omio, Trainline, 12Go | site_only | booking APIs by contract |
| trains | national operators (DB, SNCF, Renfe, CP, РЖД) | site_only | |
| taxi | Uber | site_only | one-click link `https://m.uber.com/ul/?action=setPickup&pickup[formatted_address]=<from>&dropoff[formatted_address]=<to>`; ride API needs Uber approval |
| taxi | Bolt, FreeNow, Yandex Go, local taxi | site_only | no public ride API |
| transfers | Welcome Pickups, Kiwitaxi, Intui | site_only | fixed-price pre-booked transfers |
| transfers | GetTransfer | api_key | via Travelpayouts, approval needed |
| rates | Frankfurter | mcp `https://mcp.frankfurter.dev/` | provider `CBR` gives Central Bank of Russia rates |
| rates | Central Bank of Russia | public_api `https://www.cbr.ru/scripts/XML_daily.asp` | divide by `Nominal` |
