"""web_debug/index.html: self-contained and wired to every contract action type."""
import json
import re

from agent.state import ACTION_TYPES

from .conftest import ROOT

HTML = (ROOT / "web_debug" / "index.html").read_text(encoding="utf-8")


def contract_action_types():
    schema = json.loads((ROOT / "agent/contracts/request.schema.json").read_text(encoding="utf-8"))
    refs = [r["$ref"].rsplit("/", 1)[1] for r in schema["definitions"]["action"]["oneOf"]]
    return [schema["definitions"][r]["properties"]["type"]["const"] for r in refs]


def test_no_external_urls_or_resources():
    assert not re.search(r"https?://", HTML, re.I)
    assert not re.search(r"""(src|href)\s*=\s*["']//""", HTML, re.I)
    assert not re.search(r"<link\b", HTML, re.I)
    assert not re.search(r"<script\b[^>]*\bsrc=", HTML, re.I)
    assert not re.search(r"@import|\burl\(", HTML)   # CSS url(...) (JS `URL(` is fine)


def test_every_action_type_has_a_sender():
    types = contract_action_types()
    assert set(types) == set(ACTION_TYPES)
    for kind in types:
        assert re.search(r"""sendAction\(\s*["']%s["']""" % kind, HTML), f"no sendAction('{kind}', …) in the mockup"
        assert re.search(r"""case\s+["']%s["']""" % kind, HTML), f"Response.actions of type {kind} not handled"


def test_mockup_covers_the_response_parts():
    for marker in ("renderQuestion", "renderChip", "renderDestination", "renderVariant", "renderComparison",
                   "renderCart", "renderOrder", "checkoutForm", "renderFiles", "renderDebug", "readAsDataURL",
                   "prefers-color-scheme", "raw-toggle", "/api/session", "/api/turn", "/api/fixtures"):
        assert marker in HTML, marker


def test_script_has_balanced_braces():
    script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    stripped = re.sub(r"`(?:\\.|[^`\\])*`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|/\*.*?\*/", "", script, flags=re.S)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    for open_, close in ("()", "[]", "{}"):
        assert stripped.count(open_) == stripped.count(close), open_
