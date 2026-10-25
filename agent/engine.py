"""Engine facade: the turn pipeline (AGENT_PLAN.md section 5, "Module interfaces").

    engine = Engine()                                   # real defaults (skill, OpenRouter, Jev, mock, open data)
    sid = engine.new_session(profile_id="demo", debug=True)
    response = engine.handle(sid, {"session_id": sid, "text": "к тёплому морю в ноябре"})

Per turn: action mapping (no model) or Jev routing / `understand` -> patch validation hook ->
shift_days and image pre-route -> route match -> steps -> validation hooks (one rerun) ->
sensitivity / ask-or-assume -> present card + output guards (one rerun) -> response -> analytics
and prefetch hooks. Budget: <= rules.max_flash_calls_per_turn model calls per turn plus at most one
rerun per card; enforced and recorded in the trace. `handle` never raises for user-caused problems
(unknown destination, no offers, model or tool failure): it answers with an honest error response.
"""
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import jsonschema

from . import jev as jev_module
from . import payloads as P
from .hooks import DEFAULT_HOOKS, as_issue, blocking, issue
from .jev_policy import guard_card_output, route_short_reply
from .llm import LLMError, LLMRequest
from .mcp_client import ToolError, default_client
from .prefetch import Prefetcher
from .profiles import ProfileStore
from .respond import build_response, error_response
from .sessions import SessionStore
from .state import (ALLOWED_PATCH_PATHS, ALLOWED_UNSET_PATHS, CachedResult, PatchError, Session, apply_patch)
from .steps import (STEP_REGISTRY, Ctx, Domain, StepError, after_travellers_hook, check_variants_hook,
                    destination_record, merge_prefetched, prepare_brief, recommended_hint, run_image_match,
                    run_search, resolve_dates, step_log_event, store_result)

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUEST_SCHEMA = json.loads((REPO_ROOT / "agent/contracts/request.schema.json").read_text(encoding="utf-8"))
DEFAULT_FILES_DIR = REPO_ROOT / "runtime" / "files"
STAGE_OF_CARD = {"present.destinations": "inspiration", "present.variants": "selection", "present.update": "selection"}
QUESTION_CARDS = ("present.destinations", "present.variants", "present.update")


class BudgetExceeded(RuntimeError):
    """The pipeline tried to call the model beyond the per-turn budget (a programming error)."""


def _default_lazy_city(name):
    from data_build.lazy import build_city  # network: one attempt per unknown name per session
    return build_city(name, timeout_s=60)


class _Lazy:
    """Build a default dependency on first use (no key/network/DB needed to construct the Engine)."""

    def __init__(self, factory):
        self.factory = factory
        self.value = None
        self.lock = threading.Lock()

    def get(self):
        if self.value is None:
            with self.lock:
                if self.value is None:
                    self.value = self.factory()
        return self.value


def _default_backend():
    from .env import load_env
    from .llm import OpenRouterBackend
    load_env(str(REPO_ROOT / ".env"))
    return OpenRouterBackend()


def _default_opendata():
    from .opendata import OpenData   # ENGINE-B
    return OpenData()


