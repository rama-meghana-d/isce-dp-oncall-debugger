#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for BLUEBOX (air mode). Reads raw blob JSON from stdin."""
import sys, json, argparse

TRIGGER_MAP = {
    "DEP": "VESSEL_DEPARTURE", "ATD": "VESSEL_DEPARTURE",
    "DEPARTURE": "VESSEL_DEPARTURE",
    "ARR": "VESSEL_ARRIVAL", "ATA": "VESSEL_ARRIVAL",
    "ARRIVAL": "VESSEL_ARRIVAL",
    "DLV": "DELIVERY", "DELIVERY": "DELIVERY",
    "PUP": "GATE_OUT", "PICK-UP": "GATE_OUT",
    "RCS": "GATE_IN", "RECEIVED": "GATE_IN",
    "MAN": "LOADED_ON_VESSEL",  # manifested
    "FOH": "GATE_OUT",          # freight on hand
}
TIMING_MAP = {"ACTUAL": "ACTUAL", "ESTIMATED": "ESTIMATED", "PLANNED": "PLANNED"}


def transform(raw):
    payload = raw.get("payload", raw)
    mawb_info = payload.get("mawbInfo", payload.get("mawb_info"))
    shipments = payload.get("shipments", [])
    rows = []

    if mawb_info is None:
        return rows, None, shipments, []

    if not shipments:
        return rows, mawb_info, [], []

    for si, s in enumerate(shipments):
        hawb = s.get("hawbNumber", s.get("hawb", "-"))
        gran = s.get("granularity", s.get("trackingUnit"))
        container = s.get("containerNumber", s.get("container", "-"))

        if not gran:
            rows.append({
                "shipment": si, "hawb": hawb, "raw_status": "-", "timing": "-",
                "ts": "-", "dep": "-", "arr": "-", "trigger": "-",
                "status": "DROPPED: invalid/null granularity",
            })
            continue

        movements = s.get("movements", s.get("events", []))
        if not movements:
            rows.append({
                "shipment": si, "hawb": hawb, "raw_status": "-", "timing": "-",
                "ts": "-", "dep": "-", "arr": "-", "trigger": "-",
                "status": "DROPPED: no movements/events in shipment",
            })
            continue

        for mi, m in enumerate(movements):
            raw_status = m.get("status", m.get("eventTrigger", m.get("type", "?")))
            timing_raw = m.get("eventTiming", m.get("timing", "ACTUAL"))
            ts   = m.get("eventDateTime", m.get("timestamp", m.get("date", "?")))
            dep  = m.get("departureIata", m.get("departure", "-"))
            arr  = m.get("arrivalIata", m.get("arrival", "-"))
            airline = m.get("airline", "-"); flight = m.get("flightNumber", m.get("flight", "-"))

            timing = TIMING_MAP.get(timing_raw.upper() if timing_raw else "", timing_raw)
            trigger = TRIGGER_MAP.get((raw_status or "").upper())
            if trigger:
                status = "TRANSFORMED"
            else:
                status = f"DROPPED: unknown movement type '{raw_status}'"
                trigger = "-"

            rows.append({
                "shipment": si, "hawb": hawb, "raw_status": raw_status,
                "timing": timing, "ts": str(ts)[:19], "dep": dep, "arr": arr,
                "airline": airline, "flight": flight,
                "trigger": trigger, "status": status,
            })
    return rows, mawb_info, shipments, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, mawb_info, shipments, _ = transform(raw)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"BLUEBOX  job={args.job_id}  container={args.container}  (AIR mode)")
    print(f"{'='*70}")

    if mawb_info is None:
        print("NULL mawbInfo — ENTIRE PAYLOAD DROPPED. No events produced.")
        print("\n── Root Cause Guide ──")
        print("  NULL mawbInfo → DATA PROVIDER sent payload without MAWB info")
        return

    mawb_num = mawb_info.get("mawbNumber", mawb_info.get("number", "?"))
    origin   = mawb_info.get("origin", "?"); dest = mawb_info.get("destination", "?")
    airline  = mawb_info.get("airline", "?")
    print(f"MAWB: {mawb_num}  {origin}→{dest}  airline={airline}  shipments={len(shipments)}")

    if not shipments:
        print("EMPTY shipments list — DROPPED.")
        return

    if not rows:
        print("NO MOVEMENTS found in any shipment.")
    else:
        print(f"\n── Transformation Table ({len(rows)} movements) ──")
        hdr = f"{'S':<3} {'HAWB':<14} {'raw_status':<18} {'timing':<10} {'ts':<20} {'dep':<6} {'arr':<6} {'airline':<10} {'flight':<10} {'→trigger':<28} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['shipment']:<3} {r['hawb']:<14} {r['raw_status']:<18} {r['timing']:<10} {r['ts']:<20} {r['dep']:<6} {r['arr']:<6} {r.get('airline','-'):<10} {r.get('flight','-'):<10} {r['trigger']:<28} {r['status']}")

    # MAWB routing
    segs = mawb_info.get("flightSegments", mawb_info.get("routing", []))
    if segs:
        print(f"\n── MAWB Flight Segments ──")
        for i, seg in enumerate(segs):
            print(f"  Seg {i}: {seg.get('origin','?')}→{seg.get('destination','?')} flight={seg.get('flightNumber','?')} ETD={seg.get('etd','?')} ETA={seg.get('eta','?')}")

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)} movements across {len(shipments)} shipments")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = r["status"].split(":")[1].strip() if ":" in r["status"] else r["status"]
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items(): print(f"    - {reason}: {cnt}")

    print(f"\n── Root Cause Guide ──")
    print("  BLUEBOX is AIR mode only — does not cover ocean containers")
    print("  NULL mawbInfo / empty shipments  → DATA PROVIDER sent incomplete payload")
    print("  DROPPED: invalid granularity     → DATA PROVIDER missing HAWB/tracking unit")
    print("  DROPPED: unknown movement type   → DAS TRANSFORMATION (unmapped Bluebox status)")
    print("  status=TRANSFORMED               → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
