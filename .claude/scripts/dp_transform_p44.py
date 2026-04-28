#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for P44_NEW / P44_ROAD_NEW. Reads raw blob JSON from stdin."""
import sys, json, argparse

TIMING_MAP = {
    "ACTUAL": "ACTUAL", "ESTIMATED": "ESTIMATED",
    "PLANNED": "PLANNED", "PREDICTED": "PREDICTED",
}
# P44 uses DCSA-like names; common mappings kept for reference
TRIGGER_MAP = {
    "ARRI": "VESSEL_ARRIVAL", "DEPA": "VESSEL_DEPARTURE",
    "LOAD": "LOADED_ON_VESSEL", "DISC": "UNLOADED_FROM_VESSEL",
    "GATE-IN": "GATE_IN", "GTIN": "GATE_IN",
    "GATE-OUT": "GATE_OUT", "GTOT": "GATE_OUT",
    "CUST-RELEASE": "CARRIER_RELEASE", "CUST-HOLD": "IMPORT_CUSTOMS_ON_HOLD",
    "DELIVERY": "DELIVERY",
}


def pick_timing_and_ts(e):
    if e.get("actualDateTime"):
        return "ACTUAL", e["actualDateTime"]
    if e.get("estimatedDateTime"):
        return "ESTIMATED", e["estimatedDateTime"]
    if e.get("plannedDateTime"):
        return "PLANNED", e["plannedDateTime"]
    t = e.get("eventTiming", "?")
    ts = e.get("eventDateTime", "?")
    return TIMING_MAP.get(t, t), ts


def transform(raw):
    payload = raw.get("payload", raw)
    shipments = payload.get("shipments", [payload])
    rows = []
    master_count = 0

    for si, s in enumerate(shipments):
        is_master = bool(s.get("isMasterShipment", s.get("master", False)))
        if is_master:
            master_count += 1
            rows.append({
                "shipment": si, "raw_trigger": "-", "raw_timing": "-",
                "source": "-", "ts": "-", "location": "-", "mode": "-",
                "trigger": "-", "timing": "-",
                "status": "DROPPED: master shipment filtered",
            })
            continue

        events = s.get("events", s.get("trackingEvents", []))
        for ei, e in enumerate(events):
            raw_trig = e.get("eventType", e.get("type", "?"))
            src      = e.get("source", e.get("sourceType", ""))
            loc      = e.get("location", {}).get("UNLocationCode", e.get("locationCode", "?"))
            mode     = e.get("transportMode", "-")
            timing, ts = pick_timing_and_ts(e)

            if src.upper() == "GEOFENCE":
                status = "DROPPED: geofence event (p44-exclude-geofence-events-enabled)"
                trigger = "-"
            elif timing == "?":
                status = "DROPPED: no timestamp/timing available"
                trigger = "-"
            else:
                trigger = TRIGGER_MAP.get(raw_trig, raw_trig)  # passthrough DCSA names
                status = "TRANSFORMED"

            rows.append({
                "shipment": si, "raw_trigger": raw_trig, "raw_timing": timing,
                "source": src or "-", "ts": str(ts)[:19], "location": loc,
                "mode": mode, "trigger": trigger, "timing": timing,
                "status": status,
            })
    return rows, payload, master_count


def parse_legs(payload):
    out = []
    for s in payload.get("shipments", [payload]):
        for i, l in enumerate(s.get("transportLegs", s.get("legs", []))):
            dep = l.get("originLocation", {}).get("UNLocationCode", l.get("from", "?"))
            arr = l.get("destinationLocation", {}).get("UNLocationCode", l.get("to", "?"))
            vessel = l.get("vessel", {}).get("name", "?")
            voyage = l.get("voyageNumber", "?")
            mode   = l.get("transportMode", "?")
            etd    = l.get("estimatedDeparture", l.get("etd", "?"))
            eta    = l.get("estimatedArrival", l.get("eta", "?"))
            out.append(f"Leg {i}: {dep}→{arr} | vessel={vessel} voyage={voyage} mode={mode} ETD={etd} ETA={eta}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    ap.add_argument("--dp-name", default="P44_NEW")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, payload, master_count = transform(raw)
    legs = parse_legs(payload)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"P44  dp={args.dp_name}  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events, {master_count} master shipments) ──")
        hdr = f"{'S':<3} {'raw_trigger':<28} {'timing':<12} {'source':<12} {'ts':<20} {'loc':<7} {'mode':<8} {'→trigger':<35} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['shipment']:<3} {r['raw_trigger']:<28} {r['raw_timing']:<12} {r['source']:<12} {r['ts']:<20} {r['location']:<7} {r['mode']:<8} {r['trigger']:<35} {r['status']}")

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs: print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)} events ({master_count} master-shipment rows)")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = r["status"].split(":")[1].strip() if ":" in r["status"] else r["status"]
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items(): print(f"    - {reason}: {cnt}")

    print(f"\n── Root Cause Guide ──")
    print("  Data absent from raw           → DATA PROVIDER did not send it")
    print("  DROPPED: geofence              → Expected; P44 geofence filter is ON")
    print("  DROPPED: master shipment       → Expected; master updates are not milestone events")
    print("  status=TRANSFORMED             → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
