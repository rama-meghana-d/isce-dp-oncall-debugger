#!/usr/bin/env python3
"""
Compare normalized GIS events against DOS transport plan legs.

Scores every candidate leg for each event and explains why the currently
stitched leg was likely selected (or why it looks wrong).

Usage:
  python3 compare_event_to_transport_plan.py \
    --event <gis_events.json> \
    --transport-plan <dos_tp.json> \
    [--mce-cluster <mce_cluster.json>]

Input files are outputs from the other parse scripts in this folder.
Output: compact JSON with per-event scoring, best candidate, explanation, and warnings.
"""
import json
import sys
import argparse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Trigger classification
# ---------------------------------------------------------------------------

DEPARTURE_TRIGGERS = {
    "VESSEL_DEPARTURE", "LOAD_ON_VESSEL", "GATE_OUT_EMPTY",
    "ROAD_DEPARTURE", "RAIL_DEPARTURE", "BARGE_DEPARTURE",
    "INLAND_PASSAGE", "CUSTOMS_DEPARTURE", "STUFFED",
}

ARRIVAL_TRIGGERS = {
    "VESSEL_ARRIVAL", "UNLOADED_FROM_VESSEL", "GATE_IN",
    "ROAD_ARRIVAL", "RAIL_ARRIVAL", "BARGE_ARRIVAL",
    "CUSTOMS_ARRIVAL", "STRIPPED",
}

# Trigger → expected transport modes
MODE_MAP = {
    "VESSEL_DEPARTURE": {"SEA", "OCEAN", "VESSEL"},
    "VESSEL_ARRIVAL": {"SEA", "OCEAN", "VESSEL"},
    "LOAD_ON_VESSEL": {"SEA", "OCEAN", "VESSEL"},
    "UNLOADED_FROM_VESSEL": {"SEA", "OCEAN", "VESSEL"},
    "RAIL_DEPARTURE": {"RAIL", "RAIL_ROAD"},
    "RAIL_ARRIVAL": {"RAIL", "RAIL_ROAD"},
    "BARGE_DEPARTURE": {"BARGE", "INLAND_WATERWAY"},
    "BARGE_ARRIVAL": {"BARGE", "INLAND_WATERWAY"},
    "ROAD_DEPARTURE": {"ROAD", "TRUCK"},
    "ROAD_ARRIVAL": {"ROAD", "TRUCK"},
    "GATE_OUT_EMPTY": {"ROAD", "TRUCK", "SEA", "OCEAN"},
    "GATE_IN": {"ROAD", "TRUCK", "SEA", "OCEAN"},
}

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

SCORE_ORIGIN_LOCATION_MATCH = 10
SCORE_DESTINATION_LOCATION_MATCH = 10
SCORE_TRIGGER_SIDE_CORRECT = 8
SCORE_TIMESTAMP_WITHIN_WINDOW = 8
SCORE_MODE_MATCH = 6
SCORE_VESSEL_MATCH = 6
SCORE_VOYAGE_MATCH = 4
PENALTY_SELF_LOOP = -5
PENALTY_WRONG_TRIGGER_SIDE = -3

TIMESTAMP_WINDOW_DAYS = 2
TIMESTAMP_ANOMALY_DAYS = 7

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True, help="GIS events JSON file (from parse_gis_shipment_journey_transaction.py)")
    p.add_argument("--transport-plan", required=True, help="DOS transport plan JSON file (from parse_dos_transport_plan.py)")
    p.add_argument("--mce-cluster", help="MCE cluster JSON file (from parse_mce_cluster_output.py) — optional")
    return p.parse_args()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def parse_ts(ts_str):
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts_str[:26], fmt[:len(ts_str)])
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def days_between(ts1, ts2):
    """Return absolute day difference, or None if either is unparseable."""
    d1, d2 = parse_ts(ts1), parse_ts(ts2)
    if d1 and d2:
        return abs((d1 - d2).total_seconds() / 86400)
    return None


def trigger_side(trigger):
    if trigger in DEPARTURE_TRIGGERS:
        return "ORIGIN"
    if trigger in ARRIVAL_TRIGGERS:
        return "DESTINATION"
    return "EITHER"


def mode_compatible(trigger, leg_mode):
    if not leg_mode or not trigger:
        return None
    expected = MODE_MAP.get(trigger)
    if not expected:
        return None
    return leg_mode.upper() in expected


# ---------------------------------------------------------------------------
# Per-leg scoring
# ---------------------------------------------------------------------------