class Engine:
    def __init__(self, skill=None, backend=None, jev=None, ostrovok=None, opendata=None, flags=None, store=None,
                 profiles=None, clock=None, *, domain=None, hooks=None, prefetcher=None, lazy_city=None,
                 files_dir=None):
        if skill is None:
            from .skill_loader import load_skill
            skill = load_skill()
        if flags is None:
            from .flags import parse_flags
            flags = parse_flags()
        self.skill = skill
        self.flags = flags
        self._backend = _Lazy(_default_backend) if backend is None else None
        self._backend_value = backend
        self.jev = jev if jev is not None else jev_module.decide
        self.ostrovok = ostrovok if ostrovok is not None else default_client()
        self._opendata = _Lazy(_default_opendata) if opendata is None else None
        self._opendata_value = opendata
        self.store = store if store is not None else SessionStore()
        self.profiles = profiles if profiles is not None else ProfileStore()
        self.clock = clock
        self.domain = domain if domain is not None else Domain()
        self.prefetcher = prefetcher if prefetcher is not None else Prefetcher()
        if lazy_city is None:
            lazy_city = _default_lazy_city if os.environ.get("TRIP_AGENT_LAZY_CITY", "1") != "0" else False
        self.lazy_city = lazy_city or None
        self.files_dir = Path(files_dir) if files_dir else DEFAULT_FILES_DIR
        self.hooks = hooks if hooks is not None else self._default_hooks()
        self._locks = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------ dependencies
    @property
    def backend(self):
        return self._backend_value if self._backend is None else self._backend.get()

    @property
    def opendata(self):
        if self._opendata is None:
            return self._opendata_value
        try:
            return self._opendata.get()
        except ImportError:
            return None

    def _default_hooks(self):
        hooks = DEFAULT_HOOKS.copy()
        hooks.register("after_understand", validate_patch_hook, "validate", "patch")
        hooks.register("after_variants", check_variants_hook, "validate", "checks")
        hooks.register("after_travellers", after_travellers_hook, "validate", "travellers")
        hooks.register("after_present", prefetch_hook, "prefetch", "prefetch_next")
        hooks.register("turn_end", step_log_event, "analytics", "insights")
        return hooks

    # ------------------------------------------------------------ sessions
    def new_session(self, profile_id=None, debug=False, now=None):
        if now is None and self.clock is not None:
            now = self.clock()
        session = Session.new(now=now, profile_id=profile_id, debug=debug)
        self.store.put(session)
        return session.id

    def get_session(self, session_id):
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"unknown session: {session_id}")
        return session

    def close(self):
        if self.prefetcher is not None:
            self.prefetcher.shutdown(wait=False)
        close = getattr(self.ostrovok, "close", None)
        if close:
            close()

    def _lock_for(self, session_id):
        with self._locks_guard:
            return self._locks[session_id]

    # ------------------------------------------------------------ turn
    def handle(self, session_id, request):
        request = dict(request or {})
        request.setdefault("session_id", session_id)
        session = self.store.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            return error_response(session_id, "session_not_found",
                                  "Сессия не найдена — начните новый разговор.", retryable=False)
        errors = sorted(jsonschema.Draft7Validator(REQUEST_SCHEMA).iter_errors(request), key=lambda e: e.path)
        if errors or request["session_id"] != session_id:
            return error_response(session_id, "bad_request", "Не понял запрос — попробуйте ещё раз.",
                                  turn=session.turn, stage=session.stage)
        with self._lock_for(session_id):
            session = self.store.get(session_id)
            return self._handle(session, request)

    def _handle(self, session, request):
        started = time.perf_counter()
        if self.clock is not None:
            session.now = self.clock()
        session.turn += 1
        ctx = Ctx(engine=self, session=session, request=request, text=request.get("text"),
                  image=request.get("image"), action=request.get("action"))
        ctx.profile = self.profiles.get(session.profile_id)
        ctx.old_totals = {v.id: v.totals.rub for v in session.variants}
        ctx.__dict__["sig_before"] = variant_signature(session.variants)
        ctx.trace["stage_path"].append(session.stage)
        ctx.trace["flags"] = self.flags.as_dict()
        try:
            self._run_turn(ctx)
        except StepError as exc:
            self._fail(ctx, exc.code, exc.message, exc.retryable)
        except ToolError as exc:
            code = "bad_request" if exc.code == "bad_request" else "tool_unavailable"
            self._fail(ctx, code, "Сервис бронирования сейчас не ответил, поэтому вариантов не будет — выдумывать их "
                                  "не хочу. Попробуйте ещё раз через минуту, ваш запрос я запомнил."
                       if code == "tool_unavailable" else "Не получилось выполнить запрос с такими данными.",
                       exc.retryable)
            ctx.trace["errors"].append(f"tool {exc.tool}: {exc.message}")
        except LLMError as exc:
            invalid = bool(exc.errors)
            self._fail(ctx, "invalid_model_output" if invalid else "model_unavailable",
                       "Не получилось разобрать сообщение — модель сейчас отвечает с ошибками. Повторите, "
                       "пожалуйста, чуть позже или нажмите на кнопку.", True)
            ctx.trace["errors"].append(f"llm {exc.card}: {exc}")
        ctx.trace["timings_ms"]["total"] = round((time.perf_counter() - started) * 1000, 1)
        ctx.trace["stage_path"].append(session.stage)
        response = build_response(ctx)
        self._finish(ctx, response)
        return response

    def _fail(self, ctx, code, message, retryable):
        ctx.error = {"code": code, "retryable": bool(retryable)}
        ctx.message = message
        ctx.questions, ctx.chips = [], []
        ctx.trace["errors"].append(f"{code}: {message}")

    def _run_turn(self, ctx):
        s = ctx.session
        merge_prefetched(ctx)
        route_kw = {}
        if ctx.action:
            self._map_action(ctx)
            route_kw = {"action": ctx.action["type"]}
        else:
            routed = None
            if self.flags.jev_routing and ctx.text and not ctx.image:
                routed = route_short_reply(self.jev, s, ctx.text, self.skill, ctx.trace["jev"])
            if routed is not None:
                ctx.action = routed
                ctx.trace["routed_by"] = "jev"
                self._map_action(ctx)
                route_kw = {"action": routed["type"]}
            else:
                self._understand(ctx)
                self._fix_intent(ctx)
                route_kw = {"intent": ctx.intent}
                self._apply_season(ctx)
                self._read_rebuild_wishes(ctx)
                self._read_location_cues(ctx)
        self._validate_patch(ctx)
        self._resolve_shift(ctx)
        if ctx.stop:
            return
        self._reconcile_dates(ctx)
        if ctx.image_info:
            run_image_match(ctx)
        tentative, _, _ = prepare_brief(ctx, s.brief, ctx.patch)
        if _dates_inverted(tentative):
            # last guard: never send check_out <= check_in to the mock (a bad_request for a sane refine)
            ctx.patch["set"].pop("dates.end", None)
            if "dates.end" not in ctx.patch["unset"]:
                ctx.patch["unset"].append("dates.end")
            ctx.trace["notes"].append("dates: end not after start, end recomputed from nights")
            tentative, _, _ = prepare_brief(ctx, s.brief, ctx.patch)
        if self._drop_contradicting_destination(ctx, tentative):
            tentative, _, _ = prepare_brief(ctx, s.brief, ctx.patch)
        route = self.skill.match_route(stage=s.stage, brief=tentative, **route_kw)
        if route is None:
            raise StepError("bad_request", "Не понял, что сделать — попробуйте сформулировать иначе.")
        ctx.route = route
        ctx.present = route.present
        ctx.trace["route"] = route.id or f"routes[{route.index}]"
        if route.present == "present.fallback":
            ctx.fallback_reason = ctx.intent if ctx.intent in ("smalltalk", "out_of_scope") else "unknown"
        for name in route.steps:
            if ctx.stop:
                break
            t0 = time.perf_counter()
            STEP_REGISTRY[name](ctx)
            ctx.trace["steps"].append(name)
            ctx.trace["timings_ms"][name] = round((time.perf_counter() - t0) * 1000, 1)
        if ctx.present == "present.update" and not ctx.stop:
            self._check_noop_rebuild(ctx)
        if ctx.present and not ctx.message and not ctx.stop:
            self._present(ctx)
        if ctx.present in STAGE_OF_CARD and not ctx.error and (ctx.variants or ctx.destination_items):
            s.stage = STAGE_OF_CARD[ctx.present]

    # ------------------------------------------------------------ 1. actions
    def _map_action(self, ctx):
        s = ctx.session
        kind, p = ctx.action["type"], ctx.action.get("payload") or {}
        sets, unsets = ctx.patch["set"], ctx.patch["unset"]

        def put(field, value):
            if value is None and field in ALLOWED_UNSET_PATHS:
                unsets.append(field)
            elif value is not None:
                sets[field] = value

        if kind == "answer_question":
            q = next((q for q in s.pending_questions if q.id == p["question_id"]), None)
            if q is None:
                spec = next((f for f, spec in (self.skill.questions or {}).items() if spec.get("id") == p["question_id"]),
                            None)
                if spec is None:
                    raise StepError("bad_request", "Этот вопрос уже неактуален — выберите из последних кнопок.")
                field = spec
            else:
                field = q.field
            put(field, p.get("value"))
        elif kind == "set_chip":
            put(p["field"], p.get("value"))
        elif kind == "choose_destination":
            item = s.find_shown("destination", p["destination_id"])
            ctx.destination_hint = p["destination_id"]
            sets["destination.name"] = item.title if item else p["destination_id"]
        elif kind in ("choose_variant", "add_to_cart"):
            ctx.variant_ids = [p["variant_id"]]
            ctx.extras = list(p.get("extras") or [])
        elif kind == "swap_hotel":
            ctx.swap = dict(p)
            ctx.variant_ids = [p["variant_id"]]
        elif kind == "compare":
            ctx.variant_ids = list(p["variant_ids"])
        elif kind == "checkout":
            ctx.checkout_payload = dict(p)
        elif kind == "pay":
            pass
        elif kind == "order_action":
            if s.order_id and p["order_id"] != s.order_id:
                raise StepError("bad_request", "Это не ваш текущий заказ.")
            ctx.post_purchase = {"kind": p["kind"], "params": dict(p.get("params") or {})}
        ctx.trace["notes"].append(f"action {kind}")

    # ------------------------------------------------------------ 2. understand
    def _understand(self, ctx):
        s = ctx.session
        payload = P.understand_input(s, ctx.text or "", bool(ctx.image))
        out = self._call_card(ctx, "understand", payload, images=[ctx.image] if ctx.image else [])
        ctx.trace["notes"].extend(normalize_understand(out, payload))
        ctx.understand = out
        ctx.intent = out.get("intent")
        patch = out.get("patch") or {}
        ctx.patch = {"set": dict(patch.get("set") or {}), "unset": list(patch.get("unset") or [])}
        ctx.refs = list(out.get("refs") or [])
        ctx.image_info = out.get("image") if ctx.image else None
        ctx.post_purchase = out.get("post_purchase")
        ctx.trace["understand"] = {"intent": ctx.intent, "patch": ctx.patch, "refs": ctx.refs}

    def _fix_intent(self, ctx):
        """route_intent (code rules on the understand intent by stage); a paid order is read from the mock."""
        s = ctx.session
        paid = False
        if ctx.intent == "post_purchase" and s.order_id:
            from .steps import tool_safe
            order = tool_safe(ctx, "order_get", {"order_id": s.order_id}, cache=False)
            # the mock is down: keep post_purchase, its step answers honestly
            paid = order is None or order.get("status") in ("paid", "changed")
        intent, patch, note = route_intent(ctx.intent, s.stage, ctx.text, ctx.patch, ctx.refs, paid,
                                           ctx.post_purchase)
        if note is None:
            return
        ctx.intent, ctx.patch = intent, patch
        if intent != "post_purchase":
            ctx.post_purchase = None
        ctx.trace["notes"].append(note)
        ctx.trace["understand"]["routed_intent"] = intent

    # ------------------------------------------------------------ an empty rebuild (s17)
    def _check_noop_rebuild(self, ctx):
        """A rebuild request that changed nothing (no brief path, the same variants): the update card says once,
        plainly, what cannot be guaranteed; the same request again right after gets a short code answer with
        the closest real option instead of the identical card (never «ничего не менял» twice in a row)."""
        s = ctx.session
        if ctx.action is not None or ctx.changed or ctx.shift_days or not ctx.variants:
            return
        sig = variant_signature(ctx.variants)
        if sig != ctx.__dict__.get("sig_before"):
            return
        prev = s.results.get(NOOP_REBUILD_KEY)
        prev = prev.data if prev is not None and isinstance(prev.data, dict) else {}
        repeat = prev.get("turn") == s.turn - 1 and prev.get("sig") == sig
        store_result(s, CachedResult(key=NOOP_REBUILD_KEY, kind="noop_rebuild", params={}, fetched_at=s.clock(),
                                     data={"turn": s.turn, "sig": sig}))
        ctx.trace["notes"].append("rebuild: nothing changed" + (" again -> code answer" if repeat else ""))
        if not repeat:
            return
        best = s.variant(card_hint(s.brief, ctx.variants)) or ctx.variants[0]
        P.record_shown(s, "variant", [(v.id, v.title or P.VARIANT_TITLES.get(v.id, v.id)) for v in ctx.variants])
        title = best.title or P.VARIANT_TITLES.get(best.id, best.id)
        tried = next((n.text for n in best.risks if n.code == "budget_fit"), None)
        if tried:   # the date / cheaper-hotel search ran and found nothing better: say what was tried
            ctx.message = (f"{tried}. Ближе всего — «{title}», {P.fmt_rub(best.totals.rub)} за всё. Можно взять его, "
                           f"поднять бюджет, сократить поездку на ночь или посмотреть другое направление.")
            return
        ctx.message = (f"Пересобрал ещё раз — подборка та же: вариантов, где это гарантировано, у меня нет. "
                       f"Ближе всего — «{title}», {P.fmt_rub(best.totals.rub)} за всё. Можно взять его или "
                       f"поменять даты, бюджет или район — напишите, что можно сдвинуть.")

    # ------------------------------------------------------------ 3. patch validation
    def _validate_patch(self, ctx):
        issues = self.hooks.run("after_understand", ctx, kinds=("validate",))
        bad = {(i.get("data") or {}).get("path") for i in blocking(issues)}
        if bad:
            ctx.patch["set"] = {k: v for k, v in ctx.patch["set"].items() if k not in bad}
            ctx.patch["unset"] = [k for k in ctx.patch["unset"] if k not in bad]
            ctx.trace["notes"].append(f"patch: dropped {sorted(p for p in bad if p)}")
            for i in blocking(issues):
                if (i.get("data") or {}).get("say"):
                    ctx.note(i["data"]["say"])
        answered = set(ctx.patch["set"]) | set(ctx.patch["unset"])
        if answered:
            ctx.session.pending_questions = [q for q in ctx.session.pending_questions if q.field not in answered]

    # ------------------------------------------------------------ shift_days (decision 6)
    def _resolve_shift(self, ctx):
        sets = ctx.patch["set"]
        if "dates.shift_days" not in sets:
            return
        k = sets["dates.shift_days"]
        s = ctx.session
        if ctx.intent == "post_purchase" and s.order_id and isinstance(k, int):
            # a move of the booked trip: shifted from the booked dates by post_purchase, never from the
            # variants on screen (after a rebook they are already the shifted ones — s16)
            sets.pop("dates.shift_days")
            pp = dict(ctx.post_purchase or {"kind": "change_dates"})
            pp["params"] = {**dict(pp.get("params") or {}), "shift_days": k}
            pp["kind"] = pp.get("kind") or "change_dates"
            ctx.post_purchase = pp
            ctx.trace["notes"].append(f"shift_days {k} -> post_purchase params (from the booked dates)")
            return
        shown = [v for v in (s.variant(i.id) for i in s.shown.variants) if v is not None] or list(s.variants)
        span = None
        for v in shown:
            stay = next((st for st in v.steps if st.type == "stay"), None)
            if stay is not None:
                span = (date.fromisoformat(stay.start[:10]), date.fromisoformat(stay.end[:10]))
                break
            if v.steps:
                span = (date.fromisoformat(v.steps[0].start[:10]), date.fromisoformat(v.steps[-1].end[:10]))
                break
        if span is not None and isinstance(k, int):
            sets.pop("dates.shift_days")
            ctx.shift_days = k
            sets["dates.start"] = (span[0] + timedelta(days=k)).isoformat()
            sets["dates.end"] = (span[1] + timedelta(days=k)).isoformat()
            ctx.trace["notes"].append(f"shift_days {k} resolved against shown dates {span[0]}..{span[1]}")
        elif not s.brief.has("dates.start"):
            ctx.stop = True
            ctx.present = None
            ctx.message = "От каких дат сдвинуть? Напишите даты поездки — например, «с 12 по 19 ноября»."

    # ------------------------------------------------------------ code guards on the patch (the model's slips)
    def _apply_season(self, ctx):
        """«летом», «осенью»...: the season's month by the code rule (season_month), shown as a chip."""
        if ctx.intent not in ("new_trip", "refine", "answer") or not ctx.text:
            return
        sets = ctx.patch["set"]
        if "dates.start" in sets or "dates.shift_days" in sets:
            return
        found = _season_of(ctx.text)
        month = season_month(ctx.text, ctx.session.today())
        if month is None:
            return
        given = sets.get("dates.month")
        if given is not None and given not in SEASON_MONTHS[found]:
            return                          # the model read another month on purpose; keep it
        sets["dates.month"] = month
        ctx.season = (found, month)
        ctx.trace["notes"].append(f"season {found} -> dates.month {month}")

    def _reconcile_dates(self, ctx):
        """A new start or a new number of nights without an end: the old end is stale, recompute it.

        (s04: a refine «в начале декабря на 4 ночи» kept the inferred end of the November dates shown
        before -> check_out before check_in -> bad_request.)
        """
        sets, unsets = ctx.patch["set"], ctx.patch["unset"]
        if "dates.end" in sets or "dates.end" in unsets:
            return
        brief = ctx.session.brief
        old_end = _iso_date((brief.values().get("dates") or {}).get("end"))
        if old_end is None:
            return
        drop = "dates.nights" in sets
        new_start = _iso_date(sets.get("dates.start"))
        if new_start is not None and (brief.source_of("dates.end") != "user" or new_start >= old_end):
            drop = True
        if drop:
            unsets.append("dates.end")
            ctx.trace["notes"].append("dates: stale end dropped (new start or nights)")

    # ------------------------------------------------------------ a change of trip kind / a rebuild wish
    def _read_rebuild_wishes(self, ctx):
        """«другие числа / даты», «подешевле / попроще» without exact values: build_variants then searches other
        start days and cheaper hotels (steps.fit_budget) instead of answering with the same variants."""
        if ctx.intent not in ("new_trip", "refine", "answer") or not ctx.text:
            return
        sets = ctx.patch["set"]
        if wants_other_dates(ctx.text) and not ({"dates.start", "dates.shift_days"} & set(sets)):
            ctx.ask_other_dates = True
            ctx.trace["notes"].append("wish: other dates without values -> date search")
        if wants_cheaper(ctx.text) and "budget_rub.max" not in sets:   # a new budget is the exact value
            ctx.ask_cheaper = True
            ctx.trace["notes"].append("wish: cheaper without values -> cheaper search")

    def _read_location_cues(self, ctx):
        """Code rules behind understand (rules.json `location`, `best_time`) on a trip request:
        - «в центре», «поближе к (историческому) центру», «в старом городе» → prefs.center_max_m (1500, «в самом
          центре» 800) when understand did not set it; a prefs.district "walkable" understand read from the same
          words is dropped unless the user also said «всё рядом / пешком» (s04: the centre wish was a no-op);
        - «когда лучше поехать», «в какой сезон» → ctx.best_time_asked: the card answers with the best months and,
          without user dates, the trip is assumed in the nearest best month (steps.resolve_dates)."""
        from .steps import best_time_cue, location_cue, walkable_cue
        if not ctx.text:
            return
        rules = self.skill.rules
        if best_time_cue(rules, ctx.text) or best_time_cue(rules, (ctx.understand or {}).get("info_question")):
            ctx.best_time_asked = True
            ctx.trace["notes"].append("best time asked")
        if ctx.intent not in ("new_trip", "refine", "answer"):
            return
        metres = location_cue(rules, ctx.text)
        if metres is None:
            return
        sets = ctx.patch["set"]
        if "prefs.center_max_m" not in sets:
            sets["prefs.center_max_m"] = metres
            ctx.trace["notes"].append(f"location cue -> prefs.center_max_m {metres}")
        if sets.get("prefs.district") == "walkable" and not walkable_cue(rules, ctx.text):
            sets.pop("prefs.district")
            ctx.trace["notes"].append("location cue: prefs.district walkable dropped (the centre is the wish)")

    def _drop_contradicting_destination(self, ctx, tentative):
        """The kind of trip changed this turn and the chosen destination no longer fits it: prefs.domestic set
        while the destination is abroad, prefs.transport = train set while no train goes there from the origin,
        or «другую страну / другой город» without naming one. The destination is unset (the patch and the trace
        show it) and the turn goes to the destinations route with the new prefs and the budget. A destination
        named in the same message is never dropped."""
        from .steps import _origin_place, _passes_dest_prefs, destination_record
        sets, unsets = ctx.patch["set"], ctx.patch["unset"]
        if ctx.destination_hint or "destination.name" in sets or "destination.name" in unsets:
            return False
        if ctx.action is None and ctx.intent not in ("new_trip", "refine", "answer"):
            return False
        if ctx.action is not None and ctx.action.get("type") not in ("set_chip", "answer_question"):
            return False
        dest = tentative.values().get("destination") or {}
        if not dest.get("id"):
            return False
        rec = destination_record(ctx, dest_id=dest["id"])
        if rec is None:
            return False
        prefs = tentative.values().get("prefs") or {}
        origin = _origin_place(ctx, tentative)
        why = None
        if sets.get("prefs.domestic") is True and prefs.get("domestic"):
            home = (origin or {}).get("country_code") or "RU"
            if rec.get("country_code") and rec["country_code"] != home:
                why = f"prefs.domestic: {dest['id']} is not in {home}"
        if why is None and sets.get("prefs.transport") == "train" and prefs.get("transport") == "train":
            if not _passes_dest_prefs(rec, origin, True, False):
                why = f"prefs.transport train: no train to {dest['id']} from {(origin or {}).get('id')}"
        if why is None and ctx.text:
            m = OTHER_PLACE_RE.search(ctx.text)
            if m:
                why = f"«{m.group(0)}» without a name"
                ctx.other_country = "стран" in m.group(0).casefold()
        if why is None:
            return False
        unsets.append("destination.name")
        ctx.dropped_destination = rec
        if ctx.action is None and ctx.intent == "answer":
            ctx.intent = "refine"
        ctx.trace["notes"].append(f"destination {dest['id']} dropped ({why}) -> destinations")
        if isinstance(ctx.trace.get("understand"), dict):
            ctx.trace["understand"]["patch"] = ctx.patch
        return True

    # ------------------------------------------------------------ model calls
    def _call_card(self, ctx, card, payload, images=(), rerun_issues=None):
        spec = self.skill.card(card)
        budget = ctx.trace["budget"]
        limit = int(self.skill.rules.get("max_flash_calls_per_turn", 2))
        reruns = budget.setdefault("reruns", {})
        if rerun_issues is not None:
            max_rerun = int((self.skill.rules.get("retries") or {}).get("card_rerun_on_hook_fail", 1))
            if reruns.get(card, 0) >= max_rerun:
                raise BudgetExceeded(f"rerun budget exceeded for {card}")
            reruns[card] = reruns.get(card, 0) + 1
        else:
            if ctx.flash_calls >= limit:
                raise BudgetExceeded(f"model call budget exceeded ({limit}) at {card}")
            ctx.flash_calls += 1
        budget.update(flash_calls=ctx.flash_calls, limit=limit)
        system = rerun_system(self.skill.system_prompt(card), rerun_issues)
        req = LLMRequest(card=card, system=system, user=json.dumps(payload, ensure_ascii=False, default=str),
                         schema=spec.schema, images=list(images), thinking=spec.thinking, max_tokens=spec.max_tokens,
                         temperature=getattr(spec, "temperature", None))
        record = {"card": card, "rerun": rerun_issues is not None}
        t0 = time.perf_counter()
        try:
            res = self.backend.complete(req)
        except LLMError as exc:
            record.update(ok=False, error=str(exc), retries=exc.retries,
                          latency_ms=exc.latency_ms or round((time.perf_counter() - t0) * 1000),
                          tokens=exc.usage, cost=(exc.usage or {}).get("cost_usd"))
            ctx.trace["calls"].append(record)
            raise
        record.update(ok=True, model=res.model, provider=res.provider, tokens=res.usage, latency_ms=res.latency_ms,
                      cost=res.usage.get("cost_usd"), retries=res.retries)
        ctx.trace["calls"].append(record)
        if card.startswith("present."):
            ctx.trace["notes"].extend(repair_output_ids(res.data, payload))
        return res.data

    # ------------------------------------------------------------ 7. present
    def _present(self, ctx):
        card = ctx.present
        ctx.chips = self._chips(ctx, card)
        payload = self._card_input(ctx, card)
        ctx.card_input = payload
        self.hooks.run("before_present", ctx)
        try:
            out = self._call_card(ctx, card, payload)
        except LLMError:
            ctx.degraded = True
            ctx.trace["notes"].append(f"{card}: model failed, code texts used")
            return
        issues = self._guard(ctx, card, payload, out)
        if blocking(issues):
            try:
                out2 = self._call_card(ctx, card, payload, rerun_issues=blocking(issues))
                issues2 = self._guard(ctx, card, payload, out2)
            except LLMError:
                out2, issues2 = None, [issue("llm_error", "rerun failed")]
            if out2 is not None and not blocking(issues2):
                out = out2
            else:
                ctx.degraded = True
                ctx.trace["notes"].append(f"{card}: guard failed twice, code texts used")
                out = None
        ctx.card_output = out

    def _guard(self, ctx, card, payload, out):
        issues = basic_card_guard(card, payload, out)
        check = self.domain.get("check_card_output", required=False)
        if check is not None:
            issues += [as_issue(i) for i in check(card, payload, out, self.skill.rules) or []]
        if self.flags.jev_guard:
            issues += guard_card_output(self.jev, card, payload, out, self.skill, ctx.trace["jev"])
        verdict = "block" if blocking(issues) else ("warn" if issues else "ok")
        ctx.trace["hooks"].append({"event": "after_present", "name": "card_guard", "kind": "validate",
                                   "verdict": verdict, "issues": issues, "card": card})
        return issues

    def _chips(self, ctx, card):
        if card not in QUESTION_CARDS:
            return []
        asked = {q.field for q in ctx.questions}
        chips = P.build_chips(self.skill, ctx.session.brief, exclude_fields=asked) if self.flags.sensitivity else []
        season = getattr(ctx, "season", None)
        if season is not None and "dates.month" not in asked:
            chips = [c for c in chips if c.field != "dates.month"]
            chips.insert(0, season_chip(*season))
        scope = P.scope_chip(self.skill, ctx.session.brief)
        if scope is not None and all(c.field != "scope" for c in chips):
            chips.append(scope)
        return chips

    def _card_input(self, ctx, card):
        s = ctx.session
        if card == "present.destinations":
            items = [{k: v for k, v in i.items() if k != "image_url"} for i in ctx.destination_items]
            return P.destinations_input(s, items, ctx.questions, ctx.image_info, ctx.insight, ctx.chips)
        if card in ("present.variants", "present.update"):
            labels = P.record_shown(s, "variant", [(v.id, v.title or P.VARIANT_TITLES.get(v.id, v.id))
                                                   for v in ctx.variants])
            hint, items = self._items(ctx, labels)
            cap = int(self.skill.rules.get("max_upsell", 2)) * 2
            changes = rebuilt = None
            if card == "present.update":
                new_totals = {v.id: v.totals.rub for v in ctx.variants}
                totals = [total_change(vid, ctx.old_totals.get(vid), new_totals.get(vid))
                          for vid in list(ctx.old_totals) + [i for i in new_totals if i not in ctx.old_totals]
                          if ctx.old_totals.get(vid) != new_totals.get(vid)]
                changes, rebuilt = ctx.changes + totals, ctx.rebuilt
            from .steps import search_question
            return P.variants_input(s, items, hint, ctx.questions, ctx.upsell[:cap],
                                    ctx.chips, changes, rebuilt, request=ctx.text if ctx.action is None else "",
                                    question=search_question(ctx))
        if card == "present.compare":
            labels = {i.id: i.label for i in s.shown.variants}
            labels = {v.id: labels.get(v.id, str(n + 1)) for n, v in enumerate(ctx.variants)}
            _, items = self._items(ctx, labels)
            return P.compare_input(s, items, P.compare_dimensions(s.brief), ctx.chips)
        if card == "present.info":
            return P.info_input(s, ctx.info_question, ctx.info_facts, ctx.chips)
        if card == "present.aftercare":
            from .steps import order_summary
            return P.aftercare_input(s, order_summary(ctx.order) if ctx.order else None, ctx.aftercare_result,
                                     ctx.chips)
        return P.fallback_input(s, ctx.text or "", ctx.fallback_reason, ctx.chips)

    def _items(self, ctx, labels):
        """(card hint, variant items) with the ready price gaps: diff_rub from the card's recommended item,
        over_budget_rub above brief.budget_rub.max."""
        hint = card_hint(ctx.session.brief, ctx.variants)
        base = next((v.totals.rub for v in ctx.variants if v.id == hint), None)
        budget = budget_max(ctx.session.brief)
        return hint, [P.variant_item(v, labels[v.id], self._district_counts(ctx, v), base_rub=base, budget_max=budget)
                      for v in ctx.variants]

    def _district_counts(self, ctx, variant):
        if variant.hotel is None:
            return {}
        from .steps import as_dict, find_offer
        offer = find_offer(ctx, variant.hotel.offer_id) or {}
        prof = as_dict(ctx.districts.get(offer.get("osm_id"))) if offer.get("osm_id") else None
        counts = (prof or {}).get("poi_counts") or (prof or {}).get("counts") or {}
        if isinstance(counts, str):
            try:
                counts = json.loads(counts)
            except json.JSONDecodeError:
                counts = {}
        return counts

    # ------------------------------------------------------------ 8. finish
    def _finish(self, ctx, response):
        s = ctx.session
        if ctx.text:
            s.remember_user(ctx.text)
        elif ctx.image and not ctx.action:
            s.remember_user("[фото]")
        if response["message"]:
            s.remember_agent(response["message"])
        if not ctx.error and (ctx.present in QUESTION_CARDS or ctx.questions):
            s.pending_questions = list(ctx.questions)
        if any(ctx.files.values()) and s.order_id:
            store_result(s, CachedResult(key=f"files:{s.order_id}", kind="files", params={},
                                         fetched_at=s.clock(), data=dict(ctx.files)))
        self.hooks.run("turn_end", ctx, kinds=("analytics",))
        if self.flags.prefetch and not ctx.error:
            self.hooks.run("after_present", ctx, kinds=("prefetch",))
        t = ctx.trace
        s.add_trace({"turn": s.turn, "route": t["route"], "stage_path": t["stage_path"], "steps": t["steps"],
                     "calls": t["calls"], "budget": t["budget"], "hooks": t["hooks"], "jev": t["jev"],
                     "tools": t["tools"], "timings_ms": t["timings_ms"], "notes": t["notes"], "errors": t["errors"],
                     "degraded": ctx.degraded, "error": ctx.error})
        self.store.put(s)


