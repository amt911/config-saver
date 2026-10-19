"""Retention policy model and the pure keep-set (restic's --keep-* semantics)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from config_saver.lib.models.model import Model
from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.retention import keep_set


def test_defaults_are_7_4_12_2() -> None:
    policy = RetentionModel()
    assert (policy.daily, policy.weekly, policy.monthly, policy.yearly) == (7, 4, 12, 2)


def test_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        RetentionModel(daily=-1)


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        RetentionModel(hourly=3)  # type: ignore[call-arg]


def test_model_accepts_retention_block() -> None:
    assert Model(directories=["/x"]).retention is None
    model = Model(directories=["/x"], retention={"daily": 3})  # type: ignore[arg-type]
    assert model.retention is not None
    assert model.retention.daily == 3
    assert model.retention.weekly == 4


def test_keep_set_empty() -> None:
    assert keep_set([], RetentionModel()) == set()


def test_newest_always_kept_even_all_zero() -> None:
    stamps = [datetime(2026, 1, d, 3) for d in range(1, 6)]
    policy = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    assert keep_set(stamps, policy) == {datetime(2026, 1, 5, 3)}


def test_daily_keeps_last_per_day() -> None:
    stamps = [
        datetime(2026, 3, 9, 1),
        datetime(2026, 3, 9, 2),
        datetime(2026, 3, 9, 3),
        datetime(2026, 3, 8, 1),
        datetime(2026, 3, 8, 2),
    ]
    policy = RetentionModel(daily=2, weekly=0, monthly=0, yearly=0)
    assert keep_set(stamps, policy) == {datetime(2026, 3, 9, 3), datetime(2026, 3, 8, 2)}


def test_daily_count_limits_days() -> None:
    stamps = [datetime(2026, 3, d, 3) for d in range(1, 6)]
    policy = RetentionModel(daily=2, weekly=0, monthly=0, yearly=0)
    assert keep_set(stamps, policy) == {datetime(2026, 3, 5, 3), datetime(2026, 3, 4, 3)}


def test_60_daily_runs_default_policy_keeps_expected() -> None:
    end = datetime(2026, 10, 19, 3)
    stamps = [end - timedelta(days=i) for i in range(60)]  # 2026-08-21 .. 2026-10-19
    expected = {datetime(2026, 10, d, 3) for d in range(13, 20)}  # 7 newest days
    # last (newest) per ISO week, 4 weeks: W43 (10-19), W42 (10-18), W41 (10-11), W40 (10-04)
    expected |= {
        datetime(2026, 10, 19, 3),
        datetime(2026, 10, 18, 3),
        datetime(2026, 10, 11, 3),
        datetime(2026, 10, 4, 3),
    }
    # last per month: Oct, Sep, Aug
    expected |= {datetime(2026, 10, 19, 3), datetime(2026, 9, 30, 3), datetime(2026, 8, 31, 3)}
    # last per year: 2026 only (no 2025 stamps)
    expected |= {datetime(2026, 10, 19, 3)}
    assert keep_set(stamps, RetentionModel()) == expected


def test_iso_week_uses_iso_year() -> None:
    # 2025-12-29 and 2026-01-01 are the same ISO week (2026-W01).
    stamps = [datetime(2025, 12, 29, 3), datetime(2026, 1, 1, 3)]
    policy = RetentionModel(daily=0, weekly=2, monthly=0, yearly=0)
    assert keep_set(stamps, policy) == {datetime(2026, 1, 1, 3)}


def test_monthly_and_yearly() -> None:
    stamps = [
        datetime(2024, 5, 1),
        datetime(2024, 5, 20),
        datetime(2025, 5, 3),
        datetime(2025, 6, 3),
        datetime(2026, 1, 1),
    ]
    monthly = RetentionModel(daily=0, weekly=0, monthly=3, yearly=0)
    assert keep_set(stamps, monthly) == {
        datetime(2026, 1, 1),
        datetime(2025, 6, 3),
        datetime(2025, 5, 3),
    }
    yearly = RetentionModel(daily=0, weekly=0, monthly=0, yearly=2)
    assert keep_set(stamps, yearly) == {datetime(2026, 1, 1), datetime(2025, 6, 3)}


def test_duplicates_do_not_consume_buckets() -> None:
    stamp = datetime(2026, 3, 9, 3)
    stamps = [stamp, stamp, datetime(2026, 3, 8, 3)]
    policy = RetentionModel(daily=2, weekly=0, monthly=0, yearly=0)
    assert keep_set(stamps, policy) == {stamp, datetime(2026, 3, 8, 3)}


_STAMPS = st.lists(
    st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)),
    min_size=1,
    max_size=40,
)


@given(stamps=_STAMPS, seed=st.integers(0, 1000))
def test_keep_set_subset_contains_newest_deterministic(stamps: list[datetime], seed: int) -> None:
    policy = RetentionModel()
    result = keep_set(stamps, policy)
    assert result <= set(stamps)
    assert max(stamps) in result
    shuffled = list(stamps)
    random.Random(seed).shuffle(shuffled)
    assert keep_set(shuffled, policy) == result
