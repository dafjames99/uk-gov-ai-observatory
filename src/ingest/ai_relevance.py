"""Load the AI-relevance config and classify procurement notices."""

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parents[2] / "config" / "ai_relevance.yaml"


@lru_cache(maxsize=1)
def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_config(path: Path | None = None) -> dict:
    """Return the AI-relevance config, cached after first load.

    Args:
        path: Override path to the YAML config file.

    Returns:
        Parsed config dict.
    """
    return _load_config(str(path or _CONFIG_PATH))


def config_version(path: Path | None = None) -> str:
    """Return the version string from the AI-relevance config.

    Args:
        path: Override path to the YAML config file.

    Returns:
        Version string, e.g. "1.0".
    """
    return load_config(path).get("version", "unknown")


@lru_cache(maxsize=8)
def _compile_keywords(keywords: tuple[str, ...]) -> re.Pattern | None:
    """Compile keywords into one word-boundary alternation regex.

    Word boundaries prevent short tokens from matching inside other words
    (e.g. 'ai' in 'maintain', 'llm' in 'fulfillment'), which plain substring
    matching would wrongly flag.
    """
    if not keywords:
        return None
    alternation = "|".join(re.escape(k) for k in keywords)
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


def _any_keyword(text: str, keywords: list[str]) -> bool:
    pattern = _compile_keywords(tuple(keywords))
    return bool(pattern and pattern.search(text))


def _any_cpv(cpv_codes: list[str] | None, prefixes: list[str]) -> bool:
    if not cpv_codes:
        return False
    return any(str(code).startswith(p) for code in cpv_codes for p in prefixes)


def classify(
    title: str | None,
    description: str | None,
    cpv_codes: list[str] | None,
    config: dict | None = None,
) -> str:
    """Score a notice's AI relevance as 'strong', 'weak', or 'none'.

    Scoring (see config/ai_relevance.yaml):
        strong : any strong_keyword OR (any weak_keyword AND any strong_cpv)
        weak   : any weak_keyword OR any strong_cpv OR any weak_cpv
        none   : otherwise

    Keyword matching is case-insensitive over (title + description). CPV is
    never sufficient alone for 'strong' — there is no dedicated AI code in the
    CPV vocabulary, so it only elevates a weak keyword.

    Args:
        title: Notice title field.
        description: Notice description field.
        cpv_codes: List of CPV code strings from the notice.
        config: Override config dict (uses module default if None).

    Returns:
        One of "strong", "weak", "none".
    """
    cfg = config or load_config()
    text = " ".join(filter(None, [title, description])).lower()

    strong_kw = _any_keyword(text, cfg.get("strong_keywords", []))
    weak_kw = _any_keyword(text, cfg.get("weak_keywords", []))
    strong_cpv = _any_cpv(cpv_codes, cfg.get("strong_cpv_prefixes", []))
    weak_cpv = _any_cpv(cpv_codes, cfg.get("weak_cpv_prefixes", []))

    if strong_kw or (weak_kw and strong_cpv):
        return "strong"
    if weak_kw or strong_cpv or weak_cpv:
        return "weak"
    return "none"


def is_ai_relevant(
    title: str | None,
    description: str | None,
    cpv_codes: list[str] | None,
    config: dict | None = None,
) -> bool:
    """Return True if a notice is AI-relevant at any confidence (not 'none').

    Thin boolean wrapper over classify() for callers that only need a flag.

    Args:
        title: Notice title field.
        description: Notice description field.
        cpv_codes: List of CPV code strings from the notice.
        config: Override config dict (uses module default if None).

    Returns:
        True if the notice scores 'strong' or 'weak'.
    """
    return classify(title, description, cpv_codes, config) != "none"
