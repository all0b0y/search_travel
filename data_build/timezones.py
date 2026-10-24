"""IANA timezones of destinations, derived offline (no guessing left silent).

Method:
1. Countries with a single IANA zone used for all their territory in this catalog get
   COUNTRY_TZ[country_code].
2. Multi-zone countries (RU, US, MX, ES, ID, KZ, PT, FR, UZ) use DEST_TZ[destination_id], a curated
   table by region (Russian federal subject -> tzdata zone, e.g. Kaliningrad oblast ->
   Europe/Kaliningrad, Sverdlovsk oblast -> Asia/Yekaterinburg, Altai Republic/Krai ->
   Asia/Barnaul, Kemerovo oblast -> Asia/Novokuznetsk, Irkutsk oblast -> Asia/Irkutsk,
   Kamchatka krai -> Asia/Kamchatka; Canary Islands -> Atlantic/Canary; Bali -> Asia/Makassar;
   Quintana Roo -> America/Cancun).
3. For a destination of a multi-zone country missing from DEST_TZ (e.g. a lazily added city),
   `tz_for(..., lon=...)` picks the zone of RU_LON_BANDS by longitude and returns method
   'longitude' so callers can mark it; unknown countries return (None, 'unknown').
Every zone is validated with zoneinfo by tests. The climate stage additionally records
Open-Meteo's coordinate-derived zone (timezone=auto) in meta `climate_done:<id>`; the report
lists mismatches between the two.
"""
from __future__ import annotations

COUNTRY_TZ = {
    "TR": "Europe/Istanbul", "EG": "Africa/Cairo", "AE": "Asia/Dubai", "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh", "LK": "Asia/Colombo", "MV": "Indian/Maldives", "CN": "Asia/Shanghai",
    "CY": "Asia/Nicosia", "GR": "Europe/Athens", "IT": "Europe/Rome", "TN": "Africa/Tunis",
    "IN": "Asia/Kolkata", "MU": "Indian/Mauritius", "SC": "Indian/Mahe", "TZ": "Africa/Dar_es_Salaam",
    "CU": "America/Havana", "DO": "America/Santo_Domingo", "ME": "Europe/Podgorica", "RS": "Europe/Belgrade",
    "HR": "Europe/Zagreb", "CZ": "Europe/Prague", "HU": "Europe/Budapest", "AT": "Europe/Vienna",
    "DE": "Europe/Berlin", "NL": "Europe/Amsterdam", "GB": "Europe/London", "GE": "Asia/Tbilisi",
    "AM": "Asia/Yerevan", "AZ": "Asia/Baku", "UZ": "Asia/Tashkent", "KG": "Asia/Bishkek",
    "BY": "Europe/Minsk", "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "MY": "Asia/Kuala_Lumpur",
    "SG": "Asia/Singapore", "QA": "Asia/Qatar", "OM": "Asia/Muscat", "JO": "Asia/Amman",
    "MA": "Africa/Casablanca",
}
MULTI_ZONE = ("RU", "US", "MX", "ES", "ID", "KZ", "PT", "FR", "UZ")

_MSK = "Europe/Moscow"
DEST_TZ = {
    # Russia, Moscow time (European Russia incl. Krasnodar krai, Dagestan, Tatarstan, Karelia, Murmansk)
    **{d: _MSK for d in (
        "moscow", "saint-petersburg", "sochi", "adler", "krasnaya-polyana", "anapa", "gelendzhik", "lazarevskoe",
        "tuapse", "derbent", "makhachkala", "kazan", "nizhny-novgorod", "yaroslavl", "suzdal", "vladimir",
        "kostroma", "plyos", "veliky-novgorod", "pskov", "kolomna", "tula", "rostov-on-don", "krasnodar",
        "petrozavodsk", "murmansk", "teriberka", "kirovsk", "sortavala", "kislovodsk", "pyatigorsk", "elbrus",
        "dombay", "arkhyz", "abrau-durso", "ostashkov")},
    "volgograd": "Europe/Volgograd",            # Volgograd oblast (UTC+3)
    "samara": "Europe/Samara",                  # Samara oblast (UTC+4)
    "kaliningrad": "Europe/Kaliningrad", "zelenogradsk": "Europe/Kaliningrad", "svetlogorsk": "Europe/Kaliningrad",
    "yekaterinburg": "Asia/Yekaterinburg",      # Sverdlovsk oblast (UTC+5)
    "novosibirsk": "Asia/Novosibirsk",          # UTC+7
    "altai": "Asia/Barnaul", "belokurikha": "Asia/Barnaul",   # Altai Republic / Altai krai (UTC+7)
    "sheregesh": "Asia/Novokuznetsk",           # Kemerovo oblast (UTC+7)
    "irkutsk": "Asia/Irkutsk", "listvyanka": "Asia/Irkutsk", "olkhon": "Asia/Irkutsk",  # UTC+8
    "vladivostok": "Asia/Vladivostok",          # Primorsky krai (UTC+10)
    "kamchatka": "Asia/Kamchatka",              # UTC+12
    # other multi-zone countries
    "new-york": "America/New_York",
    "cancun": "America/Cancun",                 # Quintana Roo
    "tenerife": "Atlantic/Canary",
    "barcelona": "Europe/Madrid", "madrid": "Europe/Madrid", "mallorca": "Europe/Madrid",
    "lloret-de-mar": "Europe/Madrid",
    "bali": "Asia/Makassar",                    # Central Indonesia time (UTC+8)
    "almaty": "Asia/Almaty", "astana": "Asia/Almaty",
    "tashkent": "Asia/Tashkent",                # Uzbekistan (east)
    "samarkand": "Asia/Samarkand", "bukhara": "Asia/Samarkand", "khiva": "Asia/Samarkand",  # Uzbekistan (west), same UTC+5
    "lisbon": "Europe/Lisbon",
    "paris": "Europe/Paris", "nice": "Europe/Paris",
}

# Fallback for Russian places missing from DEST_TZ: (max longitude, zone), first match wins.
RU_LON_BANDS = (
    (22.9, "Europe/Kaliningrad"), (48.0, _MSK), (55.5, "Europe/Samara"), (66.0, "Asia/Yekaterinburg"),
    (75.0, "Asia/Omsk"), (88.0, "Asia/Novosibirsk"), (97.0, "Asia/Krasnoyarsk"), (113.0, "Asia/Irkutsk"),
    (130.0, "Asia/Yakutsk"), (142.0, "Asia/Vladivostok"), (155.0, "Asia/Magadan"), (180.0, "Asia/Kamchatka"),
)


def tz_for(dest_id: str, country_code: str, lon: float | None = None) -> tuple[str | None, str]:
    """(zone, method) with method in 'destination', 'country', 'longitude', 'unknown'."""
    if dest_id in DEST_TZ:
        return DEST_TZ[dest_id], "destination"
    cc = (country_code or "").upper()
    if cc in COUNTRY_TZ:
        return COUNTRY_TZ[cc], "country"
    if cc == "RU" and lon is not None:
        for max_lon, zone in RU_LON_BANDS:
            if lon <= max_lon:
                return zone, "longitude"
    return None, "unknown"
