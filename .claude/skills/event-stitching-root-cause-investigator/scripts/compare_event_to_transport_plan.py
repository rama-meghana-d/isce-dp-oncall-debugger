#!/usr/bin/env python3
"""
Simulate the DOS ClusteredShipmentJourneyUtility stitching algorithm.

Takes MCE cluster data + DOS transport plan legs and runs the same 7-priority
stitching logic to explain why each cluster was assigned to its leg.
Optionally cross-checks against GIS journey_request to detect discrepancies.

Usage:
  python3 compare_event_to_transport_plan.py \
    --clusters <mce_clusters.json> \
    --transport-plan <dos_tp.json> \
    [--gis-events <gis_events.json>]

Input files:
  --clusters        Output of parse_mce_cluster_output.py
  --transport-plan  Output of parse_dos_transport_plan.py
  --gis-events      Output of parse_gis_shipment_journey_transaction.py (optional)

Output: per-cluster JSON with stitchingPhase, simulatedLeg, phaseReason, gisLeg, discrepancy.
"""
import json
import argparse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Trigger classification (from EventTriggerConstants.java)
# ---------------------------------------------------------------------------

# START triggers when enable_gate_in_gate_out_reversal is DISABLED (default)
START_TRIGGERS_DEFAULT = {
    "GATE_IN",
    "EMPTY_CONTAINER_DISPATCHED",
    "ARRIVED_AT_CUSTOMER_LOCATION",
    "RECEIVED_AT_AIRPORT",
    "LOADED_ON_VESSEL", "LOADED_ON_RAIL", "LOADED_ON_BARGE",
    "VESSEL_DEPARTURE", "TRUCK_DEPARTURE",
    "RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP",
    "BARGE_DEPARTURE", "FLIGHT_DEPARTURE",
    "BOOKED_ON_FLIGHT", "MANIFEST_COMPLETED_AT_AIRPORT", "RECEIVED_FROM_SHIPPER",
    "EXPORT_CUSTOMS_CLEARED", "STUFFING", "DEPARTURE", "LOADED",
}

# END triggers when enable_gate_in_gate_out_reversal is DISABLED (default)
END_TRIGGERS_DEFAULT = {
    "VESSEL_ARRIVAL", "TRUCK_ARRIVAL",
    "RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP",
    "BARGE_ARRIVAL", "FLIGHT_ARRIVAL",
    "UNLOADED_FROM_VESSEL", "UNLOADED_FROM_A_RAIL_CAR", "UNLOADED_FROM_BARGE",
    "CARRIER_RELEASE",
    "GATE_OUT", "EXIT_FACILITY", "GATE_OUT_FROM_AIRPORT",
    "DELIVERY",
    "EMPTY_CONTAINER_RETURNED", "DELIVERY_CONFIRMED",
    "IN_TRANSIT_CUSTOMS_CLEARANCE_OPENED",
    "IN_TRANSIT_CUSTOMS_CLEARANCE_CLOSED",
    "IN_TRANSIT_CUSTOMS_CLEARANCE_EXPIRY",
    "IMPORT_CUSTOMS_ON_HOLD", "IMPORT_CUSTOMS_CLEARED",
    "RECEIVED_FROM_FLIGHT", "DELIVERED_AT_AIRPORT",
    "STRIPPING", "ARRIVAL", "UNLOADED",
}

# Terminal milestones without location → last ocean leg (Priority 2)
TERMINAL_WITHOUT_LOCATION = {"CARRIER_RELEASE", "IMPORT_CUSTOMS_CLEARED", "PICKUP_APPOINTMENT"}

# Vessel events → restricted to OCEAN legs in circular resolution
VESSEL_EVENTS = {"LOADED_ON_VESSEL", "VESSEL_DEPARTURE", "VESSEL_ARRIVAL", "UNLOADED_FROM_VESSEL"}