# ---------------------------------------------------------------- default hooks and guards

def budget_max(brief):
    return ((brief.values().get("budget_rub") or {}).get("max")) or None


def card_hint(brief, variants):
    """The variant a card recommends: the fit hint, budget-consistent (variants.md rule 4)."""
    return P.card_hint(variants, recommended_hint(variants), budget_max(brief))


def total_change(variant_id, before, after):
    """A `changes` row of present.update for one variant's total, with the ready gap (the card never
    subtracts — s25 «8 510», «11 830»): delta_rub = |after - before|, delta_word «дешевле» / «дороже» / None
    (None also when the variant is new or gone)."""
    row = {"field": f"totals.{variant_id}.rub", "before": before, "after": after, "delta_rub": None,
           "delta_word": None}
    if before is not None and after is not None:
        diff = int(after) - int(before)
        row.update(delta_rub=abs(diff), delta_word="дешевле" if diff < 0 else "дороже" if diff > 0 else None)
    return row


# «пересоберите», «соберите заново», «другой отель / номер»: a rebuild request, whatever intent understand saw
REBUILD_RE = re.compile(r"пересоб[её]р|пересобра|пересборк|(?:собер|подбер)\w*\s+(?:заново|по-другому|друг)|"
                        r"(?:друг|ин)(?:ой|ую|ие|ое|ого)\s+(?:отел|номер|вариант|гостиниц|жил)", re.I)
