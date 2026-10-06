from datetime import date, datetime, timedelta, timezone

import pytest

from studybot.clock import FakeClock, StudyCalendar, from_iso, parse_hhmm, to_iso
from conftest import MSK


def test_day_boundary_three_am(cal):
    # 02:59 во вторник — ещё понедельник
    assert cal.study_date(datetime(2026, 9, 29, 2, 59, tzinfo=MSK)) == date(2026, 9, 28)
    assert cal.study_date(datetime(2026, 9, 29, 3, 0, tzinfo=MSK)) == date(2026, 9, 29)


def test_study_date_from_utc(cal):
    # 23:30 UTC = 02:30 МСК следующего календарного дня → тот же учебный день
    moment = datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc)
    assert cal.study_date(moment) == date(2026, 9, 28)


def test_naive_rejected(cal):
    with pytest.raises(ValueError):
        cal.study_date(datetime(2026, 9, 28, 12, 0))


def test_day_start_and_end(cal):
    d = date(2026, 9, 28)
    assert cal.day_start(d) == datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    assert cal.day_end(d) - cal.day_start(d) == timedelta(days=1)


def test_notify_time_in_study_day(cal):
    d = date(2026, 9, 28)
    assert cal.at_local(d, "20:30") == datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc)
    # 01:00 относится к ночи после дня
    assert cal.study_date(cal.at_local(d, "01:00")) == d


def test_weekend_and_week(cal):
    assert not cal.is_weekend(date(2026, 9, 25))  # пятница
    assert cal.is_weekend(date(2026, 9, 26))
    assert cal.is_sunday(date(2026, 9, 27))
    assert cal.week_start(date(2026, 9, 27)) == date(2026, 9, 21)


def test_saturday_night_block_counts_to_saturday(cal):
    # вечерний блок в 01:30 ночи на воскресенье — суббота, выходная норма
    d = cal.study_date(datetime(2026, 9, 27, 1, 30, tzinfo=MSK))
    assert d == date(2026, 9, 26) and cal.is_weekend(d) and not cal.is_sunday(d)


def test_fake_clock(clock):
    start = clock.now()
    clock.advance(hours=15)
    assert clock.now() - start == timedelta(hours=15)
    assert clock.now().tzinfo is timezone.utc
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


def test_iso_roundtrip(clock):
    assert from_iso(to_iso(clock.now())) == clock.now()


@pytest.mark.parametrize("bad", ["2030", "25:00", "aa:bb", ""])
def test_parse_hhmm_bad(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)