def score_leg(event, leg):
    score = 0
    reasons = []
    warnings = []

    trigger = event.get("eventTrigger") or ""
    ev_loc = event.get("eventLocationCode")
    ev_ts = event.get("eventTimestamp")
    ev_vessel = event.get("vesselName")
    ev_voyage = event.get("voyage")
    ev_mode = event.get("mode")

    leg_from = leg.get("fromLocation")
    leg_to = leg.get("toLocation")
    leg_mode = leg.get("mode")
    leg_vessel = leg.get("vesselName")
    leg_voyage = leg.get("voyage")
    leg_seq = leg.get("legSequence")
    is_self_loop = leg.get("isSelfLoopLeg", False)

    side = trigger_side(trigger)

    # --- Self-loop penalty
    if is_self_loop:
        score += PENALTY_SELF_LOOP
        warnings.append(f"Self-loop leg ({leg_from} → {leg_to}) — stitching here requires justification")

    # --- Location match
    origin_match = ev_loc and leg_from and ev_loc == leg_from
    dest_match = ev_loc and leg_to and ev_loc == leg_to

    if origin_match:
        score += SCORE_ORIGIN_LOCATION_MATCH
        reasons.append(f"eventLocation {ev_loc} matches leg origin {leg_from}")
    if dest_match:
        score += SCORE_DESTINATION_LOCATION_MATCH
        reasons.append(f"eventLocation {ev_loc} matches leg destination {leg_to}")
    if ev_loc and not origin_match and not dest_match:
        warnings.append(f"eventLocation {ev_loc} does not match leg origin ({leg_from}) or destination ({leg_to})")

    # --- Trigger-side compatibility
    if side == "ORIGIN" and origin_match:
        score += SCORE_TRIGGER_SIDE_CORRECT
        reasons.append(f"Departure trigger {trigger} correctly associated with leg origin")
    elif side == "ORIGIN" and dest_match and not origin_match:
        score += PENALTY_WRONG_TRIGGER_SIDE
        warnings.append(f"Departure trigger {trigger} stitched to leg destination (wrong side)")
    elif side == "DESTINATION" and dest_match:
        score += SCORE_TRIGGER_SIDE_CORRECT
        reasons.append(f"Arrival trigger {trigger} correctly associated with leg destination")
    elif side == "DESTINATION" and origin_match and not dest_match:
        score += PENALTY_WRONG_TRIGGER_SIDE
        warnings.append(f"Arrival trigger {trigger} stitched to leg origin (wrong side)")

    # --- Timestamp proximity
    # Use the most specific available timestamp for the relevant leg side
    if side == "ORIGIN":
        leg_ts = leg.get("actualDeparture") or leg.get("estimatedDeparture") or leg.get("plannedDeparture")
    elif side == "DESTINATION":
        leg_ts = leg.get("actualArrival") or leg.get("estimatedArrival") or leg.get("plannedArrival")
    else:
        leg_ts = leg.get("plannedDeparture") or leg.get("plannedArrival")

    diff_days = days_between(ev_ts, leg_ts)
    if diff_days is not None:
        if diff_days <= TIMESTAMP_WINDOW_DAYS:
            score += SCORE_TIMESTAMP_WITHIN_WINDOW
            reasons.append(f"Event timestamp within {diff_days:.1f} days of leg scheduled time")
        elif diff_days <= TIMESTAMP_ANOMALY_DAYS:
            reasons.append(f"Event timestamp {diff_days:.1f} days from leg scheduled time (outside 2-day window)")
        else:
            warnings.append(f"Event timestamp {diff_days:.1f} days from leg scheduled time (anomaly)")

    # --- Mode compatibility
    compat = mode_compatible(trigger, leg_mode)
    if compat is True:
        score += SCORE_MODE_MATCH
        reasons.append(f"Trigger {trigger} is compatible with leg mode {leg_mode}")
    elif compat is False:
        warnings.append(f"Trigger {trigger} is NOT compatible with leg mode {leg_mode}")

    # --- Vessel match
    if ev_vessel and leg_vessel:
        if ev_vessel.strip().upper() == leg_vessel.strip().upper():
            score += SCORE_VESSEL_MATCH
            reasons.append(f"Vessel name matches: {ev_vessel}")
        else:
            warnings.append(f"Vessel mismatch: event={ev_vessel}, leg={leg_vessel}")

    # --- Voyage match
    if ev_voyage and leg_voyage:
        if ev_voyage.strip().upper() == leg_voyage.strip().upper():
            score += SCORE_VOYAGE_MATCH
            reasons.append(f"Voyage matches: {ev_voyage}")
        else:
            warnings.append(f"Voyage mismatch: event={ev_voyage}, leg={leg_voyage}")

    return {
        "legSequence": leg_seq,
        "from": leg_from,
        "to": leg_to,
        "mode": leg_mode,
        "isSelfLoop": is_self_loop,
        "score": score,
        "reasons": reasons,
        "warnings": warnings,
    }


def confidence_label(best_score, selected_score, best_is_selected):
    if best_is_selected and best_score >= 28:
        return "High"
    if best_is_selected and best_score >= 14:
        return "Medium"
    if not best_is_selected and (best_score - selected_score) >= 10:
        return "Low"
    return "Medium"


