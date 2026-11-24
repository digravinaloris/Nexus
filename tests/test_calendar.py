import datetime as dt
import sys
from pathlib import Path

# Ensure repo root is importable even when CI invokes pytest from tests/.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from cogs.calendar import parse_event_datetime, validate_timezone, UTC

def test_timezone_validation():
    assert validate_timezone('Europe/Paris') == 'Europe/Paris'
    with pytest.raises(ValueError): validate_timezone('Fake/Timezone')

def test_paris_time_to_utc():
    now=dt.datetime(2026,1,1,tzinfo=UTC)
    assert parse_event_datetime('2026-01-02','18:30','Europe/Paris',now)==dt.datetime(2026,1,2,17,30,tzinfo=UTC)

def test_date_only_local_midnight():
    now=dt.datetime(2026,1,1,tzinfo=UTC)
    assert parse_event_datetime('2026-01-03',None,'Europe/Paris',now)==dt.datetime(2026,1,2,23,tzinfo=UTC)

@pytest.mark.parametrize('date,time',[('bad','10:00'),('2026-01-02','25:00'),('2026-01-02','10:99')])
def test_invalid_date_time(date,time):
    with pytest.raises(ValueError): parse_event_datetime(date,time,'Europe/Paris',dt.datetime(2026,1,1,tzinfo=UTC))

def test_past_event_rejected():
    with pytest.raises(ValueError,match='future'): parse_event_datetime('2025-12-31','12:00','UTC',dt.datetime(2026,1,1,tzinfo=UTC))

def test_dst_gap_rejected():
    with pytest.raises(ValueError,match='does not exist'): parse_event_datetime('2026-03-29','02:30','Europe/Paris',dt.datetime(2026,1,1,tzinfo=UTC))
