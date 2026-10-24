"""Ostrovok mock (MCP over stdio) — tool contract (AGENT_PLAN.md section 7).

Input/output pydantic models for every tool, shared by the server (ostrovok_mcp/)
and the engine's client (agent/mcp_client.py). Conventions:
- money: RUB as int; times: ISO 8601 strings WITH UTC offset (IsoDateTime); dates: `date`;
- ids: strings; generated (synthetic) data carries `synthetic: true`;
- tools that depend on "today" take `today` (date) from the caller, stateful tools take
  `now` (IsoDateTime): the engine passes the session clock, the mock never reads the wall clock.
"""
from datetime import date as Date
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict
from pydantic import Field as PField
from pydantic import model_validator


def _iso_with_offset(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"not an ISO 8601 datetime: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"datetime needs a UTC offset: {value!r}")
    return value


def _hhmm(value: str) -> str:
    hh, sep, mm = value.partition(":")
    if not (sep and hh.isdigit() and mm.isdigit() and len(hh) == 2 and len(mm) == 2
            and int(hh) < 24 and int(mm) < 60):
        raise ValueError(f"time must be HH:MM: {value!r}")
    return value


IsoDateTime = Annotated[str, AfterValidator(_iso_with_offset)]
HHMM = Annotated[str, AfterValidator(_hhmm)]
Rub = Annotated[int, PField(ge=0)]
MealsCode = Literal["none", "breakfast", "half_board", "full_board", "all_inclusive"]
ItemKind = Literal["flight", "train", "hotel", "transfer"]
TransferKind = Literal["taxi", "shuttle", "public", "private"]
SegmentName = Literal["solo", "couple", "family", "friends", "other"]
K_MIN = 20  # insights: only groups with n >= K_MIN are ever returned


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True,
                              serialize_by_alias=True)


class Issue(Model):
    """A compatibility / policy problem found by the mock."""
    code: str                   # e.g. infant_fare, child_bed, hotel_age_limit, dates_mismatch, pets_not_allowed
    message: str                # Russian, shown to the user as is
    severity: Literal["info", "warning", "blocking"] = "warning"
    item_ids: list[str] = PField(default_factory=list)


class StayDates(Model):
    check_in: Date
    check_out: Date

    @model_validator(mode="after")
    def _order(self):
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class Party(Model):
    adults: int = PField(ge=1, le=12)
    children_ages: list[Annotated[int, PField(ge=0, le=17)]] = PField(default_factory=list)


# ---------------------------------------------------------------- quote_destinations

class QuoteDestinationsIn(Model):
    destination_ids: list[str] = PField(min_length=1, max_length=40)
    origin_city: str
    month: Optional[int] = PField(default=None, ge=1, le=12)
    dates: Optional[StayDates] = None
    nights: Optional[int] = PField(default=None, ge=1, le=60)  # used with `month`; mock default 7
    party: Party
    today: Date

    @model_validator(mode="after")
    def _month_or_dates(self):
        if (self.month is None) == (self.dates is None):
            raise ValueError("give exactly one of month or dates")
        return self


class DestinationQuote(Model):
    destination_id: str
    available: bool
    nights: Optional[int] = None
    check_in: Optional[Date] = None
    check_out: Optional[Date] = None
    flight_price_rub: Optional[Rub] = None   # round trip for the party
    stay_price_rub: Optional[Rub] = None
    total_rub: Optional[Rub] = None
    flight_offer_id: Optional[str] = None          # outbound
    return_flight_offer_id: Optional[str] = None   # back
    hotel_offer_id: Optional[str] = None
    notes: list[str] = PField(default_factory=list)  # Russian: why a part is missing or approximated
    currency_notes: list[str] = PField(default_factory=list)  # Russian, e.g. "цены отеля пересчитаны из USD по курсу ЦБ"
    synthetic: bool = True


class QuoteDestinationsOut(Model):
    quotes: list[DestinationQuote]
    synthetic: bool = True


# ---------------------------------------------------------------- hotels