OCEAN_MODES = {"OCEAN", "SEA", "VESSEL"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--clusters", required=True, help="MCE clusters JSON (from parse_mce_cluster_output.py)")
    p.add_argument("--transport-plan", required=True, help="DOS transport plan JSON (from parse_dos_transport_plan.py)")
    p.add_argument("--gis-events", help="GIS events JSON (from parse_gis_shipment_journey_transaction.py) — optional")
    return p.parse_args()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def is_ocean(leg):
    mode = (leg.get("mode") or "").upper()
    return mode in OCEAN_MODES


def locations_match(cluster_loc, leg_loc):
    """Simple UNLOC equality. Both may be None."""
    if not cluster_loc or not leg_loc:
        return False
    return cluster_loc.strip().upper() == leg_loc.strip().upper()


def find_duplicate_locations(legs):
    """
    Find UNLOC codes that appear as a start or end of more than one leg.
    Returns (duplicate_starts: set, duplicate_ends: set).
    Mirrors ClusteredShipmentJourneyUtility.findDuplicateLocations().
    """
    start_count = {}
    end_count = {}
    for leg in legs:
        s = (leg.get("fromLocation") or "").strip().upper()
        e = (leg.get("toLocation") or "").strip().upper()
        if s:
            start_count[s] = start_count.get(s, 0) + 1
        if e:
            end_count[e] = end_count.get(e, 0) + 1
    dup_starts = {loc for loc, cnt in start_count.items() if cnt > 1}
    dup_ends = {loc for loc, cnt in end_count.items() if cnt > 1}
    return dup_starts, dup_ends


def parse_ts(ts_str):
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts_str[:26], fmt[:len(ts_str)])
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def ts_diff_days(ts1, ts2):
    d1, d2 = parse_ts(ts1), parse_ts(ts2)
    if d1 and d2:
        return abs((d1 - d2).total_seconds() / 86400)
    return None


def best_leg_timestamp(leg, side):
    """Get the most relevant leg timestamp for proximity comparison."""
    if side == "start":
        return leg.get("actualDeparture") or leg.get("estimatedDeparture") or leg.get("plannedDeparture")
    elif side == "end":
        return leg.get("actualArrival") or leg.get("estimatedArrival") or leg.get("plannedArrival")
    return leg.get("plannedDeparture") or leg.get("plannedArrival")


def sequence_proximity(cluster_seq, leg_clusters):
    """Distance between cluster sequence and the closest cluster already on a leg."""
    if not leg_clusters:
        return float("inf")
    return min(abs(cluster_seq - c.get("clusterSequenceNumber", 0)) for c in leg_clusters)


# ---------------------------------------------------------------------------
# Priority algorithm (mirrors ClusteredShipmentJourneyUtility)
# ---------------------------------------------------------------------------

