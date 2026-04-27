#!/usr/bin/env python3
"""
Extract vessel name, voyage number, and event timestamps from IJ event references per ocean leg.

Vessel/voyage come exclusively from each event's references[] list — NOT from leg.vessel.
Use the printed '→ SV fallback:' lines to build the Fallback SV Port Call Query.

Usage:
  python3 scripts/parse_ij_vessel.py <filepath> <event_type>

  event_type: ETD | ATD | ETA | ATA | ETD,ATD | ETA,ATA | ALL

Examples:
  python3 scripts/parse_ij_vessel.py /path/to/file.txt ETD,ATD
  python3 scripts/parse_ij_vessel.py /path/to/file.txt ALL
  python3 scripts/parse_ij_vessel.py /path/to/file.txt ETA,ATA
"""

import json
import sys


# event_type → (trigger, timing) pairs to scan
_EVENT_TYPE_MAP = {
    "ETD": [("VESSEL_DEPARTURE", "ESTIMATED")],
    "ATD": [("VESSEL_DEPARTURE", "ACTUAL")],
    "ETA": [("VESSEL_ARRIVAL",   "ESTIMATED")],
    "ATA": [("VESSEL_ARRIVAL",   "ACTUAL")],
}


def resolve_event_specs(event_type_arg):
    """Return list of (trigger, timing, label) tuples to scan."""
    parts = [p.strip().upper() for p in event_type_arg.split(",")]
    if "ALL" in parts or set(parts) == {"ETD", "ATD", "ETA", "ATA"}:
        return [
            ("VESSEL_DEPARTURE", "ESTIMATED", "ETD"),
            ("VESSEL_DEPARTURE", "ACTUAL",    "ATD"),
            ("VESSEL_ARRIVAL",   "ESTIMATED", "ETA"),
            ("VESSEL_ARRIVAL",   "ACTUAL",    "ATA"),
        ]
    specs = []
    seen = set()
    for p in parts:
        for trigger, timing in _EVENT_TYPE_MAP.get(p, []):
            key = (trigger, timing)
            if key not in seen:
                specs.append((trigger, timing, p))
                seen.add(key)
    return specs


def is_ocean_leg(leg):
    tm = leg.get("transportMode")
    if isinstance(tm, dict):
        return tm.get("transportModeEnum") == "OCEAN"
    return tm == "OCEAN"


def get_loc_unloc(loc_obj):
    if not loc_obj:
        return "-"
    inner = loc_obj.get("location") or {}
    return inner.get("unLocCode") or loc_obj.get("unLocCode") or "-"


def ref_value(references, type_enum):
    for r in (references or []):
        if r.get("referenceTypeEnum") == type_enum:
            return r.get("reference") or "-"
    return None


def parent_conn_exists(event_connections):
    """Check parentEventConnectionExists on the VESSEL_NAME entry."""
    for ec in (event_connections or []):
        if ec.get("messageLevel") == "VESSEL_NAME":
            return ec.get("parentEventConnectionExists", False)
    return False


def find_event(events, trigger, timing):
    for e in (events or []):
        if e.get("eventTrigger") == trigger and e.get("eventTiming") == timing:
            return e
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: parse_ij_vessel.py <filepath> <event_type>")
        print("  event_type: ETD | ATD | ETA | ATA | ETD,ATD | ETA,ATA | ALL")
        sys.exit(1)

    filepath = sys.argv[1]
    event_type_arg = sys.argv[2]
    specs = resolve_event_specs(event_type_arg)

    if not specs:
        print(f"Unknown event_type: {event_type_arg}")
        sys.exit(1)

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
        journey = json.loads(journey_raw) if isinstance(journey_raw, str) else journey_raw

        status = journey.get("status") or "-"
        print(f"=== {unit}  updated: {updated_at}  status: {status} ===")
        print()

        legs = journey.get("shipmentJourneyLegs") or journey.get("transportLegs") or []
        ocean_legs = [l for l in legs if is_ocean_leg(l)]

        if not ocean_legs:
            print("  [no OCEAN legs found in journey]")
            print()
            continue

        for i, leg in enumerate(ocean_legs):
            seq = leg.get("sequence") or leg.get("sequenceNumber") or (i + 1)
            start_unloc = get_loc_unloc(leg.get("startLocation"))
            end_unloc   = get_loc_unloc(leg.get("endLocation"))
            events = leg.get("events") or []

            print(f"leg {seq}  start: {start_unloc}  end: {end_unloc}")

            # Per-spec: find the matching event and extract vessel/voyage from its references
            fallback_by_trigger = {}   # trigger → (vessel, voyage, unloc) for the → SV fallback line

            for trigger, timing, label in specs:
                evt = find_event(events, trigger, timing)
                if evt is None:
                    print(f"  {label:<4}  [no {trigger} {timing} event on this leg]")
                    continue

                refs    = evt.get("references") or []
                conns   = evt.get("eventConnections") or []
                vessel  = ref_value(refs, "VESSEL_NAME")
                voyage  = ref_value(refs, "VOYAGE_NUMBER")
                imo     = ref_value(refs, "VESSEL_IMO")
                ts      = evt.get("timestamp") or "-"
                sv_link = "YES" if parent_conn_exists(conns) else "NO"

                vessel_disp = vessel or "-"
                voyage_disp = voyage or "-"
                imo_disp    = imo    or "-"

                print(
                    f"  {label:<4}  vessel: {vessel_disp:<22}  voyage: {voyage_disp:<14}"
                    f"  imo: {imo_disp:<8}  ts: {ts:<32}  sv_link: {sv_link}"
                )

                # Record for fallback line — one per trigger (deduplicated)
                if trigger not in fallback_by_trigger and vessel and voyage:
                    unloc = start_unloc if trigger == "VESSEL_DEPARTURE" else end_unloc
                    fallback_by_trigger[trigger] = (vessel, voyage, unloc, trigger)

            # Print → SV fallback lines
            for trigger, (vessel, voyage, unloc, trig) in sorted(fallback_by_trigger.items()):
                print(
                    f"  → SV fallback: vessel='{vessel}'  voyage='{voyage}'"
                    f"  unloc='{unloc}'  trigger={trig}"
                )

            if not fallback_by_trigger:
                print("  → SV fallback: [no vessel/voyage found in event references for this leg]")

            print()

        print()


if __name__ == "__main__":
    main()
