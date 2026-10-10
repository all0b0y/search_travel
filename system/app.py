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
        request = {"kind": "message", "text": text, **self._context(convo)}
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
        request = {"kind": "event", "event": event, "itinerary": convo["itinerary"], **self._context(convo)}
        return self._ask(convo, request)

    def providers(self, key):
        return self.store.providers(self.store.conversation(key)["user"])

    def set_providers(self, key, providers):
        user = self.store.conversation(key)["user"]
        try:
            self.store.set_providers(user, providers)
        except ValueError as e:
            raise AppError(str(e)) from e
        return self.store.providers(user)

    def set_key(self, key, env, value):
        """Save a provider key; it reaches the core only as an environment variable."""
        user = self.store.conversation(key)["user"]
        if not value.strip():
            raise AppError("empty key")
        try:
            self.store.set_key(user, env, value.strip())
        except ValueError as e:
            raise AppError(str(e)) from e
        return self.store.providers(user)

    def _context(self, convo):
        user = convo["user"]
        context = {"profile": self.store.profile(user), "now": now()}
        providers = self.store.providers(user)
        if providers:
            context["providers"] = providers
        return context

    def _travelling(self, convo):
        return bool(convo["itinerary"] and convo["itinerary"].get("chosen_variant"))

    def _ask(self, convo, request):
        reply, session_id, meta = self.core.ask(request, convo["session_id"], self.store.keys(convo["user"]))
        convo["session_id"] = session_id
        convo["replies"].append({"at": now(), "request": {k: v for k, v in request.items() if k != "itinerary"}, "type": reply["type"], "meta": meta})
        self.store.merge_profile(convo["user"], reply.get("profile_updates"))
        if reply.get("connections"):
            self.store.merge_connections(convo["user"], reply["connections"])
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
                "chosen_variant": (convo["itinerary"] or {}).get("chosen_variant"),
                "providers": self.store.providers(convo["user"])}

    def file_url(self, path):
        """Map a file the core wrote to its /files/ URL; None outside the trips folder."""
        p = Path(path)
        if not p.is_absolute():
            p = self.trips.parent / p
        try:
            return "/files/" + p.resolve().relative_to(self.trips).as_posix()
        except ValueError:
            return None
