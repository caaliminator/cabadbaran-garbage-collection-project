"""
Reports -- the five types the City Hall Admin can generate over a date range.

The mockup only says "Generate", so the concrete output is defined here: a
printable HTML page and a CSV download of the same rows. Both come from one
`build()` call, so the printed page and the spreadsheet can never disagree.

Every figure is derived from the records at generation time. Frozen days come
from `history.json` where they exist, so a report run twice over the same past
range produces the same numbers -- which is the whole reason days are frozen.
"""

import csv
import io

from services import (assignment_service, collection_service, history_service,
                      mrf_service, property_service, storage, timeutil)
from services.validation import ValidationError

PROPERTY_COLLECTION = "property_collection"
MRF_COLLECTION = "mrf_collection"
DELIVERIES = "deliveries"
CARRY_OVERS = "carry_overs"
COLLECTOR_PERFORMANCE = "collector_performance"

TYPES = (
    (PROPERTY_COLLECTION, "Property Collection"),
    (MRF_COLLECTION, "MRF Collection"),
    (DELIVERIES, "Deliveries to Landfill"),
    (CARRY_OVERS, "Carry-Overs"),
    (COLLECTOR_PERFORMANCE, "Collector Performance"),
)

TYPE_LABELS = dict(TYPES)

# A range wider than this would scan every record many times over; the limit
# keeps a mistyped year from locking the page up.
MAX_RANGE_DAYS = 366


def build(report_type: str, start, end, barangay_id: str | None = None) -> dict:
    """
    Returns {title, columns, rows, totals, meta} -- everything both outputs
    need, computed once.
    """
    if report_type not in TYPE_LABELS:
        raise ValidationError({"report_type": "Choose a report type."})

    first, last = timeutil.to_date(start), timeutil.to_date(end)
    if not first or not last:
        raise ValidationError({"start_date": "Choose a valid start and end date."})
    if last < first:
        raise ValidationError({"end_date": "The end date cannot be before the start date."})
    if (last - first).days > MAX_RANGE_DAYS:
        raise ValidationError(
            {"end_date": f"Choose a range of {MAX_RANGE_DAYS} days or fewer."})

    days = timeutil.date_range(first, last)
    builder = {
        PROPERTY_COLLECTION: _property_collection,
        MRF_COLLECTION: _mrf_collection,
        DELIVERIES: _deliveries,
        CARRY_OVERS: _carry_overs,
        COLLECTOR_PERFORMANCE: _collector_performance,
    }[report_type]

    result = builder(days, barangay_id)
    names = {b["id"]: b["name"] for b in storage.read("barangays")}

    result.update({
        "type": report_type,
        "title": TYPE_LABELS[report_type],
        "meta": {
            "start": timeutil.date_str(first),
            "end": timeutil.date_str(last),
            "start_display": timeutil.display_date(first),
            "end_display": timeutil.display_date(last),
            "days": len(days),
            "barangay": names.get(barangay_id) if barangay_id else "All barangays",
            "generated_at": timeutil.stamp(),
            "generated_display": (f"{timeutil.display_date(timeutil.now())} "
                                  f"{timeutil.display_time(timeutil.now())}"),
        },
    })
    return result


# ---------------------------------------------------------------------------
# The five report types
# ---------------------------------------------------------------------------

def _property_collection(days, barangay_id) -> dict:
    columns = ["Date", "Day", "Registered", "Collected", "Not Collected",
               "Pending", "% Complete", "Sacks", "Kilos", "Disputed"]
    rows, totals = [], {"collected": 0, "not_collected": 0, "pending": 0,
                        "sacks": 0, "kilos": 0, "disputed": 0}

    for day in days:
        summary = history_service.summary_for(day, barangay_id)
        props, load = summary["properties"], summary["load"]
        rows.append([
            timeutil.date_str(day), timeutil.weekday_name(day),
            props["total"], props["collected"], props["not_collected"],
            props["pending"], f"{props['percent']}%",
            load["sacks"], load["kilos"], props["disputed"],
        ])
        for key in ("collected", "not_collected", "pending", "disputed"):
            totals[key] += props[key]
        totals["sacks"] += load["sacks"]
        totals["kilos"] += load["kilos"]

    return {
        "columns": columns,
        "rows": rows,
        "totals": totals,
        "summary": [
            ("Collected", totals["collected"]),
            ("Not collected", totals["not_collected"]),
            ("Pending", totals["pending"]),
            ("Disputed", totals["disputed"]),
            # Two units, kept apart -- they are not addable.
            ("Total load", f"{totals['sacks']} sacks and {totals['kilos']} kg"),
        ],
    }


