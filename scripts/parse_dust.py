#!/usr/bin/env python3
"""
Parse a saved milestonepipeline (DUST) MCP result file.

Usage:
  python3 scripts/parse_dust.py <filepath> <trigger> <timing> [un_loc_code]

  trigger : VESSEL_DEPARTURE | VESSEL_ARRIVAL | ALL
  timing  : ESTIMATED | ACTUAL | ALL
  un_loc_code : optional — filter events to this UNLOC only

Examples:
  python3 scripts/parse_dust.py /path/to/file.txt VESSEL_DEPARTURE ESTIMATED
  python3 scripts/parse_dust.py /path/to/file.txt ALL ALL INNSA
  python3 scripts/parse_dust.py /path/to/file.txt VESSEL_DEPARTURE ACTUAL
"""

import json
import sys


def get_unloc(location):
    if not location:
        return "-"
    # DUST confirmed path: facility.postalAddress.locationCode or facility.locationDetail.value
    facility = location.get("facility") or {}
    postal = facility.get("postalAddress") or location.get("postalAddress") or {}
    detail = facility.get("locationDetail") or location.get("locationDetail") or {}
    return (
        postal.get("locationCode")
        or detail.get("value")
        or location.get("unLocCode")
        or location.get("locationCode")
        or "-"
    )


def event_matches(event, trigger_filter, timing_filter, unloc_filter):
    if trigger_filter != "ALL" and event.get("eventTrigger") != trigger_filter:
        return False
    if timing_filter != "ALL" and event.get("eventTiming") != timing_filter:
        return False
    if unloc_filter:
        locs = event.get("locations") or []
        if unloc_filter not in [get_unloc(l) for l in locs]:
            return False
    return True


def format_event(event):
    locs = event.get("locations") or []
    unloc = get_unloc(locs[0]) if locs else "-"
    ts = event.get("timestamp") or "-"
    return (
        f"    {event.get('eventTrigger', '?'):<25} | "
        f"{event.get('eventTiming', '?'):<10} | "
        f"{ts:<30} | "
        f"{unloc}"
    )


def main():
    if len(sys.argv) < 4:
        print("Usage: parse_dust.py <filepath> <trigger> <timing> [un_loc_code]")
        sys.exit(1)

    filepath = sys.argv[1]
    trigger_filter = sys.argv[2].upper()
    timing_filter = sys.argv[3].upper()
    unloc_filter = sys.argv[4].upper() if len(sys.argv) > 4 else None

    with open(filepath, encoding="utf-8") as f:
        outer = json.load(f)

    rows = outer.get("rows", [])
    if not rows:
        print("No rows in result.")
        return

    total_matching = 0

    for row in rows:
        pipeline_id = row.get("pipeline_id", "-")
        job_id = row.get("job_id", "-")
        updated_at = row.get("updated_at", "-")

        raw = row.get("raw_milestone") or "{}"
        if isinstance(raw, str):
            payload = json.loads(raw)
        else:
            payload = raw

        # DUST structure: {"shipments": [{"events": [...]}]}
        all_events = []
        for shipment in (payload.get("shipments") or []):
            all_events.extend(shipment.get("events") or [])

        matching = [e for e in all_events if event_matches(e, trigger_filter, timing_filter, unloc_filter)]

        print(f"pipeline_id: {pipeline_id}")
        print(f"  job_id: {job_id}  updated_at: {updated_at}")

        if matching:
            for e in matching:
                print(format_event(e))
            total_matching += len(matching)
        else:
            desc = f"{trigger_filter} {timing_filter}"
            unloc_note = f" at {unloc_filter}" if unloc_filter else ""
            print(f"    [no {desc} events{unloc_note}]")
        print("---")

    if total_matching == 0:
        trigger_desc = trigger_filter if trigger_filter != "ALL" else "matching"
        print(f"\n[SUMMARY: no {trigger_desc} {timing_filter} events found across {len(rows)} row(s)]")
    else:
        print(f"\n[SUMMARY: {total_matching} matching event(s) across {len(rows)} row(s)]")


if __name__ == "__main__":
    main()