def simulate_stitching(clusters, legs):
    """
    Run the 7-priority DOS stitching algorithm on clusters + legs.
    Returns list of cluster result dicts with simulatedLeg and stitchingPhase.
    """
    if not legs:
        return [
            {**c, "simulatedLeg": None, "stitchingPhase": "NO_LEGS",
             "phaseReason": "No transport plan legs available"}
            for c in clusters
        ]

    sorted_legs = sorted(legs, key=lambda l: l.get("legSequence") or 0)
    first_leg = sorted_legs[0]
    last_leg = sorted_legs[-1]
    ocean_legs = [l for l in sorted_legs if is_ocean(l)]
    last_ocean_leg = ocean_legs[-1] if ocean_legs else None

    dup_starts, dup_ends = find_duplicate_locations(sorted_legs)

    # Track which clusters are assigned to which leg for sequence proximity
    # leg_seq → list of cluster dicts
    leg_clusters_map = {l.get("legSequence"): [] for l in sorted_legs}

    results = []

    # --- Priority 1: Empty container hardwired ---
    p1_clusters = []
    remaining = []
    for cluster in clusters:
        trigger = cluster.get("eventTrigger") or ""
        if trigger == "EMPTY_CONTAINER_DISPATCHED":
            p1_clusters.append({
                **cluster,
                "simulatedLeg": first_leg.get("legSequence"),
                "stitchedLegFrom": first_leg.get("fromLocation"),
                "stitchedLegTo": first_leg.get("toLocation"),
                "stitchingPhase": "P1",
                "phaseReason": "EMPTY_CONTAINER_DISPATCHED always stitches to first leg regardless of location",
                "warnings": [],
            })
            leg_clusters_map[first_leg.get("legSequence")].append(cluster)
        elif trigger == "EMPTY_CONTAINER_RETURNED":
            p1_clusters.append({
                **cluster,
                "simulatedLeg": last_leg.get("legSequence"),
                "stitchedLegFrom": last_leg.get("fromLocation"),
                "stitchedLegTo": last_leg.get("toLocation"),
                "stitchingPhase": "P1",
                "phaseReason": "EMPTY_CONTAINER_RETURNED always stitches to last leg regardless of location",
                "warnings": [],
            })
            leg_clusters_map[last_leg.get("legSequence")].append(cluster)
        else:
            remaining.append(cluster)

    results.extend(p1_clusters)

    # --- Priority 2: Terminal milestones without location → last ocean leg ---
    p2_clusters = []
    next_remaining = []
    for cluster in remaining:
        trigger = cluster.get("eventTrigger") or ""
        loc = cluster.get("clusterLocationCode")
        if trigger in TERMINAL_WITHOUT_LOCATION and not loc:
            target_leg = last_ocean_leg or last_leg
            p2_clusters.append({
                **cluster,
                "simulatedLeg": target_leg.get("legSequence"),
                "stitchedLegFrom": target_leg.get("fromLocation"),
                "stitchedLegTo": target_leg.get("toLocation"),
                "stitchingPhase": "P2",
                "phaseReason": (
                    f"{trigger} has no cluster location → "
                    f"stitched to last {'ocean ' if last_ocean_leg else ''}leg "
                    f"(seq {target_leg.get('legSequence')})"
                ),
                "warnings": [],
            })
            leg_clusters_map[target_leg.get("legSequence")].append(cluster)
        else:
            next_remaining.append(cluster)

    results.extend(p2_clusters)
    remaining = next_remaining

    # --- Priority 3: Non-duplicate location + matching trigger side ---
    p3_clusters = []
    next_remaining = []
    for cluster in remaining:
        trigger = cluster.get("eventTrigger") or ""
        loc = (cluster.get("clusterLocationCode") or "").strip().upper()

        matched_leg = None
        phase_reason = None

        if trigger in START_TRIGGERS_DEFAULT and loc and loc not in dup_starts:
            for leg in sorted_legs:
                leg_from = (leg.get("fromLocation") or "").strip().upper()
                if loc == leg_from:
                    matched_leg = leg
                    phase_reason = (
                        f"P3: {trigger} is a START trigger; cluster location {loc} "
                        f"matches leg {leg.get('legSequence')} startLocation {leg.get('fromLocation')} "
                        f"(unique boundary — not a duplicate start location)"
                    )
                    break

        if matched_leg is None and trigger in END_TRIGGERS_DEFAULT and loc and loc not in dup_ends:
            for leg in sorted_legs:
                leg_to = (leg.get("toLocation") or "").strip().upper()
                if loc == leg_to:
                    matched_leg = leg
                    phase_reason = (
                        f"P3: {trigger} is an END trigger; cluster location {loc} "
                        f"matches leg {leg.get('legSequence')} endLocation {leg.get('toLocation')} "
                        f"(unique boundary — not a duplicate end location)"
                    )
                    break

        if matched_leg:
            p3_clusters.append({
                **cluster,
                "simulatedLeg": matched_leg.get("legSequence"),
                "stitchedLegFrom": matched_leg.get("fromLocation"),
                "stitchedLegTo": matched_leg.get("toLocation"),
                "stitchingPhase": "P3",
                "phaseReason": phase_reason,
                "warnings": [],
            })
            leg_clusters_map[matched_leg.get("legSequence")].append(cluster)
        else:
            next_remaining.append(cluster)

    results.extend(p3_clusters)
    remaining = next_remaining

    # --- Priority 4: Duplicate location → circular leg resolution ---
    p4_clusters = []
    next_remaining = []
    for cluster in remaining:
        trigger = cluster.get("eventTrigger") or ""
        loc = (cluster.get("clusterLocationCode") or "").strip().upper()
        cluster_seq = cluster.get("clusterSequenceNumber") or 0
        is_vessel = trigger in VESSEL_EVENTS

        candidate_legs = []
        side = None

        if trigger in START_TRIGGERS_DEFAULT and loc in dup_starts:
            candidate_legs = [l for l in sorted_legs
                              if (l.get("fromLocation") or "").strip().upper() == loc]
            side = "start"
        elif trigger in END_TRIGGERS_DEFAULT and loc in dup_ends:
            candidate_legs = [l for l in sorted_legs
                              if (l.get("toLocation") or "").strip().upper() == loc]
            side = "end"

        if not candidate_legs:
            next_remaining.append(cluster)
            continue

        # Filter by mode for vessel vs non-vessel (simplified: always restrict vessel to ocean)
        warnings = []
        if is_vessel:
            ocean_candidates = [l for l in candidate_legs if is_ocean(l)]
            if ocean_candidates:
                candidate_legs = ocean_candidates
                warnings.append(f"Vessel cluster {trigger} — restricted to OCEAN legs in duplicate resolution")
            else:
                warnings.append(f"Vessel cluster {trigger} — no OCEAN legs among candidates; using all modes")

        # Find best leg by sequence proximity
        best_leg = None
        best_reason = None

        # Check if cluster seq falls within range of any candidate leg's existing clusters
        for leg in sorted_legs:
            if leg not in candidate_legs:
                continue
            leg_seq = leg.get("legSequence")
            existing = leg_clusters_map.get(leg_seq) or []
            if existing:
                min_seq = min(c.get("clusterSequenceNumber", 0) for c in existing)
                max_seq = max(c.get("clusterSequenceNumber", 0) for c in existing)
                if min_seq <= cluster_seq <= max_seq:
                    best_leg = leg
                    best_reason = (
                        f"P4: Duplicate location {loc}; cluster sequence {cluster_seq} falls within "
                        f"range [{min_seq}–{max_seq}] of clusters already on leg {leg_seq}"
                    )
                    break

        # Fallback: nearest cluster sequence among candidate legs
        if best_leg is None:
            best_proximity = float("inf")
            for leg in sorted_legs:
                if leg not in candidate_legs:
                    continue
                leg_seq = leg.get("legSequence")
                existing = leg_clusters_map.get(leg_seq) or []
                prox = sequence_proximity(cluster_seq, existing)
                if prox < best_proximity:
                    best_proximity = prox
                    best_leg = leg
                    if existing:
                        best_reason = (
                            f"P4: Duplicate location {loc}; cluster sequence {cluster_seq} is "
                            f"closest (distance {prox:.0f}) to clusters on leg {leg_seq}"
                        )
                    else:
                        best_reason = (
                            f"P4: Duplicate location {loc}; leg {leg_seq} has no existing clusters — "
                            f"assigned by timestamp proximity to leg scheduled dates"
                        )

        if best_leg is None and candidate_legs:
            # Last resort: use timestamp proximity to leg dates
            cluster_ts = cluster.get("clusterTimestamp")
            best_diff = float("inf")
            for leg in candidate_legs:
                leg_ts = best_leg_timestamp(leg, side)
                diff = ts_diff_days(cluster_ts, leg_ts)
                if diff is not None and diff < best_diff:
                    best_diff = diff
                    best_leg = leg
                    best_reason = (
                        f"P4: Duplicate location {loc}; assigned to leg {leg.get('legSequence')} "
                        f"by timestamp proximity ({best_diff:.1f} days from scheduled date)"
                    )

        if best_leg:
            p4_clusters.append({
                **cluster,
                "simulatedLeg": best_leg.get("legSequence"),
                "stitchedLegFrom": best_leg.get("fromLocation"),
                "stitchedLegTo": best_leg.get("toLocation"),
                "stitchingPhase": "P4",
                "phaseReason": best_reason,
                "warnings": warnings,
            })
            leg_clusters_map[best_leg.get("legSequence")].append(cluster)
        else:
            next_remaining.append(cluster)

    results.extend(p4_clusters)
    remaining = next_remaining

    # --- Priority 5: Any location fallback ---
    p5_clusters = []
    next_remaining = []
    for cluster in remaining:
        loc = (cluster.get("clusterLocationCode") or "").strip().upper()
        matched_leg = None
        if loc:
            for leg in sorted_legs:
                leg_from = (leg.get("fromLocation") or "").strip().upper()
                leg_to = (leg.get("toLocation") or "").strip().upper()
                if loc == leg_from or loc == leg_to:
                    matched_leg = leg
                    break

        if matched_leg:
            side_match = "start" if (loc == (matched_leg.get("fromLocation") or "").upper()) else "end"
            p5_clusters.append({
                **cluster,
                "simulatedLeg": matched_leg.get("legSequence"),
                "stitchedLegFrom": matched_leg.get("fromLocation"),
                "stitchedLegTo": matched_leg.get("toLocation"),
                "stitchingPhase": "P5",
                "phaseReason": (
                    f"P5: Fallback any-location match; cluster location {loc} matches "
                    f"leg {matched_leg.get('legSequence')} {side_match}Location "
                    f"(trigger-side not enforced at this priority)"
                ),
                "warnings": [
                    f"Trigger {cluster.get('eventTrigger')} may be on wrong side — matched by fallback only"
                ],
            })
            leg_clusters_map[matched_leg.get("legSequence")].append(cluster)
        else:
            next_remaining.append(cluster)

    results.extend(p5_clusters)
    remaining = next_remaining

    # --- Priority 6: DELIVERY_CONFIRMED orphan → last leg ---
    p6_clusters = []
    next_remaining = []
    for cluster in remaining:
        trigger = cluster.get("eventTrigger") or ""
        if trigger == "DELIVERY_CONFIRMED":
            p6_clusters.append({
                **cluster,
                "simulatedLeg": last_leg.get("legSequence"),
                "stitchedLegFrom": last_leg.get("fromLocation"),
                "stitchedLegTo": last_leg.get("toLocation"),
                "stitchingPhase": "P6",
                "phaseReason": "P6: DELIVERY_CONFIRMED orphan — no location matched any leg; stitched to last leg",
                "warnings": ["DELIVERY_CONFIRMED had no location or did not match any leg boundary"],
            })
            leg_clusters_map[last_leg.get("legSequence")].append(cluster)
        else:
            next_remaining.append(cluster)

    results.extend(p6_clusters)
    remaining = next_remaining

    # --- Priority 7: True orphan ---
    for cluster in remaining:
        loc = cluster.get("clusterLocationCode")
        trigger = cluster.get("eventTrigger") or ""
        reason_parts = []
        if not loc:
            reason_parts.append("cluster has no location")
        else:
            reason_parts.append(f"cluster location {loc} did not match any leg boundary")
        if trigger not in START_TRIGGERS_DEFAULT and trigger not in END_TRIGGERS_DEFAULT:
            reason_parts.append(f"trigger {trigger} is not in any start/end trigger list")
        results.append({
            **cluster,
            "simulatedLeg": None,
            "stitchedLegFrom": None,
            "stitchedLegTo": None,
            "stitchingPhase": "P7",
            "phaseReason": f"P7: True orphan — {'; '.join(reason_parts)}. "
                           "Cluster appears in CorrelatedShipmentJourney.events (top-level), not on any leg.",
            "warnings": ["Cluster is orphaned — investigate whether transport plan covers this location"],
        })

    return results