# ---------------------------------------------------------------------------
# Main comparison logic
# ---------------------------------------------------------------------------

def compare_event(event, legs, mce_events_by_trigger_loc):
    trigger = event.get("eventTrigger") or "UNKNOWN"
    ev_loc = event.get("eventLocationCode")
    current_leg_seq = event.get("stitchedLegSequence")

    # Score every leg
    scores = [score_leg(event, leg) for leg in legs]
    scores.sort(key=lambda s: s["score"], reverse=True)

    best = scores[0] if scores else None
    selected = next((s for s in scores if s["legSequence"] == current_leg_seq), None)

    best_is_selected = (
        best is not None
        and selected is not None
        and best["legSequence"] == selected["legSequence"]
    )

    # Build explanation
    explanation_parts = []
    if selected:
        explanation_parts.append(f"Event ({trigger} @ {ev_loc}) is currently stitched to leg {current_leg_seq} ({selected['from']} → {selected['to']}).")
        if selected["reasons"]:
            explanation_parts.append("Supporting signals: " + "; ".join(selected["reasons"]) + ".")
        if selected["warnings"]:
            explanation_parts.append("Concerns: " + "; ".join(selected["warnings"]) + ".")
    else:
        explanation_parts.append(f"Stitched leg {current_leg_seq} not found in transport plan — leg may be missing or sequence mismatch.")

    if not best_is_selected and best:
        explanation_parts.append(
            f"Higher-scoring leg {best['legSequence']} ({best['from']} → {best['to']}) "
            f"scored {best['score']} vs selected leg score {selected['score'] if selected else 'N/A'}. "
            f"Possible stitching anomaly."
        )

    # MCE context
    mce_key = f"{trigger}|{ev_loc}"
    mce_match = mce_events_by_trigger_loc.get(mce_key)
    if mce_match:
        mce_leg = mce_match.get("selectedLeg")
        if mce_leg and str(mce_leg) != str(current_leg_seq):
            explanation_parts.append(
                f"MCE cluster selected leg {mce_leg} but GIS stitched to leg {current_leg_seq} — possible GIS persistence issue."
            )

    # Assessment
    if not best_is_selected and best and (best["score"] - (selected["score"] if selected else 0)) >= 10:
        assessment = "Suspicious"
    elif selected and selected["warnings"] and selected["score"] < 10:
        assessment = "Suspicious"
    elif not selected:
        assessment = "Incorrect"
    else:
        assessment = "Correct"

    conf = confidence_label(
        best["score"] if best else 0,
        selected["score"] if selected else 0,
        best_is_selected,
    )

    return {
        "eventTrigger": trigger,
        "eventTiming": event.get("eventTiming"),
        "eventTimestamp": event.get("eventTimestamp"),
        "eventLocationCode": ev_loc,
        "selectedLeg": current_leg_seq,
        "selectedLegScore": selected["score"] if selected else None,
        "bestCandidateLeg": best["legSequence"] if best else None,
        "bestCandidateScore": best["score"] if best else None,
        "bestIsSelected": best_is_selected,
        "candidateLegScores": scores,
        "explanation": " ".join(explanation_parts),
        "assessment": assessment,
        "confidence": conf,
        "mceSelectedLeg": mce_match.get("selectedLeg") if mce_match else None,
    }


def main():
    args = parse_args()

    gis_data = load_json(args.event)
    tp_data = load_json(args.transport_plan)
    mce_data = load_json(args.mce_cluster) if args.mce_cluster else {}

    events = gis_data.get("events") or []
    legs = tp_data.get("legs") or []

    # Index MCE events by trigger+location for quick lookup
    mce_events_by_trigger_loc = {}
    for ev in (mce_data.get("events") or []):
        key = f"{ev.get('eventTrigger')}|{ev.get('eventLocationCode')}"
        mce_events_by_trigger_loc[key] = ev

    if not legs:
        print(json.dumps({"status": "NO_TRANSPORT_PLAN", "message": "No legs found in DOS transport plan"}, indent=2))
        return

    results = []
    for event in events:
        results.append(compare_event(event, legs, mce_events_by_trigger_loc))

    # Summary
    suspicious = [r for r in results if r["assessment"] in ("Suspicious", "Incorrect")]

    output = {
        "status": "OK",
        "journeyId": gis_data.get("journeyId"),
        "unitOfTracking": gis_data.get("unitOfTracking"),
        "eventCount": len(results),
        "suspiciousCount": len(suspicious),
        "results": results,
        "suspiciousEvents": [
            {"trigger": r["eventTrigger"], "location": r["eventLocationCode"],
             "selectedLeg": r["selectedLeg"], "bestCandidateLeg": r["bestCandidateLeg"],
             "assessment": r["assessment"], "confidence": r["confidence"]}
            for r in suspicious
        ],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