class HotelFilters(Model):
    meals: Optional[MealsCode] = None
    free_cancellation: Optional[bool] = None
    stars_min: Optional[int] = PField(default=None, ge=1, le=5)
    max_price_total: Optional[Rub] = None
    pets: Optional[bool] = None
    kinds: Optional[list[str]] = None


class SearchHotelsIn(Model):
    destination_id: str
    check_in: Date
    check_out: Date
    adults: int = PField(ge=1, le=12)
    children_ages: list[Annotated[int, PField(ge=0, le=17)]] = PField(default_factory=list)
    filters: HotelFilters = PField(default_factory=HotelFilters)
    limit: int = PField(default=20, ge=1, le=40)
    today: Date
    hotel_id: Optional[str] = None   # only this hotel (room / meal upgrades of a chosen stay)
    all_rooms: bool = False          # one offer per fitting room type instead of the cheapest only

    @model_validator(mode="after")
    def _order(self):
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class Room(Model):
    room_id: str
    name: str                   # Russian
    capacity_adults: int
    capacity_children: int = 0
    bed: Optional[str] = None   # e.g. "double", "twin"


class Cancellation(Model):
    free_until: Optional[IsoDateTime] = None
    penalty_pct: int = PField(ge=0, le=100)


class KidsPolicy(Model):
    children_allowed: bool = True
    min_child_age: Optional[int] = None
    infant_cot: Literal["free", "paid", "on_request", "none"] = "on_request"
    extra_bed_price_rub: Optional[Rub] = None


class EarlyCheckin(Model):
    available: bool
    price_rub: Optional[Rub] = None


class HotelOffer(Model):
    offer_id: str
    hotel_id: str
    osm_id: str                 # e.g. "node/123456" (real)
    name: str                   # real (OSM)
    kind: str                   # hotel | apartment | guest_house | hostel | resort ... (real OSM tag)
    stars: Optional[int] = PField(default=None, ge=0, le=5)
    rating: Optional[float] = PField(default=None, ge=0, le=10)
    reviews: int = PField(default=0, ge=0)
    lat: float
    lon: float
    room: Room
    meals: MealsCode
    cancellation: Cancellation
    price_total_rub: Rub        # whole stay for the party
    pets_allowed: bool
    kids_policy: KidsPolicy
    early_checkin: EarlyCheckin
    check_in: IsoDateTime       # earliest regular check-in on the first day
    check_out: IsoDateTime      # latest regular check-out on the last day
    distance_beach_m: Optional[int] = PField(default=None, ge=0)
    distance_center_m: int = PField(ge=0)
    synthetic: bool = True      # price, availability, rating, policies are synthetic


class SearchHotelsOut(Model):
    offers: list[HotelOffer]
    total_found: int = 0
    synthetic: bool = True


class GetHotelIn(Model):
    hotel_id: str


class Photo(Model):
    url: str
    source: Literal["wikimedia", "osm", "other"]
    license: Optional[str] = None


class HotelPolicies(Model):
    check_in_from: HHMM
    check_out_until: HHMM
    pets_allowed: bool
    kids_policy: KidsPolicy
    early_checkin: EarlyCheckin
    cancellation_note: Optional[str] = None  # Russian


class HotelDetails(Model):
    hotel_id: str
    osm_id: str
    destination_id: str
    name: str
    kind: str
    stars: Optional[int] = PField(default=None, ge=0, le=5)
    rating: Optional[float] = PField(default=None, ge=0, le=10)
    reviews: int = 0
    lat: float
    lon: float
    address: Optional[str] = None
    distance_beach_m: Optional[int] = None
    distance_center_m: Optional[int] = None
    policies: HotelPolicies
    rooms: list[Room]
    photos: list[Photo] = PField(default_factory=list)  # real links only; empty when none
    source: str = "osm"
    synthetic: bool = True      # some fields are synthetic: see synthetic_fields
    synthetic_fields: list[str] = PField(default_factory=lambda: ["stars", "rating", "reviews", "policies", "rooms"])


GetHotelOut = HotelDetails


