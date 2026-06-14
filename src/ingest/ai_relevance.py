"""Load the AI-relevance config and classify procurement notices."""

import logging
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


def is_ai_relevant(
    title: str | None,
    description: str | None,
    cpv_codes: list[str] | None,
    config: dict | None = None,
) -> bool:
    """Return True if a notice matches the AI-relevance definition.

    A notice is AI-relevant if any keyword appears in the combined title +
    description text (case-insensitive), OR if any CPV code starts with a
    configured prefix. Either condition is sufficient.

    Args:
        title: Notice title field.
        description: Notice description field.
        cpv_codes: List of CPV code strings from the notice.
        config: Override config dict (uses module default if None).

    Returns:
        True if the notice is AI-relevant.
    """
    cfg = config or load_config()
    text = " ".join(filter(None, [title, description])).lower()

    for keyword in cfg.get("keywords", []):
        if keyword.lower() in text:
            return True

    if cpv_codes:
        for code in cpv_codes:
            for prefix in cfg.get("cpv_code_prefixes", []):
                if str(code).startswith(prefix):
                    return True

    return False
