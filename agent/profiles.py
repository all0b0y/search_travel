"""User profiles (runtime/profiles.sqlite): home city, age, preferences, past trips.

    profiles = ProfileStore()          # or ProfileStore(":memory:")
    profile = profiles.get("demo")     # built-in demo profile: lives in Moscow
    brief = apply_profile(brief, profile)

Profile values enter the brief only through `agent.state.assume(..., "profile")` and never override
what the user said (source "user").
"""
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from .state import Prefs, Segment, assume

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_DB = REPO_ROOT / "runtime" / "profiles.sqlite"

PREF_KEYS = tuple(Prefs.model_fields)


class PastTrip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: Optional[str] = None
    destination_name: Optional[str] = None
    month: Optional[int] = PField(default=None, ge=1, le=12)
    year: Optional[int] = None
    segment: Optional[Segment] = None
    nights: Optional[int] = None


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    home_city: Optional[str] = None
    home_iata: list[str] = PField(default_factory=list)
    age: Optional[int] = PField(default=None, ge=0, le=120)
    # brief prefs (keys of agent.state.Prefs) plus "segment" (usual party segment) and "nights"
    preferences: dict[str, Any] = PField(default_factory=dict)
    past_trips: list[PastTrip] = PField(default_factory=list)


# The demo user lives in Moscow (the Moscow airports are a fact, not a preference); no invented
# preferences: everything else is learned from what the user says.
DEMO_PROFILE = Profile(id="demo", home_city="Москва", home_iata=["SVO", "DME", "VKO"], age=34)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id    TEXT PRIMARY KEY,
    data  TEXT NOT NULL
);
"""


class ProfileStore:
    def __init__(self, path=DEFAULT_PROFILES_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def get(self, profile_id):
        if not profile_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT data FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row:
            return Profile.model_validate_json(row[0])
        if profile_id == DEMO_PROFILE.id:
            return DEMO_PROFILE.model_copy(deep=True)
        return None

    def put(self, profile):
        with self._lock:
            self._conn.execute("INSERT INTO profiles(id, data) VALUES (?, ?) "
                               "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                               (profile.id, profile.model_dump_json()))

    def close(self):
        with self._lock:
            self._conn.close()


def profile_value(profile, path):
    """The profile's value for a brief path (used for questions' recommended choice), or None."""
    if profile is None:
        return None
    if path == "origin.city":
        return profile.home_city
    if path == "party.segment":
        return profile.preferences.get("segment")
    if path == "dates.nights":
        return profile.preferences.get("nights")
    if path == "budget_rub.max":
        return profile.preferences.get("budget_rub")
    top, _, sub = path.partition(".")
    if top == "prefs" and sub in PREF_KEYS:
        return profile.preferences.get(sub)
    return None


def apply_profile(brief, profile):
    """Fill brief fields the user has not given from the profile (source "profile")."""
    if profile is None:
        return brief
    if profile.home_city and not brief.has("origin"):
        brief = assume(brief, "origin", {"city": profile.home_city, "iata": list(profile.home_iata)}, "profile")
    for key in PREF_KEYS:
        if key not in profile.preferences:
            continue
        if brief.source_of(f"prefs.{key}") == "user" and brief.prefs.value is not None:
            continue
        brief = assume(brief, f"prefs.{key}", profile.preferences[key], "profile")
    return brief
