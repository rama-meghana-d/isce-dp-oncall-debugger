#!/usr/bin/env python3
"""
Parse a saved intelligent_journey MCP result file.

Usage:
  python3 scripts/parse_ij.py <filepath> <trigger> <timing> [un_loc_code]

  trigger : VESSEL_DEPARTURE | VESSEL_ARRIVAL | ALL
  timing  : ESTIMATED | ACTUAL | ALL
  un_loc_code : optional — filter events to this UNLOC only

Examples:
  python3 scripts/parse_ij.py /path/to/file.txt VESSEL_DEPARTURE ESTIMATED
  python3 scripts/parse_ij.py /path/to/file.txt ALL ALL INNSA
  python3 scripts/parse_ij.py /path/to/file.txt VESSEL_DEPARTURE ALL
"""

import json
import sys


def get_unloc(location):
    """Extract UNLOC from an event location entry."""
    if not location:
        return "-"
    # IJ confirmed path: location.unLocCode (nested one level)
    inner = location.get("location") or {}
    return (
        inner.get("unLocCode")
        or location.get("unLocCode")
        or (location.get("facility") or {}).get("postalAddress", {}).get("locationCode")
        or "-"
    )


def event_matches(event, trigger_filter, timing_filter, unloc_filter):
    if trigger_filter != "ALL" and event.get("eventTrigger") != trigger_filter:
        return False
    if timing_filter != "ALL" and event.get("eventTiming") != timing_filter:
        return False
    if unloc_filter:
        locs = event.get("locations") or []
        event_unlocs = [get_unloc(l) for l in locs]
        if unloc_filter not in event_unlocs:
            return False
    return True


def format_event(event):
    locs = event.get("locations") or []
    unloc = get_unloc(locs[0]) if locs else "-"
    return (
        f"  {event.get('eventTrigger', '?'):<25} | "
        f"{event.get('eventTiming', '?'):<10} | "
        f"{event.get('timestamp', '-'):<30} | "
        f"{unloc}"
    )


def get_loc_unloc(loc_obj):
    """Extract unLocCode from a startLocation/endLocation object (nested under .location)."""
    if not loc_obj:
        return "-"
    inner = loc_obj.get("location") or {}
    return inner.get("unLocCode") or loc_obj.get("unLocCode") or "-"


def is_ocean_leg(leg):
    tm = leg.get("transportMode")
    if isinstance(tm, dict):
        return tm.get("transportModeEnum") == "OCEAN"
    return tm == "OCEAN"


def main():
    if len(sys.argv) < 4:
        print("Usage: parse_ij.py <filepath> <trigger> <timing> [un_loc_code]")
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

    for row in rows:
        unit = row.get("unit_of_tracking", "-")
        updated_at = row.get("updated_at", "-")

        journey_raw = row.get("journey") or "{}"
        if isinstance(journey_raw, str):
            journey = json.loads(journey_raw)
        else:
            journey = journey_raw

        status = journey.get("status", "-")
        print(f"unit_of_tracking : {unit}")
        print(f"updated_at       : {updated_at}")
        print(f"status           : {status}")
        print("---")

        # IJ uses shipmentJourneyLegs (confirmed); fall back to transportLegs
        legs = journey.get("shipmentJourneyLegs") or journey.get("transportLegs") or []
        ocean_legs = [l for l in legs if is_ocean_leg(l)]

        if not ocean_legs:
            print("  [no OCEAN legs found in journey]")
            continue

        for i, leg in enumerate(ocean_legs):
            seq = leg.get("sequence") or leg.get("sequenceNumber") or (i + 1)
            start_unloc = get_loc_unloc(leg.get("startLocation"))
            end_unloc = get_loc_unloc(leg.get("endLocation"))
            vessel_obj = leg.get("vessel") or {}
            vessel = vessel_obj.get("vesselName") or "-"
            voyage = vessel_obj.get("voyageNumber") or "-"

            events = leg.get("events") or []
            matching = [e for e in events if event_matches(e, trigger_filter, timing_filter, unloc_filter)]

            print(f"leg_seq: {seq}  mode: OCEAN  start: {start_unloc}  end: {end_unloc}")
            print(f"  vessel: {vessel}  voyage: {voyage}")

            if matching:
                for e in matching:
                    print(format_event(e))
            else:
                desc = f"{trigger_filter} {timing_filter}"
                unloc_note = f" at {unloc_filter}" if unloc_filter else ""
                print(f"  [no {desc} events{unloc_note}]")
        print()


if __name__ == "__main__":
    main()
