"""
Per-day summaries -- the History pages, and the source for reports.

A day's summary is **computed** from the records for that date, so it is always
consistent with them. Once a day is over, the summary is **frozen** into
`history.json` and read from there afterwards.

Freezing matters for one specific reason: figures that have already been
reported must not silently change. A property deleted next month, or a
correction made to a record, would otherwise rewrite last week's numbers and
nobody would know the report they printed no longer matches the system.

The freeze runs lazily, on the first request of a new day, rather than on a
background scheduler -- so it survives the server being switched off overnight
and adds no dependency (see `ensure_frozen`).
"""

from services import (carryover_service, collection_service, mrf_service,
                      property_service, storage, timeutil)


def compute(date, barangay_id: str | None = None) -> dict:
    """
    Build a day's summary from the underlying records.

    `barangay_id=None` produces the citywide summary.
    """
    day = timeutil.date_str(date)
    properties = property_service.listing(barangay_id=barangay_id)
    counts = collection_service.counts(properties, day)
    entries = collection_service.entries_for_date(day, barangay_id=barangay_id)

    if barangay_id:
        pickups = storage.find("mrf_pickups", date=day, barangay_id=barangay_id)
        deliveries = []
    else:
        pickups = storage.find("mrf_pickups", date=day)
        deliveries = storage.find("deliveries", date=day)

    collected_mrfs = [p for p in pickups if p.get("status") == mrf_service.COLLECTED]
    missed_mrfs = [p for p in pickups if p.get("status") == mrf_service.NOT_COLLECTED]

    return {
        "date": day,
        "weekday": timeutil.weekday_name(day),
        "barangay_id": barangay_id,
        "scope": "barangay" if barangay_id else "city",
        "properties": {
            "total": counts["total"],
            "collected": counts["collected"],
            "pending": counts["pending"],
            "not_collected": counts["not_collected"],
            "disputed": counts["disputed"],
            "percent": counts["percent"],
        },
        "load": collection_service.totals(entries),
        "mrf": {
            "collected": len(collected_mrfs),
            "missed": len(missed_mrfs),
            "total": 1 if barangay_id else storage.count("barangays"),
        },
        "deliveries": {
            "count": len(deliveries),
            "sacks": sum((d.get("load") or {}).get("sacks", 0) for d in deliveries),
            "kilos": sum((d.get("load") or {}).get("kilos", 0) for d in deliveries),
        },
        "carry_overs_opened": sum(
            1 for c in storage.read("carry_overs")
            if (c.get("first_missed_date") == day
                and (not barangay_id or c.get("barangay_id") == barangay_id))),
        "entries": len(entries),
    }


def frozen(date, barangay_id: str | None = None) -> dict | None:
    day = timeutil.date_str(date)
    for row in storage.find("history", date=day):
        if row.get("barangay_id") == barangay_id:
            return row
    return None


def summary_for(date, barangay_id: str | None = None) -> dict:
    """
    A day's summary: the frozen record if there is one, otherwise computed
    live. Today is always live -- it is still changing.
    """
    day = timeutil.date_str(date)
    if day < timeutil.today_str():
        saved = frozen(day, barangay_id)
        if saved:
            return {**saved, "is_frozen": True}
    return {**compute(day, barangay_id), "is_frozen": False}


def freeze(date, actor: str = "system") -> list[dict]:
    """
    Write the citywide and per-barangay summaries for a past date.

    Idempotent: a day already frozen is left exactly as it was, because
    re-freezing is precisely the silent rewrite this exists to prevent.
    """
    day = timeutil.date_str(date)
    if not day or day >= timeutil.today_str():
        return []

    written = []
    scopes = [None] + [b["id"] for b in storage.read("barangays")]
    for barangay_id in scopes:
        if frozen(day, barangay_id):
            continue
        written.append(storage.insert("history", compute(day, barangay_id), actor))
    return written


def ensure_frozen(actor: str = "system", look_back_days: int = 14) -> list[dict]:
    """
    The lazy end-of-day job. Called on the first request of a new day: if
    yesterday has no frozen summary, compute and write it now.

    This replaces a background scheduler. It costs one check per request
    (a cheap lookup), needs no extra dependency, and -- unlike a cron job --
    still runs correctly if the server was switched off overnight, because it
    walks back over any days it missed.
    """
    from datetime import timedelta

    today = timeutil.today()
    written = []
    for offset in range(1, look_back_days + 1):
        day = timeutil.date_str(today - timedelta(days=offset))
        if frozen(day, None):
            break       # this day and everything older is already done
        # Close off any MRF nobody gave a status, so its load is not lost.
        mrf_service.auto_mark_missed(day, actor)
        written.extend(freeze(day, actor))
    return written


def feed(barangay_id: str | None = None, limit: int = 14) -> list[dict]:
    """
    Recent days, newest first, for the History pages. Today is included and
    marked, so the page is not empty on a fresh install.
    """
    from datetime import timedelta

    today = timeutil.today()
    rows = []
    for offset in range(limit):
        day = today - timedelta(days=offset)
        summary = summary_for(day, barangay_id)
        rows.append({
            **summary,
            "is_today": offset == 0,
            "label": ("Today" if offset == 0
                      else "Yesterday" if offset == 1
                      else timeutil.weekday_name(day)),
            "date_display": timeutil.display_day(day),
            "had_activity": bool(summary["entries"] or summary["mrf"]["collected"]
                                 or summary["mrf"]["missed"]),
        })
    return rows