# ---------------------------------------------------------------------------
# Cross-check against GIS events
# ---------------------------------------------------------------------------

def build_gis_index(gis_events):
    """Index GIS events by (trigger, location) for quick lookup."""
    idx = {}
    for ev in gis_events:
        key = (
            (ev.get("eventTrigger") or "").upper(),
            (ev.get("eventLocationCode") or "").upper(),
        )
        idx[key] = ev
    return idx


def cross_check(result, gis_index):
    """Add gisLeg and discrepancy fields by matching cluster trigger+location against GIS."""
    trigger = (result.get("eventTrigger") or "").upper()
    loc = (result.get("clusterLocationCode") or "").upper()
    key = (trigger, loc)
    gis_ev = gis_index.get(key)
    if gis_ev:
        gis_leg = gis_ev.get("stitchedLegSequence")
        result["gisLeg"] = gis_leg
        sim_leg = result.get("simulatedLeg")
        result["discrepancy"] = (
            sim_leg is not None and gis_leg is not None and str(sim_leg) != str(gis_leg)
        )
        if result["discrepancy"]:
            result["discrepancyNote"] = (
                f"DOS simulation places cluster on leg {sim_leg} "
                f"but GIS journey_request shows leg {gis_leg} — possible persistence issue"
            )
    else:
        result["gisLeg"] = None
        result["discrepancy"] = False
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    mce_data = load_json(args.clusters)
    tp_data = load_json(args.transport_plan)
    gis_data = load_json(args.gis_events) if args.gis_events else {}

    clusters = mce_data.get("clusters") or []
    legs = tp_data.get("legs") or []
    gis_events = gis_data.get("events") or []

    if not legs:
        print(json.dumps({
            "status": "NO_TRANSPORT_PLAN",
            "message": "No legs found in DOS transport plan",
        }, indent=2))
        return

    if not clusters:
        print(json.dumps({
            "status": "NO_CLUSTERS",
            "message": "No clusters found in MCE subscription_data",
            "storageType": mce_data.get("storageType"),
        }, indent=2))
        return

    results = simulate_stitching(clusters, legs)

    if gis_events:
        gis_index = build_gis_index(gis_events)
        results = [cross_check(r, gis_index) for r in results]

    orphans = [r for r in results if r.get("stitchingPhase") == "P7"]
    discrepancies = [r for r in results if r.get("discrepancy")]
    p5_fallbacks = [r for r in results if r.get("stitchingPhase") == "P5"]
    suspicious = orphans + discrepancies + p5_fallbacks

    # De-duplicate suspicious list
    seen = set()
    suspicious_deduped = []
    for r in suspicious:
        cid = r.get("clusterIdentifier")
        if cid not in seen:
            seen.add(cid)
            suspicious_deduped.append(r)

    output = {
        "status": "OK",
        "journeyId": mce_data.get("journeyId"),
        "unitOfTracking": mce_data.get("unitOfTracking"),
        "clusterCount": len(results),
        "suspiciousCount": len(suspicious_deduped),
        "results": results,
        "suspiciousClusterSummary": [
            {
                "clusterIdentifier": r.get("clusterIdentifier"),
                "clusterTimestamp": r.get("clusterTimestamp"),
                "clusterLocationCode": r.get("clusterLocationCode"),
                "clusterSequenceNumber": r.get("clusterSequenceNumber"),
                "eventTrigger": r.get("eventTrigger"),
                "stitchingPhase": r.get("stitchingPhase"),
                "simulatedLeg": r.get("simulatedLeg"),
                "gisLeg": r.get("gisLeg"),
                "discrepancy": r.get("discrepancy"),
                "phaseReason": r.get("phaseReason"),
                "warnings": r.get("warnings"),
            }
            for r in suspicious_deduped
        ],
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()