PRE_PAY_STAGES = ("selection", "cart", "checkout")


def is_rebuild_request(text):
    return bool(REBUILD_RE.search(text or ""))


# «другие числа / даты», «сдвиньте даты», «перенесите поездку» — without exact values the date search runs
OTHER_DATES_RE = re.compile(r"(?:друг|ин)(?:ие|ую|ой|ое|ими|их|ая)\s+(?:числ|дат|дн[иеяй]|недел)|"
                            r"сдвин\w*\s+(?:дат|поездк|на\s+друг)|перенес\w*\s+(?:дат|поездк)|"
                            r"(?:поменя|смен|измен)\w*\s+дат", re.I)
# «подешевле», «попроще», «что-нибудь дешевле» (not the variant title «Дешевле» alone)
CHEAPER_RE = re.compile(r"подешевл|попроще|поскромнее|(?:что-нибудь|что-то|ещё|еще|вариант\w*|отел\w*)\s+дешевле|"
                        r"дешевле\s+(?:есть|найд|можно|бы|вариант|отел)", re.I)
# «другую страну / другой город / другое направление» without naming one
OTHER_PLACE_RE = re.compile(r"(?:друг|ин)(?:ая|ую|ой|ое|ие|ого)\s+(?:стран|город|направлен|мест[оа]|курорт)", re.I)


