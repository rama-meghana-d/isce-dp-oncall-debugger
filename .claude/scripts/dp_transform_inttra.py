#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for INTTRA. Reads raw blob JSON from stdin.
NOTE: VESSEL_ARRIVAL and VESSEL_DEPARTURE are dropped by default (flag=true)."""
import sys, json, argparse

EXCLUDED_BY_FLAG = {"VESSEL_ARRIVAL", "VESSEL_DEPARTURE"}
TRIGGER_REMAP = {"ESTIMATED_DELIVERY": "DELIVERY"}


def transform(raw):
    payload = raw.get("payload", raw)
    events = payload.get("events", payload.get("milestones", []))
    rows = []
    seen = set()  # dedup key: (trigger, timing, location)

    for i, e in enumerate(events):
        raw_trig = e.get("eventTrigger", e.get("type", "?"))
        timing   = e.get("eventTiming", e.get("timing", "?"))
        ts       = e.get("eventDateTime", e.get("timestamp", "?"))
        loc      = e.get("locationCode", e.get("unLocode", "?"))
        vessel   = e.get("vesselName", e.get("vessel", {}).get("name", "-") if isinstance(e.get("vessel"), dict) else e.get("vessel", "-"))
        voyage   = e.get("voyageNumber", e.get("voyage", "-"))

        trigger = TRIGGER_REMAP.get(raw_trig, raw_trig)

        if trigger in EXCLUDED_BY_FLAG:
            status = f"DROPPED: inttra-exclude-vessel-arrival-departure=true (default ON)"
        else:
            dedup_key = (trigger, timing, loc)
            if dedup_key in seen:
                status = f"DROPPED: duplicate (trigger={trigger}, timing={timing}, loc={loc})"
            else:
                seen.add(dedup_key)
                status = "TRANSFORMED"

        rows.append({
            "i": i, "raw_trigger": raw_trig, "timing": timing, "ts": str(ts)[:19],
            "location": loc, "vessel": vessel, "voyage": voyage,
            "trigger": trigger, "status": status,
        })
    return rows, payload


def parse_event_connections(payload):
    out = []
    for e in payload.get("events", []):
        for ec in e.get("eventConnections", []):
            vessel = ec.get("vesselName", "-")
            voyage = ec.get("voyageNumber", "-")
            imo    = ec.get("imoNumber", "-")
            mode   = ec.get("transportMode", "-")
            if vessel != "-":
                out.append(f"  vessel={vessel} voyage={voyage} imo={imo} mode={mode}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
    rows, payload = transform(raw)
    connections = parse_event_connections(payload)
    transformed = [r for r in rows if r["status"] == "TRANSFORMED"]
    dropped     = [r for r in rows if r["status"].startswith("DROPPED")]

    print(f"\n{'='*70}")
    print(f"INTTRA  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")
    print("NOTE: VESSEL_ARRIVAL and VESSEL_DEPARTURE are ALWAYS dropped (inttra-exclude flag default=true)")

    if not rows:
        print("NO EVENTS found.")
    else:
        print(f"\n── Transformation Table ({len(rows)} events) ──")
        hdr = f"{'#':<3} {'raw_trigger':<28} {'timing':<12} {'ts':<20} {'loc':<7} {'vessel':<20} {'voyage':<12} {'→trigger':<28} status"
        print(hdr); print("-"*len(hdr))
        for r in rows:
            print(f"{r['i']:<3} {r['raw_trigger']:<28} {r['timing']:<12} {r['ts']:<20} {r['location']:<7} {str(r['vessel']):<20} {str(r['voyage']):<12} {r['trigger']:<28} {r['status']}")

    if connections:
        print(f"\n── Event Connections (vessel references) ──")
        for c in connections: print(c)

    print(f"\n── Counts ──")
    print(f"  Provider sent : {len(rows)}")
    print(f"  Transformed   : {len(transformed)}")
    print(f"  Dropped       : {len(dropped)}")
    if dropped:
        reasons = {}
        for r in dropped:
            k = "vessel arrival/departure exclusion" if "exclude" in r["status"] else \
                ("deduplication" if "duplicate" in r["status"] else r["status"])
            reasons[k] = reasons.get(k, 0) + 1
        for reason, cnt in reasons.items(): print(f"    - {reason}: {cnt}")

    print(f"\n── Root Cause Guide ──")
    print("  VESSEL_ARRIVAL/DEPARTURE missing  → EXPECTED — dropped by inttra exclusion flag (by design)")
    print("  Other event absent from raw        → DATA PROVIDER did not send it")
    print("  In raw but DROPPED: duplicate      → DAS TRANSFORMATION (dedup)")
    print("  status=TRANSFORMED                 → Issue is DOWNSTREAM (MP/MCE/DOS/GIS)")


if __name__ == "__main__":
    main()
