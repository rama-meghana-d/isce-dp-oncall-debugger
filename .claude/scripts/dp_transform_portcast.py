#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for PORTCAST. Reads raw blob JSON from stdin."""
import sys, json, argparse

TRIGGER_MAP = {
    "ARRI": "VESSEL_ARRIVAL", "VESSEL_ARRIVAL": "VESSEL_ARRIVAL",
    "DEPA": "VESSEL_DEPARTURE", "VESSEL_DEPARTURE": "VESSEL_DEPARTURE",
    "LOAD": "LOADED_ON_VESSEL", "LOADED_ON_VESSEL": "LOADED_ON_VESSEL",
    "DISC": "UNLOADED_FROM_VESSEL", "UNLOADED_FROM_VESSEL": "UNLOADED_FROM_VESSEL",
    "GATE_IN": "GATE_IN", "GATE-IN": "GATE_IN", "GTIN": "GATE_IN",
    "GATE_OUT": "GATE_OUT", "GATE-OUT": "GATE_OUT", "GTOT": "GATE_OUT",
    "DELIVERY": "DELIVERY",
}
TIMING_MAP = {"ACTUAL": "ACTUAL", "ESTIMATED": "ESTIMATED", "PLANNED": "PLANNED", "PREDICTED": "PREDICTED"}
HOLD_TYPE_MAP = {"CUSTOMS": "IMPORT_CUSTOMS_ON_HOLD", "LINE": "LINE_HOLD"}


def transform(raw):
    payload = raw.get("payload", raw)
    shipments = payload.get("shipments", [payload])
    rows = []
    holds_found = []

    for si, s in enumerate(shipments):
        is_master = bool(s.get("isMaster", s.get("isMasterShipment", False)))
        bol       = s.get("billOfLading", s.get("bill_of_lading"))
        cmeta     = s.get("containerMetadata", s.get("container_metadata"))

        if is_master:
            rows.append({"shipment": si, "raw_trigger": "-", "timing": "-", "ts": "-",
                         "location": "-", "trigger": "-", "status": "DROPPED: master shipment"})
            continue
        if bol is None:
            rows.append({"shipment": si, "raw_trigger": "-", "timing": "-", "ts": "-",
                         "location": "-", "trigger": "-", "status": "DROPPED: null billOfLading"})
            continue
        if cmeta is None:
            rows.append({"shipment": si, "raw_trigger": "-", "timing": "-", "ts": "-",
                         "location": "-", "trigger": "-", "status": "DROPPED: null containerMetadata"})
            continue

        # holds → attributes, not dropped events
        for h in s.get("terminalApiImportPlanHolds", s.get("holds", [])):
            hold_type = h.get("type", h.get("holdType", "OTHER"))
            active    = h.get("active", h.get("isActive", "?"))
            attr = HOLD_TYPE_MAP.get(hold_type.upper(), "OTHER_HOLD")
            holds_found.append(f"shipment={si} holdType={hold_type} → attribute={attr} active={active}")

        for ei, e in enumerate(s.get("events", s.get("milestones", []))):
            raw_trig = e.get("eventType", e.get("type", "?"))
            timing   = TIMING_MAP.get(e.get("eventTiming", e.get("timing", "?")), e.get("eventTiming", "?"))
            ts       = e.get("eventDateTime", e.get("timestamp", "?"))
            loc      = e.get("locationCode", e.get("unLocode", "?"))
            vessel   = e.get("vesselName", "-"); voyage = e.get("voyageNumber", "-")

            trigger = TRIGGER_MAP.get(raw_trig)
            if trigger:
                status = "TRANSFORMED"
            else:
                status = f"DROPPED: unknown trigger '{raw_trig}'"
                trigger = "-"

            rows.append({
                "shipment": si, "raw_trigger": raw_trig, "timing": timing,
                "ts": str(ts)[:19], "location": loc, "vessel": vessel, "voyage": voyage,
                "trigger": trigger, "status": status,
            })
    return rows, holds_found, payload


def parse_legs(payload):
    out = []
    for s in payload.get("shipments", [payload]):
        for i, l in enumerate(s.get("transportLegs", s.get("legs", []))):
            dep = l.get("origin", l.get("from", "?")); arr = l.get("destination", l.get("to", "?"))
            vessel = l.get("vesselName", "?"); voyage = l.get("voyageNumber", "?")
            etd = l.get("etd", "?"); eta = l.get("eta", "?")
            out.append(f"Leg {i}: {dep}→{arr} | vessel={vessel} voyage={voyage} ETD={etd} ETA={eta}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, holds_found, payload = transform(raw)
    legs = parse_legs(payload)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"PORTCAST  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'S':<3} {'raw_trigger':<25} {'timing':<12} {'ts':<20} {'loc':<7} {'vessel':<18} {'voyage':<10} {'→trigger':<28} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['shipment']:<3} {r['raw_trigger']:<25} {r['timing']:<12} {r['ts']:<20} {r['location']:<7} {str(r.get('vessel','-')):<18} {str(r.get('voyage','-')):<10} {r['trigger']:<28} {r['status']}")

    if holds_found:
        print(f"\n── Import Holds (output as attributes, NOT milestone events) ──")
        for h in holds_found: print(" ", h)

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs: print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)} events")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = r["status"].split(":")[1].strip() if ":" in r["status"] else r["status"]
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items(): print(f"    - {reason}: {cnt}")
    if holds_found:
        print(f"  Holds (attributes): {len(holds_found)} (not part of milestone stream)")

    print(f"\n── Root Cause Guide ──")
    print("  Data absent from raw           → DATA PROVIDER did not send it")
    print("  DROPPED: null BL/metadata      → DAS TRANSFORMATION (PortCast metadata filter)")
    print("  Hold attributes present        → Check import hold status separately (not a milestone event)")
    print("  status=TRANSFORMED             → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