def _mrf_collection(days, barangay_id) -> dict:
    columns = ["Date", "Barangay", "Source Schedule Day", "Waste Type",
               "Truck", "Status", "Sacks", "Kilos"]
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    rows = []
    collected = missed = sacks = kilos = 0

    for day in days:
        for pickup in storage.find("mrf_pickups", date=timeutil.date_str(day)):
            if barangay_id and pickup.get("barangay_id") != barangay_id:
                continue
            load = pickup.get("load") or {}
            rows.append([
                pickup.get("date"), names.get(pickup.get("barangay_id"), "—"),
                pickup.get("source_schedule_day", "—"),
                pickup.get("waste_type", "—"),
                pickup.get("truck_code") or "—", pickup.get("status"),
                load.get("sacks", 0), load.get("kilos", 0),
            ])
            if pickup.get("status") == mrf_service.COLLECTED:
                collected += 1
                sacks += load.get("sacks", 0)
                kilos += load.get("kilos", 0)
            else:
                missed += 1

    rows.sort(key=lambda r: (r[0], r[1]))
    return {
        "columns": columns, "rows": rows,
        "totals": {"collected": collected, "missed": missed,
                   "sacks": sacks, "kilos": kilos},
        "summary": [
            ("MRF pickups collected", collected),
            ("Missed pickups", missed),
            ("Total load collected", f"{sacks} sacks and {kilos} kg"),
        ],
    }


def _deliveries(days, barangay_id) -> dict:
    columns = ["Date", "Time", "Truck", "Operator", "MRFs Included",
               "Barangays", "Sacks", "Kilos"]
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    operators = {u["id"]: u.get("full_name") for u in storage.read("users")}
    wanted = {timeutil.date_str(d) for d in days}

    rows, sacks, kilos = [], 0, 0
    for delivery in storage.read("deliveries"):
        if delivery.get("date") not in wanted:
            continue
        included = delivery.get("mrfs_included") or []
        if barangay_id and barangay_id not in included:
            continue
        load = delivery.get("load") or {}
        stamp = timeutil.parse_stamp(delivery.get("timestamp"))
        rows.append([
            delivery.get("date"),
            timeutil.display_time(stamp) if stamp else "—",
            delivery.get("truck_code") or "—",
            operators.get(delivery.get("operator_id")) or "—",
            len(included),
            ", ".join(names.get(b, b) for b in included) or "—",
            load.get("sacks", 0), load.get("kilos", 0),
        ])
        sacks += load.get("sacks", 0)
        kilos += load.get("kilos", 0)

    rows.sort(key=lambda r: (r[0], r[1]))
    return {
        "columns": columns, "rows": rows,
        "totals": {"trips": len(rows), "sacks": sacks, "kilos": kilos},
        "summary": [
            ("Trips to the landfill", len(rows)),
            ("Total delivered", f"{sacks} sacks and {kilos} kg"),
        ],
    }


