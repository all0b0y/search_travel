"""The train rule shared by the mock (ostrovok_mcp/trains.py) and the engine (agent/opendata.train_reachable):
Russian domestic pairs only, both places with a station, great circle TRAIN_MIN_KM..TRAIN_MAX_KM.

Up to DAY_TRAIN_MAX_KM the mock sells today's mix of day trains, «Сапсан» and overnight sleepers; beyond it
only long-distance sleepers (1–2 a day, platzkart / coupe / sv, up to ~two days on the road). The engine shows
those long trains only when the user wants a train, when the pair has no flight, or in a budget-led
inspiration where they fit the budget. Constants only: no imports, so the engine can read them cheaply.
"""
TRAIN_COUNTRY = "RU"
TRAIN_MIN_KM = 30
TRAIN_MAX_KM = 2500
DAY_TRAIN_MAX_KM = 1000      # beyond it a pair is a long-distance pair (sleepers only)
LONG_TRAIN_LEAD_DAYS = 2     # a long train may leave up to two days before the check-in day
