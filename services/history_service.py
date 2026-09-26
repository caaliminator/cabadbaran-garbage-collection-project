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

    # The day's own stops only. A carry-over stop is an earlier day's batch,
    # counted separately so one barangay cannot read "collected" twice.
    regular = [p for p in pickups if mrf_service.is_regular(p)]
    collected_mrfs = [p for p in regular if p.get("status") == mrf_service.COLLECTED]
    missed_mrfs = [p for p in regular if p.get("status") == mrf_service.NOT_COLLECTED]
    carried = [p for p in pickups if not mrf_service.is_regular(p)
               and p.get("status") == mrf_service.COLLECTED]

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
            "carry_overs_collected": len(carried),
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

    # A day the system held nothing on is not worth preserving. Freezing
    # exists so reported figures cannot silently change, and there were no
    # figures -- writing one empty row per barangay would only bury the real
    # history under rows that never meant anything. Unfrozen days still show
    # on the History pages; summary_for computes them live.
    if _nothing_happened(compute(day, None)):
        return []

    # Every scope's summary is worked out first and written together: one
    # rewrite of history.json for the day rather than 32, which on a store of
    # weeks is the difference between a moment and most of a minute.
    # The summaries only read, so each file is parsed once for all of them.
    with storage.read_cache():
        already = {row.get("barangay_id") for row in storage.find("history", date=day)}
        scopes = [None] + [b["id"] for b in storage.read("barangays")]
        rows = [compute(day, barangay_id) for barangay_id in scopes
                if barangay_id not in already]
    return storage.insert_many("history", rows, actor)


def _nothing_happened(summary: dict) -> bool:
    """
    Nothing dated to that day: no collection entry, no MRF pickup, no delivery.

    The property count is deliberately not part of this. A property carries no
    date, so `compute` counts today's properties against any past day -- which
    means a day before the register existed would freeze as "0 of 10
    collected, 0%", reading as a total collection failure rather than as a day
    the system was not in use. Only dated records can say a day happened.
    """
    return (not summary["entries"]
            and not summary["mrf"]["collected"]
            and not summary["mrf"]["missed"]
            and not summary["deliveries"]["count"])


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


def days_on_record(cap: int = 1096) -> int:
    """
    How many days the History pages can page back through: from the first
    dated record the system holds to today, inclusive. At least 1, so a fresh
    install still shows today; capped, so one stray ancient date cannot turn
    the pager into thousands of empty pages.
    """
    earliest = None
    for collection in ("collections", "mrf_pickups", "deliveries", "history"):
        for row in storage.read(collection):
            day = row.get("date")
            if day and (earliest is None or day < earliest):
                earliest = day
    first = timeutil.to_date(earliest) if earliest else None
    if not first:
        return 1
    return max(1, min(cap, (timeutil.today() - first).days + 1))


def feed(barangay_id: str | None = None, limit: int = 14,
         offset: int = 0) -> list[dict]:
    """
    Days newest first, for the History pages: `limit` of them, starting
    `offset` days back from today. Today is included and marked, so the page
    is not empty on a fresh install. Each day is computed or read only when
    it is on the page being shown, so paging far back costs no more than the
    first page.
    """
    from datetime import timedelta

    today = timeutil.today()
    rows = []
    for back in range(offset, offset + limit):
        day = today - timedelta(days=back)
        summary = summary_for(day, barangay_id)
        rows.append({
            **summary,
            "is_today": back == 0,
            "label": ("Today" if back == 0
                      else "Yesterday" if back == 1
                      else timeutil.weekday_name(day)),
            "date_display": timeutil.display_day(day),
            "had_activity": bool(summary["entries"] or summary["mrf"]["collected"]
                                 or summary["mrf"]["missed"]),
        })
    return rows
