"""The Analyst's two switches (capability layer K2, plan R3/R7): its effort and the day's call budget."""

import pytest

from agent import config


def test_effort_defaults_to_medium_and_validates(monkeypatch):
    assert config.load().analyst_llm_effort == "medium"
    monkeypatch.setenv("AGENT_ANALYST_LLM_EFFORT", "high")
    assert config.load().analyst_llm_effort == "high"
    monkeypatch.setenv("AGENT_ANALYST_LLM_EFFORT", "max")
    with pytest.raises(ValueError, match="AGENT_ANALYST_LLM_EFFORT must be one of"):
        config.load()


def test_max_per_day_allows_zero_and_refuses_negative(monkeypatch):
    assert config.load().analyst_max_per_day == 2000
    monkeypatch.setenv("AGENT_ANALYST_MAX_PER_DAY", "0")
    assert config.load().analyst_max_per_day == 0
    for bad in ("-1", "1.5", "many"):
        monkeypatch.setenv("AGENT_ANALYST_MAX_PER_DAY", bad)
        with pytest.raises(ValueError, match="AGENT_ANALYST_MAX_PER_DAY must be a whole number"):
            config.load()