def wants_other_dates(text):
    return bool(OTHER_DATES_RE.search(text or ""))


def wants_cheaper(text):
    return bool(CHEAPER_RE.search(text or ""))


def route_intent(intent, stage, text, patch, refs=(), paid=False, post_purchase=None):
    """Code fixes of the understand intent by stage, before routing -> (intent, patch, note | None).

    - post_purchase without a paid order is never «сначала оплатим»: a change of the trip is a refine
      (dates from its params go to the patch); a bare question about the stay is info;
    - in selection / cart / checkout a rebuild request («пересоберите…») or a new condition (a non-empty patch
      without a variant ref) is a refine that rebuilds the variants, even when understand said
      choose / cart / checkout / info (s02, s04, s17).
    """
    sets = dict((patch or {}).get("set") or {})
    unsets = list((patch or {}).get("unset") or [])
    rebuild = is_rebuild_request(text) or wants_other_dates(text) or wants_cheaper(text)
    if intent == "post_purchase" and not paid:
        pp = post_purchase or {}
        params = dict(pp.get("params") or {})
        if pp.get("kind") == "change_dates":
            nd = params.get("new_dates") if isinstance(params.get("new_dates"), dict) else params
            start, end = nd.get("check_in") or nd.get("start"), nd.get("check_out") or nd.get("end")
            if start:
                sets.setdefault("dates.start", start)
                if end:
                    sets.setdefault("dates.end", end)
            elif isinstance(params.get("shift_days"), int):
                sets.setdefault("dates.shift_days", params["shift_days"])
        new = "refine" if (sets or unsets or rebuild or pp.get("kind") == "change_dates") else "info"
        return new, {"set": sets, "unset": unsets}, f"intent post_purchase without a paid order -> {new}"
    if stage in PRE_PAY_STAGES and intent in ("choose", "cart", "checkout", "info"):
        variant_ref = any((r or {}).get("kind") == "variant" for r in refs or [])
        # «есть что-то ближе к Айя-Софии?»: a question that carries the centre pref is a rebuild (s04 t6)
        centre = intent == "info" and "prefs.center_max_m" in sets
        if rebuild or centre or ((sets or unsets) and not variant_ref and intent in ("choose", "cart")):
            return "refine", {"set": sets, "unset": unsets}, f"intent {intent} with a rebuild request -> refine"
    return intent, {"set": sets, "unset": unsets}, None


