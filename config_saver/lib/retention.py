"""Pure retention logic: which timestamps a policy keeps.

Mirrors restic's ``--keep-daily/weekly/monthly/yearly`` so the restic and tar
backends agree. Timestamps are naive local datetimes.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from datetime import datetime

from .models.retention_model import RetentionModel


def _walk(newest_first: list[datetime], count: int, key: Callable[[datetime], Hashable]) -> set[datetime]:
    kept: set[datetime] = set()
    keys: list[Hashable] = []
    for stamp in newest_first:
        if len(keys) >= count:
            break
        bucket = key(stamp)
        if keys and bucket == keys[-1]:
            continue
        kept.add(stamp)
        keys.append(bucket)
    return kept


def keep_set(timestamps: Iterable[datetime], policy: RetentionModel) -> set[datetime]:
    """Return the timestamps to keep: per bucket kind the newest of each of the
    last N distinct buckets, the union of all kinds, plus the single newest."""
    newest_first = sorted(set(timestamps), reverse=True)
    if not newest_first:
        return set()
    kept = {newest_first[0]}
    kept |= _walk(newest_first, policy.daily, lambda d: d.date())
    kept |= _walk(newest_first, policy.weekly, lambda d: d.isocalendar()[:2])
    kept |= _walk(newest_first, policy.monthly, lambda d: (d.year, d.month))
    kept |= _walk(newest_first, policy.yearly, lambda d: d.year)
    return kept
