"""Offer ids (foundation decision 5): deterministic and self-describing.

Format: `<prefix>1.<base64url(compact JSON of the generator params)>.<checksum>` where
prefix is `fl` (flight), `rw` (train), `ho` (hotel), `tf` (transfer) and the checksum is the
first 12 hex chars of HMAC-SHA256(key, prefix + payload). The key is env OSTROVOK_OFFER_KEY
(default: a fixed mock key) — it only guards against tampering/typos, it is not a secret.
`cart_add` rebuilds the offer from the decoded params alone; a changed byte is rejected.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from .db import ServiceError

PREFIX = {"flight": "fl", "train": "rw", "hotel": "ho", "transfer": "tf"}
KIND_BY_PREFIX = {v: k for k, v in PREFIX.items()}
VERSION = "1"


def _key() -> bytes:
    return os.environ.get("OSTROVOK_OFFER_KEY", "ostrovok-mock-offer-key-v1").encode("utf-8")


def _checksum(prefix: str, payload: str) -> str:
    return hmac.new(_key(), (prefix + "." + payload).encode("ascii"), hashlib.sha256).hexdigest()[:12]


def encode(kind: str, params: dict[str, Any]) -> str:
    prefix = PREFIX[kind] + VERSION
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{prefix}.{payload}.{_checksum(prefix, payload)}"


def decode(offer_id: str, expect_kind: str | None = None) -> tuple[str, dict[str, Any]]:
    """Return (kind, params) or raise ServiceError('invalid_offer_id')."""
    parts = offer_id.split(".") if isinstance(offer_id, str) else []
    if len(parts) != 3:
        raise ServiceError("invalid_offer_id", f"malformed offer id: {offer_id!r}")
    prefix, payload, check = parts
    kind = KIND_BY_PREFIX.get(prefix[:-1]) if prefix.endswith(VERSION) else None
    if kind is None:
        raise ServiceError("invalid_offer_id", f"unknown offer id prefix: {prefix!r}")
    if not hmac.compare_digest(check, _checksum(prefix, payload)):
        raise ServiceError("invalid_offer_id", "offer id checksum mismatch (tampered or corrupted)")
    try:
        params = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ServiceError("invalid_offer_id", "offer id payload is not valid JSON") from None
    if not isinstance(params, dict):
        raise ServiceError("invalid_offer_id", "offer id payload is not an object")
    if expect_kind is not None and kind != expect_kind:
        raise ServiceError("offer_kind_mismatch", f"offer id is a {kind} offer, not {expect_kind}")
    return kind, params
