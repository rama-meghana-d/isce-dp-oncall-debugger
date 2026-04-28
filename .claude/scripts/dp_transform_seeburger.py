#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for SEEBURGER. Reads raw blob JSON from stdin.
Only EQUIPMENT_EVENT type passes through; trigger/timing are passthrough enum names."""
import sys, json, argparse

VALID_TRIGGERS = {
    "GATE_IN", "GATE_OUT", "LOADED_ON_VESSEL", "UNLOADED_FROM_VESSEL",
    "VESSEL_ARRIVAL", "VESSEL_DEPARTURE", "DELIVERY", "CARRIER_RELEASE",
    "IMPORT_CUSTOMS_ON_HOLD", "IMPORT_CUSTOMS_CLEARED", "EMPTY_CONTAINER_RETURNED",
    "LOADED_ON_RAIL", "UNLOADED_FROM_A_RAIL_CAR",
    "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP", "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    "BARGE_ARRIVAL", "BARGE_DEPARTURE", "LOADED_ON_BARGE", "UNLOADED_FROM_BARGE",
    "STRIPPING", "STUFFING",
}
VALID_TIMINGS = {"ACTUAL", "ESTIMATED", "PLANNED", "PREDICTED"}


def get_name(field):
    if isinstance(field, dict):
        return field.get("name", "?")
    return str(field) if field else "?"


def transform(raw):
    payload = raw.get("payload", raw)
    messages = payload.get("messages", payload.get("events", []))
    rows = []
    seen = set()

    for i, m in enumerate(messages):
        trig_name   = get_name(m.get("eventTrigger"))
        timing_name = get_name(m.get("eventTiming"))
        etype_name  = get_name(m.get("eventType"))
        ts          = m.get("eventDateTime", m.get("timestamp", "?"))

        site = m.get("site", m.get("facility", {})) or {}
        city = m.get("city", {}) or {}
        loc  = site.get("unlocode", city.get("unlocode", "?"))
        vessel = m.get("vesselName", "?")
        voyage = m.get("voyageNumber", "?")
        mode   = m.get("transportMode", "-")

        if etype_name != "EQUIPMENT_EVENT":
            status = f"DROPPED: eventType={etype_name} (only EQUIPMENT_EVENT supported)"
            trig_name_out = "-"; timing_name_out = "-"
        elif trig_name not in VALID_TRIGGERS:
            status = f"DROPPED: unknown trigger '{trig_name}' not in Milestone enum"
            trig_name_out = "-"; timing_name_out = timing_name
        elif timing_name not in VALID_TIMINGS:
            status = f"DROPPED: unknown timing '{timing_name}'"
            trig_name_out = trig_name; timing_name_out = "-"
        else:
            dedup_key = (trig_name, timing_name, loc)
            if dedup_key in seen:
                status = f"DROPPED: duplicate ({trig_name}/{timing_name}/{loc})"
                trig_name_out = trig_name; timing_name_out = timing_name
            else:
                seen.add(dedup_key)
                status = "TRANSFORMED"
                trig_name_out = trig_name; timing_name_out = timing_name

        rows.append({
            "i": i, "raw_trigger": trig_name, "raw_timing": timing_name,
            "event_type": etype_name, "ts": str(ts)[:19], "location": loc,
            "vessel": vessel, "voyage": voyage, "mode": mode,
            "trigger": trig_name_out, "timing": timing_name_out, "status": status,
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

    print(f"\n{'='*70}")
    print(f"SEEBURGER  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'#':<3} {'raw_trigger':<28} {'timing':<10} {'eventType':<18} {'ts':<20} {'loc':<7} {'vessel':<18} {'→trigger':<28} {'→timing':<10} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['i']:<3} {r['raw_trigger']:<28} {r['raw_timing']:<10} {r['event_type']:<18} {r['ts']:<20} {r['location']:<7} {str(r['vessel']):<18} {r['trigger']:<28} {r['timing']:<10} {r['status']}")

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)}")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = "non-EQUIPMENT_EVENT" if "EQUIPMENT_EVENT" in r["status"] else \
                ("deduplication" if "duplicate" in r["status"] else \
                ("invalid trigger" if "trigger" in r["status"] else "invalid timing"))
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items(): print(f"    - {reason}: {cnt}")

    print(f"\n── Root Cause Guide ──")
    print("  Data absent from raw              → DATA PROVIDER did not send it")
    print("  DROPPED: non-EQUIPMENT_EVENT      → Provider sent wrong eventType; DAS only processes EQUIPMENT_EVENT")
    print("  DROPPED: invalid trigger/timing   → DAS TRANSFORMATION (enum mismatch)")
    print("  DROPPED: duplicate                → DAS TRANSFORMATION (dedup)")
    print("  status=TRANSFORMED                → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
