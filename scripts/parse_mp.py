#!/usr/bin/env python3
"""


Parse a saved milestone (Milestone Processor) MCP result file.

Usage:
  python3 scripts/parse_mp.py <filepath> <trigger> <timing> [un_loc_code]

  trigger : VESSEL_DEPARTURE | VESSEL_ARRIVAL | ALL
  timing  : ESTIMATED | ACTUAL | ALL
  un_loc_code : optional — filter events to this UNLOC only

Examples:
  python3 scripts/parse_mp.py /path/to/file.txt VESSEL_DEPARTURE ESTIMATED
  python3 scripts/parse_mp.py /path/to/file.txt ALL ALL INNSA
  python3 scripts/parse_mp.py /path/to/file.txt VESSEL_DEPARTURE ACTUAL
"""

import json
import sys


def get_unloc(location):
    """Extract UNLOC from an event location entry (handles multiple schemas)."""
    if not location:
        return "-"
    # Try nested .location.unLocCode (IJ-style)
    inner = location.get("location") or {}
    if inner.get("unLocCode"):
        return inner["unLocCode"]
    # Try postalAddress.locationCode (DUST/MP style)
    postal = location.get("postalAddress") or {}
    if postal.get("locationCode"):
        return postal["locationCode"]
    # Try locationDetail.value
    detail = location.get("locationDetail") or {}
    if detail.get("value"):
        return detail["value"]
    return location.get("unLocCode") or "-"


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
    src = event.get("source") or event.get("dataProviderName") or "-"
    return (
        f"    {event.get('eventTrigger', '?'):<25} | "
        f"{event.get('eventTiming', '?'):<10} | "
        f"{event.get('timestamp', '-'):<30} | "
        f"loc:{unloc}  src:{src}"
    )


def main():
    if len(sys.argv) < 4:
        print("Usage: parse_mp.py <filepath> <trigger> <timing> [un_loc_code]")
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

    # Deduplicate by job_id — same job_id appears multiple times (one per journey_leg_id)
    seen_jobs = {}
    for row in rows:
        job_id = row.get("job_id", "-")
        updated_at = row.get("updated_at", "-")
        if job_id not in seen_jobs or updated_at > seen_jobs[job_id]["updated_at"]:
            seen_jobs[job_id] = row

    total_matching = 0

    for job_id, row in sorted(seen_jobs.items(), key=lambda x: x[1].get("updated_at", ""), reverse=True):
        unit = row.get("unit_of_tracking", "-")
        updated_at = row.get("updated_at", "-")

        enriched_raw = row.get("enriched_data") or "{}"
        if isinstance(enriched_raw, str):
            enriched = json.loads(enriched_raw)
        else:
            enriched = enriched_raw

        # MP enriched_data is a flat dict: key = "{job_id}_{trigger}_{timing}_{unloc}_{source}_"
        # value = event object with eventTrigger, eventTiming, timestamp, locations[]
        matching = []
        if isinstance(enriched, dict):
            for key, val in enriched.items():
                if isinstance(val, dict) and "eventTrigger" in val:
                    if event_matches(val, trigger_filter, timing_filter, unloc_filter):
                        matching.append(val)

        print(f"job_id: {job_id}  unit: {unit}  updated_at: {updated_at}")

        if matching:
            for e in sorted(matching, key=lambda x: (x.get("eventTrigger", ""), x.get("eventTiming", ""))):
                print(format_event(e))
            total_matching += len(matching)
        else:
            desc = f"{trigger_filter} {timing_filter}"
            unloc_note = f" at {unloc_filter}" if unloc_filter else ""
            print(f"    [no {desc} events{unloc_note}]")
        print("---")

    if total_matching == 0:
        trigger_desc = trigger_filter if trigger_filter != "ALL" else "matching"
        print(f"\n[SUMMARY: no {trigger_desc} {timing_filter} events found across {len(seen_jobs)} job(s)]")
    else:
        print(f"\n[SUMMARY: {total_matching} matching event(s) across {len(seen_jobs)} job(s)]")


if __name__ == "__main__":
    main()