def variant_signature(variants):
    """What the user sees of a variant set: ids, totals and the offers behind every step."""
    return [[v.id, int(v.totals.rub), [s.offer_id for s in v.steps]] for v in variants]


NOOP_REBUILD_KEY = "engine:noop_rebuild"


def validate_patch_hook(ctx):
    """after_understand: every patch path must apply on its own; dates must be sane (not past, end > start)."""
    out = []
    brief = ctx.session.brief
    sets, unsets = ctx.patch["set"], ctx.patch["unset"]
    for path, value in list(sets.items()):
        if path not in ALLOWED_PATCH_PATHS:
            out.append(issue("patch_path", f"path not allowed: {path}", path=path))
            continue
        try:
            apply_patch(brief, {"set": {path: value}})
        except PatchError as exc:
            out.append(issue("patch_value", str(exc), path=path))
    for path in unsets:
        if path not in ALLOWED_UNSET_PATHS:
            out.append(issue("patch_unset", f"cannot unset {path}", path=path))
    origin, dest = sets.get("origin.city"), sets.get("destination.name")
    if dest is None and brief.destination.value is not None:
        dest = brief.destination.value.name
    if isinstance(origin, str) and isinstance(dest, str) and origin.strip().casefold() == dest.strip().casefold():
        # «в Питер» read as «из Питера» too: the destination is never the origin
        out.append(issue("origin_is_destination", f"origin equals destination {dest}", path="origin.city"))
    today = ctx.session.today()
    start = sets.get("dates.start")
    if isinstance(start, str):
        try:
            if date.fromisoformat(start) < today:
                out.append(issue("date_past", f"start {start} is in the past", path="dates.start",
                                 say="Эта дата уже прошла — взял ближайшие подходящие даты."))
        except ValueError:
            pass
    end = sets.get("dates.end")
    if isinstance(start, str) and isinstance(end, str):
        try:
            if date.fromisoformat(end) <= date.fromisoformat(start):
                out.append(issue("date_order", "end is not after start", path="dates.end"))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- seasons and wishes (code rules behind understand)