# ---------------------------------------------------------------- flights / trains / transfers

class SearchFlightsIn(Model):
    origin_iata: list[str] = PField(min_length=1)
    dest_iata: list[str] = PField(min_length=1)
    date: Date
    adults: int = PField(ge=1, le=9)
    children: int = PField(default=0, ge=0, le=8)
    infants: int = PField(default=0, ge=0, le=4)
    flex_days: int = PField(default=0, ge=0, le=3)
    today: Date


class FlightSegment(Model):
    origin_iata: str
    dest_iata: str
    dep: IsoDateTime
    arr: IsoDateTime
    carrier: str
    flight_no: str


class Baggage(Model):
    checked_kg: Optional[int] = None   # None = no checked bag included
    cabin_kg: Optional[int] = None


class Fare(Model):
    name: Optional[Literal["basic", "standard", "flex"]] = None   # flex = the flexible_fare upsell
    refundable: bool
    change_fee_rub: Optional[Rub] = None  # None = changes not allowed


class FlightOffer(Model):
    offer_id: str
    carrier: str
    flight_no: str
    origin_iata: str
    dest_iata: str
    segments: list[FlightSegment] = PField(min_length=1)
    dep: IsoDateTime
    arr: IsoDateTime
    duration_min: int = PField(ge=1)
    stops: int = PField(ge=0)
    international: bool
    baggage: Baggage
    fare: Fare
    price_rub: Rub              # whole party
    date: Date
    synthetic: bool = True


class SearchFlightsOut(Model):
    offers: list[FlightOffer]
    synthetic: bool = True


class SearchTrainsIn(Model):
    origin_city: str
    dest_city: str
    date: Date
    passengers: int = PField(ge=1, le=9)
    today: Date


class TrainOffer(Model):
    offer_id: str
    carrier: str
    train_no: str
    from_station: str
    to_station: str
    dep: IsoDateTime
    arr: IsoDateTime
    duration_min: int = PField(ge=1)
    car_class: Literal["seat", "platzkart", "coupe", "sv"]
    price_rub: Rub              # all passengers
    date: Date
    synthetic: bool = True


class SearchTrainsOut(Model):
    offers: list[TrainOffer]    # empty outside domestic RU routes <= ~1000 km
    note: Optional[str] = None  # why it is empty, if it is
    synthetic: bool = True


class TransferPoint(Model):
    """Exactly one form: an airport, a station, or coordinates."""
    iata: Optional[str] = None
    station: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    @model_validator(mode="after")
    def _one_form(self):
        forms = [self.iata is not None, self.station is not None, self.lat is not None or self.lon is not None]
        if sum(forms) != 1 or ((self.lat is None) != (self.lon is None)):
            raise ValueError("give exactly one of iata, station, or lat+lon")
        return self


class HotelRef(Model):
    hotel_id: str


class SearchTransfersIn(Model):
    from_: TransferPoint = PField(alias="from")
    to: HotelRef
    at: IsoDateTime             # pickup time
    pax: int = PField(ge=1, le=20)
    kinds: Optional[list[TransferKind]] = None
    reverse: bool = False       # True: from the hotel to the point (`at` = pickup at the hotel)
    today: Date


class TransferOffer(Model):
    offer_id: str
    kind: TransferKind
    duration_min: int = PField(ge=1)
    price_rub: Rub
    meeting_point: str          # Russian
    synthetic: bool = True


class SearchTransfersOut(Model):
    offers: list[TransferOffer]
    synthetic: bool = True


# ---------------------------------------------------------------- cart

class CartCreateIn(Model):
    session_id: str
    now: IsoDateTime


class CartAddIn(Model):
    session_id: str
    offer_id: str
    kind: ItemKind
    now: IsoDateTime


class CartRemoveIn(Model):
    session_id: str
    offer_id: str
    kind: ItemKind


class CartGetIn(Model):
    session_id: str


class CartItem(Model):
    item_id: str
    offer_id: str
    kind: ItemKind
    title: str                  # Russian
    price_rub: Rub
    start: Optional[IsoDateTime] = None
    end: Optional[IsoDateTime] = None
    details: dict[str, Any] = PField(default_factory=dict)  # snapshot of the offer


