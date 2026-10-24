"""Tiny .env loader (no python-dotenv). Values are never printed or logged.

    load_env(".env")        # sets missing os.environ entries, returns the key names it set
    get_openrouter_key()    # str or None
"""
import os
from pathlib import Path

OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"


def _parse_value(raw):
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        while quote == '"' and end > 0 and raw[end - 1] == "\\":
            end = raw.find(quote, end + 1)
        if end > 0:  # anything after the closing quote (spaces, # comment) is ignored
            value = raw[1:end]
            if quote == '"':
                value = value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            return value
    # unquoted: an inline comment starts at " #"
    cut = raw.find(" #")
    if cut >= 0:
        raw = raw[:cut]
    return raw.strip()


def parse_env_text(text):
    """KEY=VALUE lines -> dict. Blank lines, comments and malformed lines are skipped."""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not key or not (key.replace("_", "").isalnum()) or key[0].isdigit():
            continue
        result[key] = _parse_value(raw)
    return result


def load_env(path=".env", environ=None):
    """Load KEY=VALUE pairs from `path` into `environ` (default os.environ).

    Existing variables are never overridden; a missing file is not an error.
    Returns the sorted list of key NAMES that were set (never values).
    """
    environ = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return []
    try:
        text = file.read_text(encoding="utf-8")
    except OSError:
        return []
    added = []
    for key, value in parse_env_text(text).items():
        if key not in environ:
            environ[key] = value
            added.append(key)
    return sorted(added)


def get_openrouter_key(environ=None):
    """The OpenRouter key from the environment, or None when absent or empty.

    Does not read .env by itself: entry points call load_env() once at startup.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(OPENROUTER_KEY_NAME, "").strip()
    return value or None