SEASON_MONTHS = {"summer": (6, 7, 8), "autumn": (9, 10, 11), "winter": (12, 1, 2), "spring": (3, 4, 5)}
_SEASON_RE = {
    "summer": re.compile(r"(?<![а-яё])(летом|лето|летн)", re.I),
    "autumn": re.compile(r"(?<![а-яё])(осенью|осень|осенн)", re.I),
    "winter": re.compile(r"(?<![а-яё])(зимой|зима|зиму|зимн)", re.I),
    "spring": re.compile(r"(?<![а-яё])(весной|весна|весну|весенн)", re.I),
}
# an explicit month or a numeric date beats the season («летом, в августе» -> August)
_MONTH_WORD_RE = re.compile(r"(?<![а-яё])(январ|феврал|март|апрел|ма[йяе](?![а-яё]*к)|майск|июн|июл|август|сентябр|"
                            r"октябр|ноябр|декабр|новогодн)|\d{1,2}[./]\d{1,2}", re.I)


_MONTH_STEMS = ("январ", "феврал", "март", "апрел", "ма[йяе](?![а-яё])", "июн", "июл", "август", "сентябр",
                "октябр", "ноябр", "декабр")
# a day was said: «5 января», «5–12 января», «05.01», «с 5-го», «числа», «в начале / середине / конце», holidays
_DAY_HINT_RE = re.compile(r"\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s*(?:январ|феврал|март|апрел|ма[йяе]|июн|июл|август|сентябр|"
                          r"октябр|ноябр|декабр)|\d{1,2}[./]\d{1,2}|\d{1,2}\s*-?\s*(?:го|е)(?![а-яё])|числ|"
                          r"начал|середин|конц|кон[её]ц|выходн|праздник|новогод", re.I)


def repair_output_ids(out, payload):
    """Ids in a present card's reply that miss exactly one input id by a typo («ras-al-khaima» for
    «ras-al-khaimah») are repaired by code before the guard; changes `out` in place, returns trace notes."""
    import difflib
    if not isinstance(out, dict) or not isinstance(payload, dict):
        return []
    item_ids = [i["id"] for i in payload.get("items") or [] if isinstance(i, dict) and isinstance(i.get("id"), str)]
    cand_ids = [c["candidate_id"] for c in payload.get("upsell_candidates") or []
                if isinstance(c, dict) and isinstance(c.get("candidate_id"), str)]
    notes = []

    def fix(value, ids):
        if not isinstance(value, str) or not ids or value in ids:
            return value
        close = difflib.get_close_matches(value, ids, n=2, cutoff=0.85)
        if len(close) == 1:
            notes.append(f"id {value} -> {close[0]} (typo repaired)")
            return close[0]
        return value

    for item in out.get("items") or []:
        if isinstance(item, dict) and "id" in item:
            item["id"] = fix(item["id"], item_ids)
    if out.get("recommended_id") is not None:
        out["recommended_id"] = fix(out["recommended_id"], item_ids)
    for u in out.get("upsell") or []:
        if isinstance(u, dict) and "candidate_id" in u:
            u["candidate_id"] = fix(u["candidate_id"], cand_ids)
    return notes


def normalize_understand(out, payload):
    """Deterministic code-side fixes of an understand reply, shared by the engine and the level-2 evals
    (changes `out` in place; returns trace notes):
    - a month named without a day is dates.month, not the 1st (month_only_start);
    - a ref whose kind is not on screen under that label takes the only kind shown with that label
      («второй вариант» while destinations are shown is destination 2)."""
    notes = []
    if not isinstance(out, dict):
        return notes
    patch = out.get("patch") if isinstance(out.get("patch"), dict) else {}
    sets = patch.get("set")
    if isinstance(sets, dict) and out.get("intent") in ("new_trip", "refine", "answer") \
            and month_only_start(sets, payload.get("message") or ""):
        notes.append("dates: a month without a day -> dates.month, not the 1st")
    kinds_by_label = {}
    for item in payload.get("shown") or []:
        kinds_by_label.setdefault(str(item.get("label")), set()).add(item.get("kind"))
    for ref in out.get("refs") or []:
        if not isinstance(ref, dict):
            continue
        kinds = kinds_by_label.get(str(ref.get("ref")))
        if kinds and ref.get("kind") not in kinds and len(kinds) == 1:
            new_kind = next(iter(kinds))
            notes.append(f"refs: {ref.get('kind')}:{ref.get('ref')} -> {new_kind} (the kind on screen)")
            ref["kind"] = new_kind
    return notes


