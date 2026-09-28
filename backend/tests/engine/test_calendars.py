from datetime import UTC, datetime

from monxuplan_engine.calendars import TimeAxis, WorkCalendar, expand_calendar
from monxuplan_engine.contract import CalendarSpec

H = 60
D = 1440


def test_add_work_pauses_over_breaks():
    cal = WorkCalendar([(0, 480, False), (600, 1080, False)])  # 0-8h, 10-18h
    assert cal.add_work(0, 60) == 60
    assert cal.add_work(420, 120) == 660  # 60 before the gap + 60 after
    assert cal.add_work(480, 30) == 630  # starts at the next window
    assert cal.add_work(0, 960) == 1080
    assert cal.add_work(0, 961) is None  # more work than the calendar has
    assert cal.working_between(0, 1080) == 960


def test_sub_work_is_inverse_of_add_work():
    cal = WorkCalendar([(0, 480, False), (600, 1080, False), (1440, 1920, False)])
    for s in (0, 100, 470, 600, 700, 1440):
        for d in (1, 30, 200, 600):
            e = cal.add_work(s, d)
            if e is None:
                continue
            back = cal.sub_work(e, d)
            assert cal.working_between(back, e) == d
            assert back >= s


def test_uninterrupted_windows():
    cal = WorkCalendar([(0, 480, False), (600, 1080, False)])
    assert cal.fits_uninterrupted(0, 480)
    assert not cal.fits_uninterrupted(400, 120)
    assert cal.next_uninterrupted_start(400, 120) == 600
    assert cal.next_uninterrupted_start(0, 500) is None


def test_adjacent_regular_and_overtime_are_continuous():
    cal = WorkCalendar([(0, 480, False), (480, 600, True)])
    assert cal.fits_uninterrupted(400, 200)
    assert cal.overtime_between(0, 600) == 120
    assert cal.without_overtime().total_minutes == 480


def test_intersect_and_subtract():
    a = WorkCalendar([(0, 1000, False)])
    b = WorkCalendar([(100, 200, False), (500, 1500, False)])
    c = a.intersect(b)
    assert c.windows() == [(100, 200, False), (500, 1000, False)]
    d = c.subtract([(150, 600)])
    assert d.windows() == [(100, 150, False), (600, 1000, False)]


def test_shift_pattern_with_breaks_and_holiday():
    axis = TimeAxis(datetime(2026, 9, 28, 0, 0, tzinfo=UTC), "UTC")  # Monday
    spec = CalendarSpec.model_validate(
        {
            "id": "C",
            "timezone": "UTC",
            "shifts": [
                {"weekday": d, "start": "06:00", "end": "14:00", "breaks": [{"start": "10:00", "end": "10:30"}]} for d in range(5)
            ],
            "exceptions": [{"start": "2026-09-29T00:00:00", "end": "2026-09-30T00:00:00", "kind": "CLOSED", "reason": "Holiday"}],
        }
    )
    cal = expand_calendar(spec, axis, 0, 7 * D)
    # Monday 7.5 h, Tuesday holiday, Wed-Fri 7.5 h each
    assert cal.working_between(0, D) == 450
    assert cal.working_between(D, 2 * D) == 0
    assert cal.working_between(0, 7 * D) == 4 * 450


def test_overnight_shift_across_dst_end_europe_madrid():
    # 2026-10-25: clocks go back 03:00 CEST -> 02:00 CET. A 22:00-06:00 shift that night lasts 9 h.
    axis = TimeAxis(datetime(2026, 10, 24, 0, 0, tzinfo=UTC), "Europe/Madrid")
    spec = CalendarSpec.model_validate(
        {"id": "N", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "22:00", "end": "06:00"} for d in range(7)]}
    )
    cal = expand_calendar(spec, axis, 0, 4 * D)
    lengths = [e - s for s, e in zip(cal.starts, cal.ends, strict=True)]
    assert 9 * H in lengths  # night of the change
    assert 8 * H in lengths


def test_overnight_shift_across_dst_start():
    # 2027-03-28: clocks go forward 02:00 CET -> 03:00 CEST. The 22:00-06:00 shift lasts 7 h.
    axis = TimeAxis(datetime(2027, 3, 26, 0, 0, tzinfo=UTC), "Europe/Madrid")
    spec = CalendarSpec.model_validate(
        {"id": "N", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "22:00", "end": "06:00"} for d in range(7)]}
    )
    cal = expand_calendar(spec, axis, 0, 4 * D)
    lengths = [e - s for s, e in zip(cal.starts, cal.ends, strict=True)]
    assert 7 * H in lengths


def test_extra_work_on_holiday_is_honoured():
    axis = TimeAxis(datetime(2026, 9, 28, 0, 0, tzinfo=UTC), "UTC")
    spec = CalendarSpec.model_validate(
        {
            "id": "C",
            "timezone": "UTC",
            "shifts": [{"weekday": d, "start": "06:00", "end": "14:00"} for d in range(5)],
            "exceptions": [
                {"start": "2026-09-29T00:00:00", "end": "2026-09-30T00:00:00", "kind": "CLOSED"},
                {"start": "2026-09-29T08:00:00", "end": "2026-09-29T12:00:00", "kind": "WORKING"},
                {"start": "2026-09-28T14:00:00", "end": "2026-09-28T16:00:00", "kind": "OVERTIME"},
            ],
        }
    )
    cal = expand_calendar(spec, axis, 0, 7 * D)
    assert cal.working_between(D, 2 * D) == 240
    assert cal.overtime_between(0, D) == 120
