"""What the system does around the core: keep the session, the profile and the trip in progress."""
from datetime import datetime
from pathlib import Path


class AppError(Exception):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class App:
    def __init__(self, core, store, runtime):
        self.core, self.store = core, store
        self.trips = (Path(runtime) / "trips").resolve()

    def start(self, user):
        return self.store.new_conversation(user)

    def message(self, key, text, attachments=None):
        convo = self.store.conversation(key)
        request = {"kind": "message", "text": text, "profile": self.store.profile(convo["user"]), "now": now()}
        if attachments:
            request["attachments"] = attachments
        if self._travelling(convo):
            request["itinerary"] = convo["itinerary"]
        return self._ask(convo, request)

    def choose(self, key, variant_id):
        convo = self.store.conversation(key)
        itinerary = convo["itinerary"]
        if not itinerary or variant_id not in {v["id"] for v in itinerary.get("variants", [])}:
            raise AppError(f"no variant {variant_id!r} in this conversation")
        itinerary["chosen_variant"] = variant_id
        self.store.save(key, convo)
        return self.view(convo, itinerary, {})

    def event(self, key, event):
        convo = self.store.conversation(key)
        if not self._travelling(convo):
            raise AppError("choose a variant before sending events")
        request = {"kind": "event", "event": event, "itinerary": convo["itinerary"],
                   "profile": self.store.profile(convo["user"]), "now": now()}
        return self._ask(convo, request)

    def _travelling(self, convo):
        return bool(convo["itinerary"] and convo["itinerary"].get("chosen_variant"))

    def _ask(self, convo, request):
        reply, session_id, meta = self.core.ask(request, convo["session_id"])
        convo["session_id"] = session_id
        convo["replies"].append({"at": now(), "request": {k: v for k, v in request.items() if k != "itinerary"}, "type": reply["type"], "meta": meta})
        self.store.merge_profile(convo["user"], reply.get("profile_updates"))
        if reply["type"] in ("itinerary", "update") and reply.get("variants"):
            chosen = (convo["itinerary"] or {}).get("chosen_variant")
            if chosen and "chosen_variant" not in reply:
                reply["chosen_variant"] = chosen
            convo["itinerary"] = reply
        self.store.save(convo["id"], convo)
        return self.view(convo, reply, meta)

    def view(self, convo, reply, meta):
        files = reply.get("files", {})
        urls = {kind: {k: self.file_url(p) for k, p in files.get(kind, {}).items()} for kind in ("calendars", "pages")}
        return {"conversation": convo["id"], "reply": reply, "urls": urls, "meta": meta,
                "chosen_variant": (convo["itinerary"] or {}).get("chosen_variant")}

    def file_url(self, path):
        """Map a file the core wrote to its /files/ URL; None outside the trips folder."""
        p = Path(path)
        if not p.is_absolute():
            p = self.trips.parent / p
        try:
            return "/files/" + p.resolve().relative_to(self.trips).as_posix()
        except ValueError:
            return None
