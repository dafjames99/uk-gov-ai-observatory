"""Tests for AI relevance classification logic."""

import pytest

from src.ingest.ai_relevance import is_ai_relevant, config_version

_CFG = {
    "version": "test-1.0",
    "keywords": ["artificial intelligence", "machine learning", "algorithm"],
    "cpv_code_prefixes": ["7221", "4800"],
}


def test_keyword_match_in_title():
    assert is_ai_relevant("Machine Learning Pipeline", None, None, config=_CFG)


def test_keyword_match_in_description():
    assert is_ai_relevant(None, "Using artificial intelligence to process claims", None, config=_CFG)


def test_keyword_match_case_insensitive():
    assert is_ai_relevant("ALGORITHM Development Services", None, None, config=_CFG)


def test_cpv_prefix_match():
    assert is_ai_relevant(None, None, ["72212000"], config=_CFG)


def test_cpv_prefix_match_48xx():
    assert is_ai_relevant(None, None, ["48001000"], config=_CFG)


def test_no_match_returns_false():
    assert not is_ai_relevant(
        "Office Cleaning Services",
        "Weekly cleaning of government buildings",
        ["90911200"],
        config=_CFG,
    )


def test_none_inputs_returns_false():
    assert not is_ai_relevant(None, None, None, config=_CFG)


def test_empty_cpv_list_falls_back_to_keywords():
    assert is_ai_relevant("Algorithm-based fraud detection", None, [], config=_CFG)


def test_config_version():
    assert config_version() != "unknown"