def month_only_start(sets, text):
    """«в январе на 10 ночей» read as dates.start = the 1st: no day was said, so it is dates.month.
    Changes `sets` in place; True when it did."""
    start = _iso_date(sets.get("dates.start"))
    if start is None or start.day != 1 or _DAY_HINT_RE.search(text or ""):
        return False
    if not re.search(r"(?<![а-яё])" + _MONTH_STEMS[start.month - 1], text or "", re.I):
        return False
    end = _iso_date(sets.pop("dates.end", None))
    sets.pop("dates.start")
    if end is not None and end > start and "dates.nights" not in sets:
        sets["dates.nights"] = (end - start).days
    sets["dates.month"] = start.month
    return True


def _season_of(text):
    found = [s for s, rx in _SEASON_RE.items() if rx.search(text or "")]
    return found[0] if len(found) == 1 else None


def season_month(text, today):
    """The month a season word stands for (documented in understand.md), or None.

    летом -> 7; весной -> 4; зимой -> 12 (1 when today is already in December-February);
    осенью -> the first autumn month still ahead (a month counts while today's day < 20, like the
    engine's date resolution), else September of the next autumn.
    An explicit month name or a numeric date in the text wins: None.
    """
    season = _season_of(text)
    if season is None or _MONTH_WORD_RE.search(text or ""):
        return None
    if season == "summer":
        return 7
    if season == "spring":
        return 4
    if season == "winter":
        return 1 if today.month in (12, 1, 2) else 12
    for m in (9, 10, 11):
        if m > today.month or (m == today.month and today.day < 20):
            return m
    return 9


def season_chip(season, month):
    from .state import Chip, ChipOption
    return Chip(field="dates.month", label=f"в {P.MONTHS_PREP[month - 1]}", value=month,
                options=[ChipOption(label=f"в {P.MONTHS_PREP[m - 1]}", value=m) for m in SEASON_MONTHS[season]])


def _iso_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _dates_inverted(brief):
    d = brief.values().get("dates") or {}
    start, end = _iso_date(d.get("start")), _iso_date(d.get("end"))
    return start is not None and end is not None and end <= start


def rerun_system(system, rerun_issues):
    """The card prompt for a rerun: the rejection reasons appended (the card input stays exactly the same)."""
    if not rerun_issues:
        return system
    return (system + "\n\nYour previous reply was rejected by the checks: "
            + "; ".join(f"{i['code']}: {i['message']}" for i in rerun_issues)
            + ". Write it again from the same input and fix exactly these problems.")


def card_guard_issues(card, payload, out, rules):
    """What the engine checks on a present card before the user sees it (without the optional Jev guard)."""
    from .guards import check_card_output
    return basic_card_guard(card, payload, out) + [as_issue(i) for i in check_card_output(card, payload, out, rules) or []]


def basic_card_guard(card, payload, out):
    """Deterministic minimum (always on): ids and upsell candidates must come from the input."""
    issues = []
    ids = {i["id"] for i in payload.get("items") or [] if isinstance(i, dict) and "id" in i}
    for item in out.get("items") or []:
        if ids and item.get("id") not in ids:
            issues.append(issue("unknown_id", f"item id {item.get('id')} is not in the input"))
    rec = out.get("recommended_id")
    if rec is not None and ids and rec not in ids:
        issues.append(issue("unknown_recommended", f"recommended_id {rec} is not in the input"))
    cands = {c["candidate_id"] for c in payload.get("upsell_candidates") or []}
    for u in out.get("upsell") or []:
        if u.get("candidate_id") not in cands:
            issues.append(issue("unknown_upsell", f"upsell {u.get('candidate_id')} is not an input candidate"))
    return issues


def prefetch_hook(ctx):
    """after_present (flag prefetch): search for the leading destination; hotel details for the leader."""
    engine, s = ctx.engine, ctx.session
    pf = engine.prefetcher
    if pf is None:
        return
    if ctx.present == "present.destinations" and ctx.destination_items:
        rec_id = (ctx.card_output or {}).get("recommended_id")
        ids = [i["id"] for i in ctx.destination_items]
        leader = rec_id if rec_id in ids else ids[0]
        snapshot = s.model_copy(deep=True)
        pf.submit(s.id, f"search:{leader}:{s.turn}", lambda: _prefetch_search(engine, snapshot, ctx.profile, leader))
    elif ctx.present in ("present.variants", "present.update") and ctx.variants:
        rec_id = (ctx.card_output or {}).get("recommended_id")
        leader = s.variant(rec_id) if rec_id else None
        leader = leader or s.variant(recommended_hint(ctx.variants))
        if leader is not None and leader.hotel is not None:
            snapshot = s.model_copy(deep=True)
            pf.submit(s.id, f"hotel:{leader.hotel.hotel_id}",
                      lambda: _prefetch_tools(engine, snapshot, [("get_hotel", {"hotel_id": leader.hotel.hotel_id})]))


def _shadow_ctx(engine, session, profile):
    shadow = Ctx(engine=engine, session=session, request={})
    shadow.profile = profile
    return shadow


def _new_records(before_keys, session):
    return [r for k, r in session.results.items() if k not in before_keys]


def _prefetch_search(engine, snapshot, profile, destination_id):
    shadow = _shadow_ctx(engine, snapshot, profile)
    before = set(snapshot.results)
    rec = destination_record(shadow, dest_id=destination_id)
    if rec is None:
        return []
    shadow.destination_hint = destination_id
    brief, _, _ = prepare_brief(shadow, snapshot.brief, {"set": {"destination.name": rec["name"]}}, allow_lazy=False)
    snapshot.brief = brief
    check_in, check_out = resolve_dates(shadow, rec)
    run_search(shadow, snapshot.brief, rec, check_in, check_out, track=False)
    return _new_records(before, snapshot)


def _prefetch_tools(engine, snapshot, calls):
    from .steps import tool
    shadow = _shadow_ctx(engine, snapshot, None)
    before = set(snapshot.results)
    for name, payload in calls:
        try:
            tool(shadow, name, payload)
        except ToolError:
            pass
    return _new_records(before, snapshot)
