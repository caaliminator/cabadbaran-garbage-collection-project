"""
The weekly waste schedule.

One record per weekday in `waste_schedule.json`. The City Hall Admin edits it;
barangay admins, collectors, and the public viewer all read it. It is the only
place the waste types are defined, which matters more than it looks: a
collection entry offers the waste types from *that day's* schedule row, so
editing Tuesday here changes what a collector can record on a Tuesday.

"Kitchen Waste" is the agreed citywide term. The mockup's dashboard chart said
"Food Waste" for the same thing; that is a mockup inconsistency, not a second
category.
"""

from services import storage, timeutil
from services.validation import ValidationError, Validator

DAYS = timeutil.WEEKDAYS

TONES = ("green", "blue", "amber", "red", "violet", "muted")

# Offered in the editor's waste-type field. Free text is still allowed, since
# the city may add a category we have not anticipated.
COMMON_TYPES = (
    "Biodegradable and Net Residual Waste",
    "Recyclable Waste",
    "Residual Waste",
    "Special Waste",
    "No Collection",
)


def week() -> list[dict]:
    """The seven rows in weekday order, Monday first."""
    rows = {r.get("day"): r for r in storage.read("waste_schedule")}
    return [rows.get(day) or _blank(day) for day in DAYS]


def for_day(day_name: str) -> dict:
    return next((r for r in week() if r["day"] == day_name), _blank(day_name))


def for_date(value=None) -> dict:
    """The schedule row governing a given date (default today, Manila)."""
    return for_day(timeutil.weekday_name(value))


def waste_types_for(value=None) -> list[str]:
    """
    The waste categories a collector may record on a date.

    Driven by the schedule rather than a fixed list, so a Tuesday entry offers
    recyclable categories instead of Kitchen Waste.
    """
    return list(for_date(value).get("details") or [])


def upcoming(today=None) -> list[dict]:
    """Monday-Saturday annotated Today / Tomorrow / In N Days, for the dashboard."""
    today = timeutil.to_date(today) or timeutil.today()
    start = today.weekday()
    out = []
    for offset, row in enumerate(week()[:6]):
        delta = (offset - start) % 7
        label = "Today" if delta == 0 else "Tomorrow" if delta == 1 else f"In {delta} Days"
        out.append({**row, "relative": label, "is_today": delta == 0})
    return out


def is_collection_day(value=None) -> bool:
    return bool(for_date(value).get("details"))


def month_calendar(year: int | None = None, month: int | None = None) -> dict:
    """
    A Sunday-first month grid where every cell carries its collection type,
    for the public viewer's calendar.

    The type comes from the weekly schedule, so editing Tuesday in the City
    Hall editor changes every Tuesday on this calendar at once.
    """
    import calendar as _calendar
    from datetime import date as _date

    today = timeutil.today()
    year = year or today.year
    month = month or today.month

    grid = _calendar.Calendar(firstweekday=6)   # 6 == Sunday
    weeks = []
    for week in grid.monthdatescalendar(year, month):
        row = []
        for day in week:
            sched = for_day(WEEKDAY_BY_INDEX[day.weekday()])
            row.append({
                "date": day,
                "day": day.day,
                "in_month": day.month == month,
                "is_today": day == today,
                "tone": sched.get("tone", "muted"),
                "waste_type": sched.get("waste_type"),
                "short": sched.get("short"),
                "has_collection": bool(sched.get("details")),
            })
        weeks.append(row)

    return {
        "weeks": weeks,
        "label": _date(year, month, 1).strftime("%B %Y"),
        "year": year,
        "month": month,
        "weekday_names": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        "prev": (year - 1, 12) if month == 1 else (year, month - 1),
        "next": (year + 1, 1) if month == 12 else (year, month + 1),
    }


WEEKDAY_BY_INDEX = list(DAYS)


def upcoming_days(count: int = 6, start=None) -> list[dict]:
    """The next N dates with their collection type, for the public list."""
    from datetime import timedelta

    today = timeutil.to_date(start) or timeutil.today()
    out = []
    for offset in range(count):
        day = today + timedelta(days=offset)
        row = for_day(timeutil.weekday_name(day))
        out.append({
            **row,
            "date": day,
            "day_num": day.day,
            "weekday_short": day.strftime("%a"),
            "is_today": offset == 0,
            "label": ("Today" if offset == 0
                      else "Tomorrow" if offset == 1
                      else timeutil.weekday_name(day)),
        })
    return out


def todays_cards(value=None) -> list[dict]:
    """
    Today's accepted waste, grouped for the public "Today's Waste" cards.

    Grouping is derived from the schedule row's own details rather than a
    hardcoded mapping, so a city that adds a category gets a card for it
    without a code change.
    """
    row = for_date(value)
    details = row.get("details") or []
    if not details:
        return []

    residual_terms = ("diaper", "tissue", "napkin", "residual")
    residual = [d for d in details
                if any(term in d.lower() for term in residual_terms)]
    main = [d for d in details if d not in residual]

    cards = []
    if main:
        cards.append({"title": row.get("short") or row.get("waste_type"),
                      "tone": row.get("tone", "green"),
                      "icon": "leaf", "items": main})
    if residual:
        cards.append({"title": "Net Residual", "tone": "amber",
                      "icon": "package", "items": residual})
    return cards


def _blank(day: str) -> dict:
    return {"day": day, "waste_type": "No Collection", "short": "No Collection",
            "tone": "muted", "details": []}


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def save_week(form, actor: str | None = None) -> list[dict]:
    """
    Save all seven rows at once -- the page edits the week as one table, so
    one submit either applies cleanly or changes nothing.

    Fields per day: `waste_type__<Day>`, `short__<Day>`, `tone__<Day>`, and
    `details__<Day>` as a newline- or comma-separated list.
    """
    v = Validator(form)
    updates = []

    for day in DAYS:
        waste_type = str(form.get(f"waste_type__{day}", "") or "").strip()
        if not waste_type:
            v.fail(f"waste_type__{day}", f"{day} needs a waste type "
                                         f"(use 'No Collection' for a rest day).")
            continue
        if len(waste_type) > 120:
            v.fail(f"waste_type__{day}", f"{day}'s waste type is too long.")
            continue

        tone = str(form.get(f"tone__{day}", "") or "muted").strip()
        if tone not in TONES:
            tone = "muted"

        details = _split_details(form.get(f"details__{day}", ""))
        if len(details) > 20:
            v.fail(f"details__{day}", f"{day} has too many detail entries (max 20).")
            continue

        short = str(form.get(f"short__{day}", "") or "").strip() or waste_type

        updates.append({
            "day": day, "waste_type": waste_type, "short": short[:60],
            "tone": tone, "details": details,
        })

    v.raise_if_invalid()

    existing = {r.get("day"): r for r in storage.read("waste_schedule")}
    saved = []
    for row in updates:
        current = existing.get(row["day"])
        if current:
            saved.append(storage.update("waste_schedule", current["id"], row, actor))
        else:
            saved.append(storage.insert("waste_schedule", row, actor))

    # Every role reads the schedule, so every client is told it changed.
    from services import triggers
    triggers.on_schedule_updated(actor)
    return saved


def _split_details(raw) -> list[str]:
    """Accept one-per-line or comma-separated; store a clean list either way."""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw or "").replace("\r", "").replace(",", "\n").split("\n")
    seen, out = set(), []
    for item in items:
        text = item.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text[:80])
    return out
