"""File-backed storage: user profiles, providers, keys and conversations, one JSON file each.

Keys live in data/secrets/, apart from everything else, and leave the store only as
the environment of the core process.
"""
import json
import os
import re
import threading
import uuid
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENV_NAME = re.compile(r"^PROVIDER_[A-Z0-9_]{1,40}_KEY$")
PROVIDER_FIELDS = ("category", "name", "access", "url", "env")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for kind in ("users", "conversations", "secrets"):
            (self.root / kind).mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "secrets", 0o700)
        self.lock = threading.Lock()

    def _path(self, kind, key):
        if not SAFE_ID.match(key):
            raise KeyError(key)
        return self.root / kind / f"{key}.json"

    def _read(self, kind, key, default):
        path = self._path(kind, key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write(self, kind, key, value):
        path = self._path(kind, key)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        if kind == "secrets":
            os.chmod(path, 0o600)

    # --- profile ---

    def profile(self, user):
        return self._read("users", user, {}).get("profile", {})

    def merge_profile(self, user, updates):
        with self.lock:
            data = self._read("users", user, {})
            data["profile"] = {**data.get("profile", {}), **(updates or {})}
            self._write("users", user, data)

    # --- providers and keys ---

    def providers(self, user):
        """The user's providers as the core sees them: has_key instead of any key."""
        keys = self._read("secrets", user, {})
        return [dict(p, has_key=bool(p.get("env") and p["env"] in keys)) for p in self._read("users", user, {}).get("providers", [])]

    def set_providers(self, user, providers):
        clean = [{k: p[k] for k in PROVIDER_FIELDS if p.get(k)} for p in providers]
        for p in clean:
            if not {"category", "name", "access"} <= p.keys():
                raise ValueError(f"provider needs category, name and access: {p}")
            if "env" in p and not ENV_NAME.match(p["env"]):
                raise ValueError(f"env must look like PROVIDER_NAME_KEY: {p['env']}")
        with self.lock:
            data = self._read("users", user, {})
            data["providers"] = clean
            self._write("users", user, data)

    def merge_connections(self, user, connections):
        """Store the services a setup reply settled on, keeping the user's order."""
        current = [{k: v for k, v in p.items() if k != "has_key"} for p in self.providers(user)]
        index = {(p["category"], p["name"].casefold()): i for i, p in enumerate(current)}
        for c in connections or []:
            entry = {k: c[k] for k in PROVIDER_FIELDS if c.get(k)}
            i = index.get((c["category"], c["name"].casefold()))
            if i is None:
                index[(c["category"], c["name"].casefold())] = len(current)
                current.append(entry)
            else:
                current[i] = {**current[i], **entry}
        self.set_providers(user, current)

    def set_key(self, user, env, value):
        if not ENV_NAME.match(env):
            raise ValueError(f"env must look like PROVIDER_NAME_KEY: {env}")
        with self.lock:
            keys = self._read("secrets", user, {})
            keys[env] = value
            self._write("secrets", user, keys)

    def keys(self, user):
        return self._read("secrets", user, {})

    # --- conversations ---

    def new_conversation(self, user):
        key = uuid.uuid4().hex[:12]
        self.save(key, {"id": key, "user": user, "session_id": None, "replies": [], "itinerary": None})
        return key

    def conversation(self, key):
        convo = self._read("conversations", key, None)
        if convo is None:
            raise KeyError(key)
        return convo

    def save(self, key, convo):
        with self.lock:
            self._write("conversations", key, convo)
