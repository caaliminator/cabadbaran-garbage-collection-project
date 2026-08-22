"""
Asia/Manila time helpers.

Every timestamp the system stores or displays is Manila local time. Call
`now()` here instead of `datetime.now()` anywhere else, so a server running in
UTC still files entries under the right Philippine date.

Windows does not ship the IANA time zone database, so `ZoneInfo("Asia/Manila")`
raises there unless the optional `tzdata` package is installed. Rather than add
a dependency, we fall back to a fixed UTC+8 offset -- which is exactly correct
for the Philippines, as it has observed no daylight saving since 1978.
"""

from datetime import date, datetime, time, timedelta, timezone

_FIXED = timezone(timedelta(hours=8), "PHT")

try:  # pragma: no cover - depends on the host having tzdata
    from zoneinfo import ZoneInfo

    MANILA = ZoneInfo("Asia/Manila")
except Exception:  # ZoneInfoNotFoundError on a bare Windows install
    MANILA = _FIXED

ISO_DATE = "%Y-%m-%d"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")


def now() -> datetime:
    """Timezone-aware 'right now' in Manila."""
    return datetime.now(MANILA)


def today() -> date:
    return now().date()


def today_str() -> str:
    """Today as YYYY-MM-DD -- the key every dated record is filed under."""
    return today().strftime(ISO_DATE)


def stamp() -> str:
    """
    ISO-8601 timestamp for audit fields.

    Milliseconds, not seconds: several records can be written inside one
    second, and code that orders events by timestamp (which collections are
    still waiting in an MRF, for instance) needs them to compare distinctly.
    """
    return now().isoformat(timespec="milliseconds")


def to_date(value) -> date | None:
    """Accept a date, datetime, or YYYY-MM-DD string; return a date or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], ISO_DATE).date()
    except ValueError:
        return None


def date_str(value) -> str:
    d = to_date(value)
    return d.strftime(ISO_DATE) if d else ""


def weekday_name(value=None) -> str:
    """'Monday' ... 'Sunday' for a date (default today)."""
    d = to_date(value) or today()
    return WEEKDAYS[d.weekday()]


def date_range(start, end) -> list[date]:
    """Inclusive list of dates from start to end; empty if the order is wrong."""
    a, b = to_date(start), to_date(end)
    if not a or not b or b < a:
        return []
    return [a + timedelta(days=n) for n in range((b - a).days + 1)]


def in_range(value, start=None, end=None) -> bool:
    """Is `value` inside the inclusive [start, end] window? Open ends allowed."""
    d = to_date(value)
    if d is None:
        return False
    a, b = to_date(start), to_date(end)
    if a and d < a:
        return False
    if b and d > b:
        return False
    return True


def display_date(value=None) -> str:
    """'April 27, 2026' -- the format the mockups use."""
    d = to_date(value) or today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def display_day(value=None) -> str:
    """'Monday, April 27, 2026'."""
    d = to_date(value) or today()
    return f"{weekday_name(d)}, {display_date(d)}"


def display_time(value=None) -> str:
    """'9:10 AM' -- no leading zero, matching the mockups."""
    dt = value if isinstance(value, datetime) else now()
    return dt.strftime("%I:%M %p").lstrip("0")


def start_of_day(value=None) -> datetime:
    d = to_date(value) or today()
    return datetime.combine(d, time.min, tzinfo=MANILA)


def parse_stamp(value) -> datetime | None:
    """Read back an ISO timestamp written by `stamp()`."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=MANILA)