class Cart(Model):
    cart_id: str
    session_id: str
    status: Literal["open", "checked_out"] = "open"
    items: list[CartItem] = PField(default_factory=list)
    total_rub: Rub = 0
    issues: list[Issue] = PField(default_factory=list)
    synthetic: bool = True


CartOut = Cart


# ---------------------------------------------------------------- checkout / pay / orders

class Traveller(Model):
    name: str = PField(min_length=1)
    birth_date: Date


class Contact(Model):
    email: Optional[str] = None
    phone: Optional[str] = None

    @model_validator(mode="after")
    def _some(self):
        if not (self.email or self.phone):
            raise ValueError("contact needs email or phone")
        return self


class CheckoutIn(Model):
    cart_id: str
    travellers: list[Traveller] = PField(min_length=1)
    contact: Contact
    now: IsoDateTime


class CheckoutOut(Model):
    order_id: str
    status: Literal["pending_payment"] = "pending_payment"
    total_rub: Rub
    issues: list[Issue] = PField(default_factory=list)  # age policies: infant fare, child beds, hotel age limits
    synthetic: bool = True


class PayIn(Model):
    order_id: str
    method: Literal["mock_card"] = "mock_card"
    now: IsoDateTime


class Booking(Model):
    item_id: str
    booking_number: str


class PayOut(Model):
    order_id: str
    status: Literal["paid"] = "paid"
    bookings: list[Booking]
    synthetic: bool = True


class OrderGetIn(Model):
    order_id: str


class OrderItem(Model):
    item_id: str
    offer_id: str
    kind: ItemKind
    title: str
    price_rub: Rub
    booking_number: Optional[str] = None
    status: Literal["pending", "confirmed", "on_request", "changed", "cancelled"] = "pending"
    start: Optional[IsoDateTime] = None
    end: Optional[IsoDateTime] = None
    details: dict[str, Any] = PField(default_factory=dict)


class OrderEvent(Model):
    at: IsoDateTime
    kind: str                   # created, paid, early_checkin_requested, dates_changed, transfer_added ...
    data: dict[str, Any] = PField(default_factory=dict)


class Order(Model):
    order_id: str
    cart_id: str
    session_id: str
    status: Literal["pending_payment", "paid", "changed", "cancelled"]
    items: list[OrderItem]
    travellers: list[Traveller]
    contact: Contact
    total_rub: Rub
    issues: list[Issue] = PField(default_factory=list)
    events: list[OrderEvent] = PField(default_factory=list)
    synthetic: bool = True


OrderOut = Order


class OrderRequestEarlyCheckinIn(Model):
    order_id: str
    time: HHMM                  # local time at the hotel
    now: IsoDateTime


class OrderRequestEarlyCheckinOut(Model):
    order_id: str
    status: Literal["granted", "on_request", "refused"]
    price_rub: Optional[Rub] = None
    synthetic: bool = True


class OrderChangeDatesIn(Model):
    order_id: str
    new_dates: StayDates
    confirm: bool = False
    now: IsoDateTime


class DatesQuote(Model):
    old_total_rub: Rub
    new_total_rub: Rub
    difference_rub: int         # may be negative
    penalty_rub: Rub = 0


class OrderChangeDatesOut(Model):
    order_id: str
    available: bool
    quote: Optional[DatesQuote] = None
    applied: bool = False
    order: Optional[Order] = None   # the updated order when applied
    issues: list[Issue] = PField(default_factory=list)
    synthetic: bool = True


class OrderAddTransferIn(Model):
    order_id: str
    offer_id: str
    now: IsoDateTime


OrderAddTransferOut = Order


# ---------------------------------------------------------------- insights (anonymous, k >= 20)

class InsightsPopularDestinationsIn(Model):
    origin_city: str
    month: int = PField(ge=1, le=12)
    segment: SegmentName


