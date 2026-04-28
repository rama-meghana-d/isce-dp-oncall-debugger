#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for CONEKSION. Reads raw blob JSON from stdin.
Critical: any shipment with UNMAPPED_EVENT type causes the ENTIRE shipment to be dropped."""
import sys, json, argparse

VALID_TRIGGERS = {
    "GATE_IN", "GATE_OUT", "LOADED_ON_VESSEL", "UNLOADED_FROM_VESSEL",
    "VESSEL_ARRIVAL", "VESSEL_DEPARTURE", "DELIVERY", "CARRIER_RELEASE",
    "IMPORT_CUSTOMS_ON_HOLD", "IMPORT_CUSTOMS_CLEARED", "EMPTY_CONTAINER_RETURNED",
    "LOADED_ON_RAIL", "UNLOADED_FROM_A_RAIL_CAR",
    "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP", "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    "BARGE_ARRIVAL", "BARGE_DEPARTURE", "LOADED_ON_BARGE", "UNLOADED_FROM_BARGE",
    "STRIPPING", "STUFFING", "ARRIVED_AT_CUSTOMER_LOCATION", "EXIT_FACILITY",
}


def transform(raw):
    payload = raw.get("payload", raw)
    shipments = payload.get("shipments", [payload])
    rows = []

    for si, s in enumerate(shipments):
        events = s.get("events", [])
        container_num = s.get("containerNumber", s.get("container_number", f"shipment_{si}"))

        # check for UNMAPPED_EVENT — drops entire shipment
        unmapped = [e for e in events if (e.get("type") or "").upper() == "UNMAPPED_EVENT"]
        if unmapped:
            for ei, e in enumerate(events):
                trig  = e.get("eventTrigger", e.get("type", "?"))
                timing = e.get("eventTiming", "?")
                ts    = e.get("timestamp", e.get("eventDateTime", "?"))
                loc   = e.get("locationCode", "?")
                etype = (e.get("type") or "?").upper()
                marker = " ← UNMAPPED_EVENT TRIGGER" if etype == "UNMAPPED_EVENT" else ""
                rows.append({
                    "shipment": si, "container": container_num,
                    "raw_trigger": trig, "timing": timing, "ts": str(ts)[:19],
                    "location": loc, "event_type": etype, "trigger": "-",
                    "status": f"DROPPED: UNMAPPED_EVENT in shipment (all {len(events)} events dropped){marker}",
                })
            continue

        for ei, e in enumerate(events):
            trig   = e.get("eventTrigger", e.get("type", "?"))
            timing = e.get("eventTiming", "?")
            ts     = e.get("timestamp", e.get("eventDateTime", "?"))
            loc    = e.get("locationCode", "?")
            vessel = e.get("vesselName", "-"); voyage = e.get("voyageNumber", "-")
            etype  = (e.get("type") or "").upper()

            if trig not in VALID_TRIGGERS:
                status = f"DROPPED: trigger '{trig}' not in Milestone enum"
                out_trig = "-"
            else:
                status = "TRANSFORMED"
                out_trig = trig

            rows.append({
                "shipment": si, "container": container_num,
                "raw_trigger": trig, "timing": timing, "ts": str(ts)[:19],
                "location": loc, "event_type": etype, "trigger": out_trig,
                "vessel": vessel, "voyage": voyage, "status": status,
            })
    return rows, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, payload = transform(raw)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]
    unmapped_shipments = len({r["shipment"] for r in rows if "UNMAPPED_EVENT in shipment" in r["status"]})

    print(f"\n{'='*70}")
    print(f"CONEKSION  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")
    if unmapped_shipments:
        print(f"⚠  {unmapped_shipments} shipment(s) dropped entirely due to UNMAPPED_EVENT")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'S':<3} {'container':<16} {'eventType':<18} {'raw_trigger':<28} {'timing':<10} {'ts':<20} {'loc':<7} {'→trigger':<28} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['shipment']:<3} {r['container']:<16} {r['event_type']:<18} {r['raw_trigger']:<28} {r['timing']:<10} {r['ts']:<20} {r['location']:<7} {r['trigger']:<28} {r['status']}")

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)} events across {len({r['shipment'] for r in rows})} shipments")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if unmapped_shipments:
        print(f"    - Full shipment drop (UNMAPPED_EVENT): {unmapped_shipments} shipments")
    invalid = [r for r in dropped if "Milestone enum" in r["status"]]
    if invalid: print(f"    - Invalid trigger (not in Milestone enum): {len(invalid)}")

    print(f"\n── Root Cause Guide ──")
    print("  DROPPED: UNMAPPED_EVENT in shipment → Coneksion could not map carrier's event")
    print("    → Coneksion data quality issue or new event type not yet mapped by Coneksion")
    print("    → Escalate to Coneksion team to add mapping for the unknown event type")
    print("  DROPPED: invalid trigger            → DAS TRANSFORMATION (Coneksion sent unknown trigger)")
    print("  status=TRANSFORMED                  → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