def _carry_overs(days, barangay_id) -> dict:
    columns = ["First Missed", "Barangay", "Source Schedule Day", "Waste Type",
               "Original Truck", "Current Truck", "Rescheduled To", "Times Missed",
               "Status"]
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    wanted = {timeutil.date_str(d) for d in days}

    rows, pending, closed = [], 0, 0
    for row in storage.read("carry_overs"):
        if row.get("first_missed_date") not in wanted:
            continue
        if barangay_id and row.get("barangay_id") != barangay_id:
            continue
        rows.append([
            row.get("first_missed_date"),
            names.get(row.get("barangay_id"), "—"),
            row.get("source_schedule_day") or "—",
            row.get("waste_type") or "—",
            row.get("original_truck") or "—",
            row.get("current_truck") or "Not reassigned",
            row.get("reschedule_date") or "Not scheduled",
            row.get("missed_count", 1),
            row.get("status"),
        ])
        if row.get("status") == "Pending":
            pending += 1
        else:
            closed += 1

    rows.sort(key=lambda r: (r[0], r[1]))
    return {
        "columns": columns, "rows": rows,
        "totals": {"pending": pending, "closed": closed},
        "summary": [
            ("Carry-overs opened", len(rows)),
            ("Still pending", pending),
            ("Collected on a later run", closed),
        ],
    }


def _collector_performance(days, barangay_id) -> dict:
    columns = ["Collector", "Username", "Vehicle", "Barangay", "Days Worked",
               "Entries", "Collected", "Not Collected", "Sacks", "Kilos"]
    names = {b["id"]: b["name"] for b in storage.read("barangays")}
    users = {u["id"]: u for u in storage.read("users")}
    wanted = {timeutil.date_str(d) for d in days}

    buckets: dict[str, dict] = {}
    for entry in storage.read("collections"):
        if entry.get("date") not in wanted:
            continue
        collector_id = entry.get("collector_id")
        if not collector_id:
            continue        # a resident-created entry has no collector
        if barangay_id and entry.get("barangay_id") != barangay_id:
            continue

        bucket = buckets.setdefault(collector_id, {
            "days": set(), "entries": [], "collected": 0, "not_collected": 0})
        bucket["days"].add(entry["date"])
        bucket["entries"].append(entry)
        if entry.get("status") == collection_service.COLLECTED:
            bucket["collected"] += 1
        else:
            bucket["not_collected"] += 1

    rows = []
    for collector_id, bucket in buckets.items():
        user = users.get(collector_id) or {}
        load = collection_service.totals(bucket["entries"])
        rows.append([
            user.get("full_name") or "Deleted account",
            user.get("username") or "—",
            user.get("assigned_vehicle") or "—",
            names.get(user.get("assigned_barangay"), "—"),
            len(bucket["days"]), len(bucket["entries"]),
            bucket["collected"], bucket["not_collected"],
            load["sacks"], load["kilos"],
        ])

    rows.sort(key=lambda r: (-r[6], r[0]))     # most collected first
    return {
        "columns": columns, "rows": rows,
        "totals": {"collectors": len(rows)},
        "summary": [
            ("Collectors active in this range", len(rows)),
            ("Entries recorded", sum(r[5] for r in rows)),
            ("Total collected",
             f"{sum(r[8] for r in rows)} sacks and {sum(r[9] for r in rows)} kg"),
        ],
    }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def to_csv(report: dict) -> str:
    """
    The same rows as the printable page, as CSV.

    `\\r\\n` line endings and a header block: this is opened in Excel far more
    often than by a script, and a spreadsheet needs to know what it is looking
    at once the file is detached from the page that produced it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    meta = report["meta"]
    writer.writerow([f"{report['title']} Report"])
    writer.writerow(["Cabadbaran City Garbage Collection Tracking System"])
    writer.writerow(["Scope", meta["barangay"]])
    writer.writerow(["Date range", f"{meta['start_display']} to {meta['end_display']}"])
    writer.writerow(["Generated", meta["generated_display"]])
    writer.writerow([])

    writer.writerow(report["columns"])
    for row in report["rows"]:
        writer.writerow(row)

    if report.get("summary"):
        writer.writerow([])
        writer.writerow(["Summary"])
        for label, value in report["summary"]:
            writer.writerow([label, value])

    return buffer.getvalue()


def csv_filename(report: dict) -> str:
    meta = report["meta"]
    return f"{report['type']}_{meta['start']}_to_{meta['end']}.csv"
