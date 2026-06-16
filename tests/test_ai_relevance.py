"""Tests for tiered AI-relevance classification (v2)."""

from src.ingest.ai_relevance import classify, config_version, is_ai_relevant

_CFG = {
    "version": "test-2.0",
    "strong_keywords": ["artificial intelligence", "machine learning", "ai", "llm"],
    "weak_keywords": ["algorithm", "automation"],
    "strong_cpv_prefixes": ["7222", "7223"],
    "weak_cpv_prefixes": ["7221", "4800"],
}


# ── word-boundary matching ──────────────────────────────────────────────────


def test_short_token_does_not_match_inside_words():
    # 'ai' must not match 'maintain'/'Thai'; 'llm' must not match 'fulfillment'.
    assert classify("Maintain Thai fulfillment centre", None, None, config=_CFG) == "none"


def test_short_token_matches_as_whole_word():
    assert classify("AI-powered triage", None, None, config=_CFG) == "strong"
    assert classify("New LLM service", None, None, config=_CFG) == "strong"


# ── strong ─────────────────────────────────────────────────────────────────


def test_strong_keyword_alone_is_strong():
    assert classify("Machine Learning Pipeline", None, None, config=_CFG) == "strong"


def test_strong_keyword_case_insensitive():
    assert classify(None, "Uses ARTIFICIAL INTELLIGENCE", None, config=_CFG) == "strong"


def test_weak_keyword_plus_strong_cpv_is_strong():
    # 'algorithm' (weak) + 72220000 (strong CPV) → elevated to strong.
    assert classify("Algorithm work", None, ["72220000"], config=_CFG) == "strong"


# ── weak ───────────────────────────────────────────────────────────────────


def test_weak_keyword_alone_is_weak():
    assert classify("Automation of forms", None, None, config=_CFG) == "weak"


def test_strong_cpv_alone_is_weak():
    # A strong CPV without any keyword is only a weak signal.
    assert classify("Generic IT services", None, ["72230000"], config=_CFG) == "weak"


def test_weak_cpv_alone_is_weak():
    assert classify("Generic IT services", None, ["48001000"], config=_CFG) == "weak"


# ── none ───────────────────────────────────────────────────────────────────


def test_unrelated_is_none():
    assert classify(
        "Office Cleaning Services", "Weekly cleaning", ["90911200"], config=_CFG
    ) == "none"


def test_none_inputs_is_none():
    assert classify(None, None, None, config=_CFG) == "none"


# ── boolean wrapper ────────────────────────────────────────────────────────


def test_is_ai_relevant_true_for_weak_and_strong():
    assert is_ai_relevant("Automation of forms", None, None, config=_CFG)
    assert is_ai_relevant("Machine learning", None, None, config=_CFG)


def test_is_ai_relevant_false_for_none():
    assert not is_ai_relevant("Office cleaning", None, ["90911200"], config=_CFG)


def test_config_version_is_v2():
    assert config_version() == "2.0"
