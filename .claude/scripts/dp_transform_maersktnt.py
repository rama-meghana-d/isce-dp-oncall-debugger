#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for MAERSKTNT. Reads raw blob JSON from stdin."""
import sys, json, argparse

TRIGGER_MAP = {
    "CONTAINER ARRIVAL":            "VESSEL_ARRIVAL",
    "CONTAINER DEPARTURE":          "VESSEL_DEPARTURE",
    "CONTAINER RETURN":             "EMPTY_CONTAINER_RETURNED",
    "DISCHARG":                     "UNLOADED_FROM_VESSEL",
    "GATE-IN":                      "GATE_IN",          # overridden if isEmpty
    "GATE-OUT":                     "GATE_OUT",         # overridden if isEmpty
    "CUSTOMER_GATE_IN":             "GATE_IN",
    "CUSTOMER_GATE_OUT":            "GATE_OUT",
    "LOAD":                         "LOADED_ON_VESSEL",
    "OFF-RAIL":                     "UNLOADED_FROM_A_RAIL_CAR",
    "ON-RAIL":                      "LOADED_ON_RAIL",
    "RAIL_ARRIVAL_AT_DESTINATION":  "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP",
    "RAIL_DEPARTURE":               "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    "ARRIVECU":                     "ARRIVED_AT_CUSTOMER_LOCATION",
    "DEPARTCU":                     "EXIT_FACILITY",
    "STRIPPIN":                     "STRIPPING",
    "STUFFING":                     "STUFFING",
    "BARGE_ARRIVAL":                "BARGE_ARRIVAL",
    "BARGE_DEPARTURE":              "BARGE_DEPARTURE",
}
TIMING_MAP = {
    "ACTUAL": "ACTUAL", "ESTIMATED": "ESTIMATED",
    "PLANNED": "PLANNED", "EXPECTED": "ESTIMATED",
}


def get_trigger(name, is_empty):
    if name == "GATE-IN" and is_empty:
        return "EMPTY_CONTAINER_RETURNED"
    if name == "GATE-OUT" and is_empty:
        return "EMPTY_CONTAINER_DISPATCHED"
    return TRIGGER_MAP.get(name)


def _unwrap(raw):
    """Unwrap dp-response-consumer envelope: raw → rawData (str) → rawData (str) → containers."""
    obj = raw
    for _ in range(3):
        rd = obj.get("rawData") if isinstance(obj, dict) else None
        if rd is None:
            break
        if isinstance(rd, str):
            try:
                obj = json.loads(rd)
            except Exception:
                break
        else:
            obj = rd
    return obj


def transform(raw):
    payload = _unwrap(raw)
    containers = payload.get("containers", [payload])
    rows = []
    for c in containers:
        cnum = c.get("containerNumber", c.get("container_number", c.get("equipmentNumber", "?")))
        for loc in c.get("locations", [c]):
            loc_code = loc.get("unLocode", loc.get("un_locode", loc.get("locationCode", "?")))
            for i, e in enumerate(loc.get("events", [])):
                trig_name = e.get("eventTriggerName", e.get("event_trigger_name", "?"))
                timing_raw = e.get("eventTimingType", e.get("event_timing_type", e.get("eventDatetimeType", "?")))
                dt   = e.get("eventDatetime", e.get("event_datetime"))
                mode = e.get("transportMode", e.get("transport_mode", "-"))
                is_empty = bool(e.get("isEmpty", e.get("is_empty", False)))

                timing = TIMING_MAP.get(timing_raw)
                trigger = get_trigger(trig_name, is_empty)

                if dt is None:
                    status = "DROPPED: null eventDatetime"
                    trigger = trigger or "-"; timing = timing or "-"
                elif trigger is None:
                    status = f"DROPPED: unknown trigger '{trig_name}'"
                    trigger = "-"; timing = timing or "-"
                elif timing is None:
                    status = f"DROPPED: unknown timing '{timing_raw}'"
                    timing = "-"
                else:
                    status = "TRANSFORMED"

                rows.append({
                    "container": cnum, "location": loc_code,
                    "raw_trigger": trig_name, "raw_timing": timing_raw,
                    "is_empty": is_empty, "dt": str(dt)[:19] if dt else "null",
                    "mode": mode, "trigger": trigger, "timing": timing or "-",
                    "status": status,
                })
    return rows, payload


def parse_legs(payload):
    out = []
    for c in payload.get("containers", [payload]):
        for i, l in enumerate(c.get("transportLegs", c.get("transport_legs", c.get("legs", [])))):
            dep = l.get("departureLocation", l.get("from", "?"))
            arr = l.get("arrivalLocation", l.get("to", "?"))
            vessel = l.get("vesselName", "?"); voyage = l.get("voyageNumber", "?")
            mode = l.get("transportMode", "?"); etd = l.get("etd", "?"); eta = l.get("eta", "?")
            out.append(f"Leg {i}: {dep}→{arr} | vessel={vessel} voyage={voyage} mode={mode} ETD={etd} ETA={eta}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, payload = transform(raw)
    legs = parse_legs(payload)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"MAERSKTNT  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'ctr':<14} {'loc':<7} {'raw_trigger':<28} {'timing':<10} {'empty':<6} {'dt':<20} {'→trigger':<42} {'→timing':<12} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['container']:<14} {r['location']:<7} {r['raw_trigger']:<28} {r['raw_timing']:<10} {str(r['is_empty']):<6} {r['dt']:<20} {r['trigger']:<42} {r['timing']:<12} {r['status']}")

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs: print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)}")
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
    print("  DROPPED: null eventDatetime    → Provider sent event with no timestamp")
    print("  DROPPED: unknown trigger       → DAS TRANSFORMATION (unmapped code)")
    print("  status=TRANSFORMED             → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
