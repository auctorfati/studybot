from helpers import add_psy_item
from datetime import date

import pytest

from studybot.settings import SPEC, Settings, SettingError


@pytest.fixture
def st(db, clock):
    return Settings(db, clock)


def test_defaults_match_spec(st):
    assert st.get("notify_time") == "20:30"
    assert st.get("interval_grid") == [1, 3, 7, 14, 30, 60, 120, 240]
    assert st.get("psy_enabled") is True
    assert st.changed() == {}


def test_set_and_persist(st, db, clock):
    st.set("notify_time", "21:05")
    assert Settings(db, clock).get("notify_time") == "21:05"
    assert st.changed() == {"notify_time": "21:05"}
    st.reset("notify_time")
    assert st.get("notify_time") == "20:30"


@pytest.mark.parametrize("key,value", [
    ("notify_time", "25:00"),
    ("new_en_weekday", -1),
    ("new_en_weekday", True),
    ("mode", "loud"),
    ("interval_grid", [1, 3, 7]),        # нет шага 30
    ("interval_grid", [3, 1, 30]),       # не по возрастанию
    ("psy_enabled", 1),
    ("pause_until", "завтра"),
])
def test_validation(st, key, value):
    with pytest.raises(SettingError):
        st.set(key, value)


def test_unknown_key(st):
    with pytest.raises(SettingError):
        st.get("nope")


def test_norms_and_limits(st):
    mon, sat = date(2026, 9, 28), date(2026, 10, 3)
    assert st.norm_min(mon) == 90 and st.norm_min(sat) == 120              # норма по умолчанию
    assert st.new_limit_en(mon) == 8 and st.new_limit_en(sat) == 10


def test_transition_mode(st, db):
    """Психология включена по умолчанию, но без её банков — только английский."""
    mon = date(2026, 9, 28)
    assert st.track_shares() == {"en": 1.0, "psy": 0.0}
    assert st.new_limit_psy(mon) == 0
    add_psy_item(db)
    assert st.track_shares() == {"en": 0.5, "psy": 0.5}
    assert st.new_limit_psy(mon) == 12
    st.set("psy_enabled", False)
    assert st.track_shares() == {"en": 1.0, "psy": 0.0}


def test_pause(st):
    assert not st.is_paused(date(2026, 9, 28))
    st.set("pause_until", "2026-10-02")
    assert st.is_paused(date(2026, 10, 2))
    assert not st.is_paused(date(2026, 10, 3))
    st.set("pause_until", None)
    assert not st.is_paused(date(2026, 10, 2))


def test_spec_titles_present():
    assert all(spec.title for spec in SPEC.values())
