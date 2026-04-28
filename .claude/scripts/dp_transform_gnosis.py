#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for GNOSIS. Reads raw blob JSON from stdin."""
import sys, json, argparse

FLAT_FIELD_MAP = {
    "in_gate_dt":        "GATE_IN",
    "loaded_on_vessel_dt": "LOADED_ON_VESSEL",
    "motherload_dt":     "LOADED_ON_VESSEL",
    "discharged_dt":     "UNLOADED_FROM_VESSEL",
    "vessel_atd_dt":     "VESSEL_DEPARTURE",
    "vessel_ata_dt":     "VESSEL_ARRIVAL",
    "out_gate_dt":       "GATE_OUT",
    "empty_out_dt":      "GATE_OUT",
    "empty_returned_dt": "EMPTY_CONTAINER_RETURNED",
    "loaded_on_rail_dt": "LOADED_ON_RAIL",
    "rail_departed_dt":  "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    "rail_ata_dt":       "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP",
    "rail_discharged_dt":"UNLOADED_FROM_A_RAIL_CAR",
}
DESC_MAP = {
    "loaded": "LOADED_ON_VESSEL",
    "discharged": "UNLOADED_FROM_VESSEL",
    "departure from": "VESSEL_DEPARTURE",
    "vessel departed": "VESSEL_DEPARTURE",
    "arrival in": "VESSEL_ARRIVAL",
    "vessel arrived": "VESSEL_ARRIVAL",
    "gate in": "GATE_IN",
    "gate out": "GATE_OUT",
}
FLAG_MAP = {"A": "ACTUAL", "E": "ESTIMATED"}


def transform_flat(payload):
    rows = []
    for field, trigger in FLAT_FIELD_MAP.items():
        val = payload.get(field)
        if val is None:
            continue
        flag = payload.get(field.replace("_dt", "_flag"), "A")
        timing = FLAG_MAP.get(flag, f"UNKNOWN({flag})")
        loc = payload.get("un_locode", payload.get("unlocode", "?"))
        rows.append({
            "raw_field": field, "value": val, "flag": flag,
            "location": loc, "trigger": trigger, "timing": timing, "status": "TRANSFORMED",
        })
    return rows


def transform_milestones(milestones):
    rows = []
    for i, m in enumerate(milestones):
        desc = (m.get("standard_event_desc") or m.get("description") or "").strip()
        ts   = m.get("event_dt", m.get("timestamp", "?"))
        flag = m.get("flag", "A")
        unlocode = m.get("unlocode", "?")

        if desc.upper() == "NOTIFY_DT":
            status = "DROPPED: NOTIFY_DT (position-only event)"
            trigger, timing = "-", "-"
        elif not ts or ts == "null":
            status = "DROPPED: null timestamp"
            trigger, timing = "-", "-"
        else:
            timing = FLAG_MAP.get(flag, f"UNKNOWN({flag})")
            desc_l = desc.lower()
            trigger = next((v for k, v in DESC_MAP.items() if k in desc_l), None)
            if trigger:
                status = "TRANSFORMED"
            else:
                status = f"DROPPED: unmapped description='{desc}'"
                trigger = "-"

        rows.append({
            "raw_field": f"milestone[{i}]", "value": ts, "flag": flag,
            "location": unlocode, "trigger": trigger, "timing": timing,
            "status": status, "desc": desc[:40],
        })
    return rows


def parse_legs(payload):
    legs = payload.get("legs", payload.get("transport_legs", []))
    out = []
    for i, l in enumerate(legs):
        pol = l.get("pol", l.get("port_of_loading", "?"))
        pod = l.get("pod", l.get("port_of_discharge", "?"))
        vessel = l.get("vessel_name", "?")
        voyage = l.get("voyage", l.get("voyage_number", "?"))
        etd = l.get("etd", "?"); eta = l.get("eta", "?")
        out.append(f"Leg {i}: {pol}→{pod} | vessel={vessel} voyage={voyage} ETD={etd} ETA={eta}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    payload = raw.get("payload", raw)

    milestones = payload.get("milestones", [])
    if milestones:
        rows = transform_milestones(milestones)
        fmt = "raw_milestones"
    else:
        rows = transform_flat(payload)
        fmt = "flat_fields"

    legs = parse_legs(payload)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"GNOSIS  job={args.job_id}  container={args.container}  format={fmt}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS in raw payload.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'field/idx':<22} {'value':<22} {'flag':<5} {'loc':<7} {'→trigger':<42} {'→timing':<10} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            desc = f" [{r.get('desc','')}]" if r.get("desc") else ""
            print(f"{r['raw_field']:<22} {str(r['value'])[:22]:<22} {r['flag']:<5} {r['location']:<7} {r['trigger']:<42} {r['timing']:<10} {r['status']}{desc}")

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs: print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)}")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        for r in dropped: print(f"    - {r['raw_field']}: {r['status']}")

    print(f"\n── Root Cause Guide ──")
    print("  Data absent from raw           → DATA PROVIDER did not send it")
    print("  In raw, status=DROPPED         → DAS TRANSFORMATION")
    print("  In raw, status=TRANSFORMED     → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
