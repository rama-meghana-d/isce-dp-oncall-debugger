#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for SUBSCRIPTIONLESS variants.
Handles: SUBSCRIPTIONLESS, SUBSCRIPTIONLESS_BOL, SUBSCRIPTIONLESS_BOLCONTAINER, SUBSCRIPTIONLESS_CBCN.
Reads raw blob JSON from stdin."""
import sys, json, argparse

GRANULARITY_PRIORITY = ["CONTAINER_NUMBER", "BILL_OF_LADING", "AIR_WAY_BILL_NUMBER", "CARRIER_BOOKING_NUMBER"]

# Triggers that are allowed to carry vessel references
VESSEL_REF_TRIGGERS = {
    "LOADED_ON_VESSEL", "UNLOADED_FROM_VESSEL", "VESSEL_ARRIVAL", "VESSEL_DEPARTURE",
    "LOADED_ON_BARGE", "UNLOADED_FROM_BARGE", "BARGE_ARRIVAL", "BARGE_DEPARTURE",
    "LOADED_ON_RAIL", "UNLOADED_FROM_A_RAIL_CAR",
    "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP", "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
}


def get_granularity(identifiers):
    types = {i.get("referenceType", i.get("type", "")): i.get("referenceValue", i.get("value", "?")) for i in identifiers}
    for p in GRANULARITY_PRIORITY:
        if p in types:
            return p, types[p]
    return "UNKNOWN", "?"


def transform(raw):
    payload = raw.get("payload", raw)
    identifiers = payload.get("identifiers", [])
    gran_type, gran_val = get_granularity(identifiers)

    # Events may be at top level or grouped per container
    raw_events = payload.get("events", [])
    if not raw_events:
        # try nested structure
        for key in ("eventsByContainer", "shipments"):
            grouped = payload.get(key, {})
            if isinstance(grouped, dict):
                for v in grouped.values():
                    raw_events.extend(v.get("events", []) if isinstance(v, dict) else v if isinstance(v, list) else [])
            elif isinstance(grouped, list):
                for s in grouped:
                    raw_events.extend(s.get("events", []) if isinstance(s, dict) else [])
            if raw_events:
                break

    rows = []
    for i, e in enumerate(raw_events):
        trig   = e.get("eventTrigger", e.get("type", "?"))
        timing = e.get("eventTiming", e.get("timing", "?"))
        ts     = e.get("timestamp", e.get("eventDateTime", "?"))
        loc    = e.get("locationCode", e.get("unLocode", "?"))
        vessel_in_raw = e.get("vesselName", "-")
        voyage_in_raw = e.get("voyageNumber", "-")
        mode   = e.get("transportMode", "-")

        vessel_ref_included = trig in VESSEL_REF_TRIGGERS
        vessel_note = "included" if vessel_ref_included else "STRIPPED (trigger not in vessel-approved list)"

        rows.append({
            "i": i, "raw_trigger": trig, "timing": timing, "ts": str(ts)[:19],
            "location": loc, "vessel_raw": vessel_in_raw, "voyage_raw": voyage_in_raw,
            "mode": mode, "trigger": trig,  # passthrough
            "vessel_ref": vessel_note, "status": "TRANSFORMED",
        })
    return rows, gran_type, gran_val, identifiers, payload


def parse_legs(payload):
    legs = payload.get("transportLegs", payload.get("legs",
           payload.get("transportPlan", {}).get("legs", [])))
    out = []
    for i, l in enumerate(legs):
        dep = l.get("departureLocation", l.get("from", "?"))
        arr = l.get("arrivalLocation", l.get("to", "?"))
        vessel = l.get("vesselName", "?"); voyage = l.get("voyageNumber", "?")
        etd = l.get("etd", "?"); eta = l.get("eta", "?"); mode = l.get("transportMode", "?")
        out.append(f"Leg {i}: {dep}→{arr} | vessel={vessel} voyage={voyage} mode={mode} ETD={etd} ETA={eta}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    ap.add_argument("--dp-name", default="SUBSCRIPTIONLESS")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, gran_type, gran_val, identifiers, payload = transform(raw)
    legs = parse_legs(payload)
    stripped = [r for r in rows if "STRIPPED" in r["vessel_ref"]]

    print(f"\n{'='*70}")
    print(f"{args.dp_name}  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")
    print(f"Granularity key : {gran_type} = {gran_val}")
    print(f"Identifiers     : {[i.get('referenceType', i.get('type','?')) for i in identifiers]}")
    print("Transformation  : passthrough (no trigger/timing remapping)")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'#':<3} {'raw_trigger':<38} {'timing':<12} {'ts':<20} {'loc':<7} {'vessel(raw)':<20} {'vessel_ref':<12} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            vref_short = "included" if r["vessel_ref"] == "included" else "STRIPPED"
            print(f"{r['i']:<3} {r['raw_trigger']:<38} {r['timing']:<12} {r['ts']:<20} {r['location']:<7} {str(r['vessel_raw']):<20} {vref_short:<12} {r['status']}")

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs: print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent  : {len(rows)} events")
    print(f"  Transformed    : {len(rows)} (passthrough — all kept)")
    print(f"  Vessel refs stripped: {len(stripped)} events (trigger not in vessel-approved list)")
    if stripped:
        for r in stripped: print(f"    - [{r['i']}] {r['raw_trigger']}")

    print(f"\n── Root Cause Guide ──")
    print("  Subscriptionless events are pre-transformed by the carrier before reaching ISCE")
    print("  Wrong eventTrigger in raw  → DATA PROVIDER (carrier) sent incorrect trigger")
    print("  Vessel ref missing         → Expected if trigger not in vessel-approved list (by design)")
    print("  Event absent from raw      → Carrier feed did not include it")
    print("  status=TRANSFORMED         → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
