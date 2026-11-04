"""Unit tests for the Meta usage-header parser (services/meta_usage.py)."""

from __future__ import annotations

import json

from polaris_api.services.meta_usage import parse_usage_pct


def test_parse_picks_worst_constraint() -> None:
    """Remaining = 100 - max(call_count, total_cputime, total_time)."""
    header = json.dumps(
        {
            "biz123": [
                {
                    "type": "messages",
                    "call_count": 15,
                    "total_cputime": 8,
                    "total_time": 40,
                    "estimated_time_to_regain_access": 0,
                }
            ]
        }
    )
    assert parse_usage_pct(header) == 60.0  # 100 - 40


def test_parse_multiple_entries_takes_global_max() -> None:
    header = json.dumps(
        {
            "biz123": [
                {"type": "messages", "call_count": 10},
                {"type": "pages_messaging", "total_time": 70},
            ]
        }
    )
    assert parse_usage_pct(header) == 30.0  # 100 - 70


def test_parse_clamps_overage_to_zero() -> None:
    header = json.dumps({"biz": [{"call_count": 150}]})
    assert parse_usage_pct(header) == 0.0


def test_parse_invalid_json_returns_none() -> None:
    assert parse_usage_pct("not json") is None


def test_parse_no_numeric_fields_returns_none() -> None:
    assert parse_usage_pct(json.dumps({"biz": [{"type": "messages"}]})) is None


def test_parse_ignores_bool_fields() -> None:
    # booleans are ints in Python — make sure they don't count as usage.
    assert parse_usage_pct(json.dumps({"biz": [{"call_count": True}]})) is None
