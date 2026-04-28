#!/usr/bin/env python3
"""DAS-Jobs transformation simulator for PCA. Reads raw blob JSON from stdin.
PCA always produces exactly one event: VESSEL_ARRIVAL / PREDICTED."""
import sys, json, argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default="?")
    ap.add_argument("--container", default="?")
    args = ap.parse_args()

    raw = json.load(sys.stdin)
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
    payload = obj.get("payload", obj)

    predicted_ts   = payload.get("predictedArrivalTime", payload.get("predicted_arrival", payload.get("predicted_timestamp", payload.get("eta", "NOT_FOUND"))))
    last_discharge = payload.get("lastDischargePort", payload.get("last_discharge_port", payload.get("pod", "NOT_FOUND")))
    container      = payload.get("containerNumber", payload.get("container_number", args.container))

    print(f"\n{'='*70}")
    print(f"PCA  job={args.job_id}  container={args.container}")
    print(f"{'='*70}")
    print("PCA always maps to: eventTrigger=VESSEL_ARRIVAL  eventTiming=PREDICTED (hardcoded)")
    print("No filtering logic — if data is present, one event is always produced.\n")

    print(f"── Raw Payload Fields ──")
    print(f"  container            : {container}")
    print(f"  lastDischargePort    : {last_discharge}")
    print(f"  predictedArrivalTime : {predicted_ts}")

    print(f"\n── Transformation Table ──")
    hdr = f"{'Raw field':<28} {'Raw value':<28} {'→ Transformed field':<24} {'→ Transformed value'}"
    print(hdr); print("-"*len(hdr))
    print(f"{'predictedArrivalTime':<28} {str(predicted_ts):<28} {'timestamp':<24} {predicted_ts}")
    print(f"{'lastDischargePort':<28} {str(last_discharge):<28} {'location':<24} {last_discharge}")
    print(f"{'(hardcoded)':<28} {'—':<28} {'eventTrigger':<24} VESSEL_ARRIVAL")
    print(f"{'(hardcoded)':<28} {'—':<28} {'eventTiming':<24} PREDICTED")

    is_complete = predicted_ts != "NOT_FOUND" and last_discharge != "NOT_FOUND"

    print(f"\n── Counts ──")
    print(f"  Provider sent : 1 record")
    print(f"  Transformed   : {'1 event' if is_complete else '0 (missing fields)'}")
    print(f"  Dropped       : 0 (PCA has no filtering)")

    print(f"\n── Root Cause Guide ──")
    print("  PCA only provides VESSEL_ARRIVAL PREDICTED — no other event types")
    print("  If predicted ETA is wrong          → DATA PROVIDER sent wrong predictedArrivalTime")
    print("  If VESSEL_ARRIVAL PREDICTED missing → Check MP/MCE/DOS chain (PCA always produces one event)")
    print("  Any other event type missing        → PCA does not cover it; check other providers")


if __name__ == "__main__":
    main()
