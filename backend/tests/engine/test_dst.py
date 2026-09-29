"""Daylight saving time (case E): working time is real elapsed time in the plant's time zone.

A night shift 22:00–06:00 (Europe/Madrid) lasts 9 real hours the night clocks go back (25 Oct 2026,
03:00 CEST → 02:00 CET) and 7 real hours the night they go forward (29 Mar 2026, 02:00 CET → 03:00
CEST). An operation needing exactly that working time fills the shift — nothing counted twice, nothing
lost — and one minute more spills to the next night."""

from datetime import UTC, datetime, timedelta

import pytest

from monxuplan_engine import solve
from monxuplan_engine.contract import Problem

NIGHT = {"id": "NIGHT", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "22:00", "end": "06:00"} for d in range(7)]}


def _problem(start_utc: datetime, minutes: int) -> Problem:
    return Problem.model_validate(
        {
            "horizon": {"start": start_utc.isoformat(), "end": (start_utc + timedelta(days=4)).isoformat(), "timezone": "Europe/Madrid", "overflow_days": 5},
            "calendars": [NIGHT],
            "resources": [{"id": "M", "code": "M", "kind": "MACHINE", "calendar_id": "NIGHT"}],
            "orders": [{"id": "O", "number": "O", "item_id": "I", "item_code": "I", "quantity": 1, "due": (start_utc + timedelta(days=3)).isoformat()}],
            "operations": [{"id": "O/10", "order_id": "O", "seq": 10, "quantity": 1, "duration": {"run_minutes_per_unit": minutes}, "modes": [{"resource_id": "M"}], "interruptible": False}],
            "solver": {"provider": "heuristic", "time_limit_s": 5, "local_search": False, "multi_start": False},
        }
    )


@pytest.mark.parametrize(
    ("night_start_utc", "real_minutes"),
    [
        (datetime(2026, 10, 24, 20, 0, tzinfo=UTC), 540),  # 22:00 CEST → 06:00 CET: 9 h
        (datetime(2026, 3, 28, 21, 0, tzinfo=UTC), 420),  # 22:00 CET → 06:00 CEST: 7 h
        (datetime(2026, 10, 17, 20, 0, tzinfo=UTC), 480),  # an ordinary night: 8 h
    ],
)
def test_night_shift_across_clock_change(night_start_utc, real_minutes):
    sol = solve(_problem(night_start_utc - timedelta(hours=1), real_minutes))
    x = sol.schedule[0]
    assert x.start == night_start_utc
    assert x.end - x.start == timedelta(minutes=real_minutes)  # elapsed = working time, exactly
    assert x.working_minutes == real_minutes
    assert not [v for v in sol.violations if v.hardness == "HARD"]
    # one minute more does not fit in that night (uninterruptible): it goes to a later night when an
    # ordinary 8 h night is long enough, and is reported unschedulable (with the reason) when not
    sol2 = solve(_problem(night_start_utc - timedelta(hours=1), real_minutes + 1))
    if real_minutes + 1 > 540:  # longer than any night, even the one with the repeated hour
        assert not sol2.schedule and sol2.unscheduled[0].reason == "NO_FEASIBLE_SLOT"
    else:
        y = sol2.schedule[0]
        assert y.start > night_start_utc + timedelta(hours=12)
        assert y.end - y.start == timedelta(minutes=real_minutes + 1)
        if real_minutes + 1 > 480:  # only the 9-hour night of 24→25 October can hold it
            assert y.start == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    assert not [v for v in sol2.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED"]