class PopularDestination(Model):
    destination_id: str
    share: float = PField(ge=0, le=1)
    n: int = PField(ge=K_MIN)


class InsightsPopularDestinationsOut(Model):
    items: list[PopularDestination]
    k_min: int = K_MIN
    synthetic: bool = True


class InsightsSegmentPreferencesIn(Model):
    segment: SegmentName
    destination_kind: Optional[str] = None


class PreferenceShares(Model):
    breakfast: float = PField(ge=0, le=1)
    all_inclusive: float = PField(ge=0, le=1)
    free_cancellation: float = PField(ge=0, le=1)
    direct_flights: float = PField(ge=0, le=1)


class BudgetPercentiles(Model):
    p25: Rub
    p50: Rub
    p75: Rub


class DistrictPreference(Model):
    lively: float = PField(ge=0, le=1)
    quiet: float = PField(ge=0, le=1)
    family: float = PField(ge=0, le=1)
    walkable: float = PField(ge=0, le=1)


class InsightsSegmentPreferencesOut(Model):
    segment: SegmentName
    destination_kind: Optional[str] = None
    available: bool             # False when the group has n < k_min (then the rest is None)
    n: int = 0
    shares: Optional[PreferenceShares] = None
    median_nights: Optional[float] = None
    budget_rub_percentiles: Optional[BudgetPercentiles] = None
    district_preference: Optional[DistrictPreference] = None
    k_min: int = K_MIN
    synthetic: bool = True


PERSONAL_KEYS = ("name", "email", "phone", "birth_date", "traveller", "travellers", "contact", "passport",
                 "session_id", "profile_id", "order_id", "cart_id")


class InsightEvent(Model):
    """An anonymous analytics event: no personal data, no ids that link to a person."""
    kind: str                   # e.g. turn, question_asked, chip_changed, variant_chosen, upsell_accepted, paid
    stage: Optional[str] = None
    segment: Optional[SegmentName] = None
    origin_city: Optional[str] = None
    destination_id: Optional[str] = None
    month: Optional[int] = PField(default=None, ge=1, le=12)
    data: dict[str, Any] = PField(default_factory=dict)
    at: IsoDateTime

    @model_validator(mode="after")
    def _no_personal_data(self):
        bad = [k for k in self.data if k.lower() in PERSONAL_KEYS]
        if bad:
            raise ValueError(f"personal data not allowed in insights events: {bad}")
        return self


class InsightsLogEventIn(Model):
    event: InsightEvent


class InsightsLogEventOut(Model):
    ok: bool = True


# ---------------------------------------------------------------- registry

TOOLS = {
    "quote_destinations": (QuoteDestinationsIn, QuoteDestinationsOut),
    "search_hotels": (SearchHotelsIn, SearchHotelsOut),
    "get_hotel": (GetHotelIn, GetHotelOut),
    "search_flights": (SearchFlightsIn, SearchFlightsOut),
    "search_trains": (SearchTrainsIn, SearchTrainsOut),
    "search_transfers": (SearchTransfersIn, SearchTransfersOut),
    "cart_create": (CartCreateIn, CartOut),
    "cart_add": (CartAddIn, CartOut),
    "cart_remove": (CartRemoveIn, CartOut),
    "cart_get": (CartGetIn, CartOut),
    "checkout": (CheckoutIn, CheckoutOut),
    "pay": (PayIn, PayOut),
    "order_get": (OrderGetIn, OrderOut),
    "order_request_early_checkin": (OrderRequestEarlyCheckinIn, OrderRequestEarlyCheckinOut),
    "order_change_dates": (OrderChangeDatesIn, OrderChangeDatesOut),
    "order_add_transfer": (OrderAddTransferIn, OrderAddTransferOut),
    "insights_popular_destinations": (InsightsPopularDestinationsIn, InsightsPopularDestinationsOut),
    "insights_segment_preferences": (InsightsSegmentPreferencesIn, InsightsSegmentPreferencesOut),
    "insights_log_event": (InsightsLogEventIn, InsightsLogEventOut),
}
TOOL_NAMES = tuple(TOOLS)
