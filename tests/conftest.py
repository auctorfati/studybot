import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studybot.clock import FakeClock, StudyCalendar  # noqa: E402
from studybot.db import open_db  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
def db():
    conn = open_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def clock():
    # понедельник, 12:00 по Москве
    return FakeClock(datetime(2026, 9, 28, 12, 0, tzinfo=MSK))


@pytest.fixture
def cal():
    return StudyCalendar.from_strings("Europe/Moscow", "03:00")
