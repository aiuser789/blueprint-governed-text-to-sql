from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sql_agent.executor import to_json_value


def test_month_buckets_show_as_dates():
    assert to_json_value(datetime(2026, 7, 1, tzinfo=timezone.utc)) == "2026-07-01"
    assert to_json_value(datetime(2026, 7, 1)) == "2026-07-01"
    assert to_json_value(date(2026, 7, 1)) == "2026-07-01"


def test_times_are_readable():
    assert to_json_value(datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)) == "2026-07-01 14:30:00 UTC"
    assert to_json_value(datetime(2026, 7, 1, 14, 30, 5)) == "2026-07-01 14:30:05"
    assert to_json_value(datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=-4)))) == "2026-07-01 09:00:00 -04:00"
    assert to_json_value(Decimal("12.50")) == 12.5
