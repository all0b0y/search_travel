"""File-backed storage: user profiles and conversations, one JSON file each."""
import json
import re
import threading
import uuid
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for kind in ("users", "conversations"):
            (self.root / kind).mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, kind, key):
        if not SAFE_ID.match(key):
            raise KeyError(key)
        return self.root / kind / f"{key}.json"

    def _read(self, kind, key, default):
        path = self._path(kind, key)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write(self, kind, key, value):
        self._path(kind, key).write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")

    def profile(self, user):
        return self._read("users", user, {}).get("profile", {})

    def merge_profile(self, user, updates):
        with self.lock:
            data = self._read("users", user, {})
            data["profile"] = {**data.get("profile", {}), **(updates or {})}
            self._write("users", user, data)

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
