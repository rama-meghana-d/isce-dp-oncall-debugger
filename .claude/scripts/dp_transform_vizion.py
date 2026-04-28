#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for VIZION. Reads raw blob JSON from stdin."""
import sys, json, argparse
from datetime import datetime

# ── mapping tables ────────────────────────────────────────────────────────────
TRIGGER_MAP = {
    ("GTIN", None): "GATE_IN",
    ("LOAD", "VESSEL"): "LOADED_ON_VESSEL", ("LOAD", None): "LOADED_ON_VESSEL",
    ("LOAD", "RAIL"): "LOADED_ON_RAIL", ("LOAD", "BARGE"): "LOADED_ON_BARGE",
    ("ARRI", "VESSEL"): "VESSEL_ARRIVAL", ("ARRI", None): "VESSEL_ARRIVAL",
    ("ARRI", "RAIL"): "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP",
    ("ARRI", "BARGE"): "BARGE_ARRIVAL",
    ("DEPA", "VESSEL"): "VESSEL_DEPARTURE", ("DEPA", None): "VESSEL_DEPARTURE",
    ("DEPA", "RAIL"): "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    ("DEPA", "BARGE"): "BARGE_DEPARTURE",
    ("DISC", "VESSEL"): "UNLOADED_FROM_VESSEL", ("DISC", None): "UNLOADED_FROM_VESSEL",
    ("DISC", "RAIL"): "UNLOADED_FROM_A_RAIL_CAR", ("DISC", "BARGE"): "UNLOADED_FROM_BARGE",
    ("GTOT", None): "GATE_OUT",
    ("ISSU", None): "CARRIER_RELEASE",
    ("HOLD", None): "IMPORT_CUSTOMS_ON_HOLD",
    ("RELS", None): "IMPORT_CUSTOMS_CLEARED",
    ("DROP", None): "DELIVERY_CONFIRMED",
}
TIMING_MAP = {"ACT": "ACTUAL", "EST": "ESTIMATED", "PLN": "PLANNED"}
DESC_OVERRIDES = {
    "gate in empty return": "EMPTY_CONTAINER_RETURNED",
    "out for delivery": "GATE_OUT",
    "discharged from barge": "UNLOADED_FROM_BARGE",
}


def get_trigger(event_type, mode, source, desc, loc_type):
    desc_l = (desc or "").lower()
    for k, v in DESC_OVERRIDES.items():
        if k in desc_l:
            return v, None
    if event_type == "ARRI" and loc_type == "POL" and source != "ais":
        return "GATE_IN", None
    if event_type == "DEPA" and loc_type == "POD":
        return "DEPARTURE", None
    if event_type == "ARRI" and loc_type == "PDE":
        return "ARRIVAL", None
    key = (event_type, mode)
    if key in TRIGGER_MAP:
        return TRIGGER_MAP[key], None
    key_no_mode = (event_type, None)
    if key_no_mode in TRIGGER_MAP:
        return TRIGGER_MAP[key_no_mode], None
    return None, f"UNKNOWN type={event_type}"


def is_ais_drop(source, desc):
    if source != "ais":
        return False
    desc_l = (desc or "").lower()
    if "vessel arrived at origin port" in desc_l:
        return True
    return False


def parse_events(payload):
    milestones = payload.get("milestones", [])
    return milestones


def transform(raw):
    payload = raw.get("payload", raw)
    events = parse_events(payload)
    rows = []
    for i, m in enumerate(events):
        etype  = m.get("type", "?")
        cls    = m.get("event_classifier", "?")
        src    = m.get("source", "")
        desc   = m.get("description", "")
        ts     = m.get("event_datetime", "?")
        loc    = m.get("location", {})
        unlocode = loc.get("un_locode", "?")
        loc_type = loc.get("location_type_code", "")
        mode   = (m.get("transport_mode") or "").upper() or None

        timing = TIMING_MAP.get(cls, f"UNKNOWN({cls})")
        trigger, reason = get_trigger(etype, mode, src, desc, loc_type)

        if is_ais_drop(src, desc):
            status = "DROPPED: AIS source (vizion-exclude-ais-events)"
        elif src == "ais" and not loc_type:
            status = "DROPPED: AIS port-of-call event"
        elif trigger is None:
            status = f"DROPPED: {reason or 'unmapped type'}"
        else:
            status = "TRANSFORMED"

        rows.append({
            "i": i, "type": etype, "classifier": cls, "source": src,
            "location": unlocode, "mode": mode or "-", "desc": (desc or "")[:40],
            "trigger": trigger or "-", "timing": timing, "status": status,
        })
    return rows, payload


def parse_legs(payload):
    legs = payload.get("legs", [])
    out = []
    for i, leg in enumerate(legs):
        dep = leg.get("departure_location", {}).get("un_locode", "?")
        arr = leg.get("arrival_location", {}).get("un_locode", "?")
        vessel = leg.get("vessel_name", "?")
        voyage = leg.get("voyage_number", "?")
        mode   = leg.get("transport_mode", "?")
        etd    = leg.get("etd", "?")
        eta    = leg.get("eta", "?")
        out.append(f"Leg {i}: {dep}→{arr} | vessel={vessel} voyage={voyage} mode={mode} ETD={etd} ETA={eta}")
    return out


def print_table(rows):
    hdr = f"{'#':<3} {'type':<6} {'cls':<4} {'src':<8} {'loc':<7} {'mode':<8} {'→trigger':<35} {'→timing':<12} status"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['i']:<3} {r['type']:<6} {r['classifier']:<4} {r['source']:<8} {r['location']:<7} {r['mode']:<8} {r['trigger']:<35} {r['timing']:<12} {r['status']}")


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
    print(f"VIZION  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS in raw payload.")
    else:
        print(f"\n── Transformation Table ({len(rows)} raw events) ──")
        print_table(rows)

    if legs:
        print(f"\n── Transport Plan Legs ({len(legs)}) ──")
        for l in legs:
            print(" ", l)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)}")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = r["status"].split(":")[1].strip() if ":" in r["status"] else r["status"]
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items():
            print(f"    - {reason}: {cnt}")

    print(f"\n── Root Cause Guide ──")
    print("  Data absent from raw           → DATA PROVIDER did not send it")
    print("  In raw, status=DROPPED         → DAS TRANSFORMATION (see reason above)")
    print("  In raw, status=TRANSFORMED     → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
