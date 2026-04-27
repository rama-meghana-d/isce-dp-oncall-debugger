#!/usr/bin/env python3
"""
Parse MCE journey_subscription query result.

Reads the MCP query output (subscription_data JSON column from journey_subscription table)
and extracts compact event-to-cluster relationships for stitching analysis.

Handles both IN_MEMORY (subscription_data present) and BLOB storage cases.

Usage:
  python3 parse_mce_cluster_output.py <result_file.json> \
    [--trigger VESSEL_DEPARTURE] \
    [--location CNSHA]

Output: compact JSON with storage info, cluster events, and per-event stitching context.
"""
import json
import sys
import argparse


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("file", help="Path to MCP query result JSON file")
    p.add_argument("--trigger", help="Filter by eventTrigger")
    p.add_argument("--location", help="Filter by eventLocationCode (LOCODE)")
    return p.parse_args()


def unwrap_mcp_response(data):
    rows = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "text" in item:
                inner = item["text"]
                if isinstance(inner, str):
                    inner = json.loads(inner)
                rows.extend(inner if isinstance(inner, list) else [inner])
            else:
                rows.append(item)
    elif isinstance(data, dict):
        rows = data.get("rows") or [data]
    return rows


def safe_json(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, json.JSONDecodeError):
        return None


def extract_location_code(loc):
    if not isinstance(loc, dict):
        return str(loc) if loc else None
    return (
        loc.get("UNLocationCode")
        or loc.get("locationCode")
        or loc.get("location_code")
        or loc.get("unLocationCode")
    )


def extract_events_from_cluster(cluster, cluster_id, row_meta):
    """Extract normalized events from a single cluster object."""
    events = []
    selected_leg = cluster.get("selectedLeg") or cluster.get("legSequence") or cluster.get("leg_sequence")
    candidate_legs = cluster.get("candidateLegs") or cluster.get("candidates") or []
    matched_reason = cluster.get("matchedReason") or cluster.get("reason") or cluster.get("selectionReason")
    applied_rules = cluster.get("appliedRules") or cluster.get("rules") or []
    fused_event_id = cluster.get("fusedEventId") or cluster.get("fused_event_id")

    source_events = (
        cluster.get("events")
        or cluster.get("sourceEvents")
        or cluster.get("milestones")
        or []
    )

    for ev in source_events:
        if not isinstance(ev, dict):
            continue
        trigger = ev.get("eventTrigger") or ev.get("event_trigger")
        if not trigger:
            continue

        loc = ev.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        loc_code = extract_location_code(loc)
        loc_func = loc.get("locationType") or loc.get("locationFunction") if isinstance(loc, dict) else None

        events.append({
            "journeyId": row_meta.get("journey_id"),
            "unitOfTracking": row_meta.get("unit_of_tracking"),
            "clusterId": cluster_id,
            "fusedEventId": fused_event_id,
            "eventIdentifier": ev.get("eventID") or ev.get("id") or ev.get("eventIdentifier"),
            "eventTrigger": trigger,
            "eventTiming": ev.get("eventTiming") or ev.get("event_timing"),
            "eventTimestamp": ev.get("timestamp") or ev.get("eventTimestamp"),
            "eventLocationCode": loc_code,
            "eventLocationFunction": loc_func,
            "provider": ev.get("dataProvider") or ev.get("provider") or ev.get("source"),
            "jobId": ev.get("jobId") or ev.get("job_id"),
            "selectedLeg": selected_leg,
            "candidateLegs": candidate_legs,
            "matchedReason": matched_reason,
            "appliedRules": applied_rules[:5] if isinstance(applied_rules, list) else applied_rules,
        })

    # If no source events but the cluster itself has event fields, treat it as a single event
    if not events:
        trigger = cluster.get("eventTrigger") or cluster.get("event_trigger")
        if trigger:
            loc = cluster.get("location") or {}
            loc_code = extract_location_code(loc)
            events.append({
                "journeyId": row_meta.get("journey_id"),
                "unitOfTracking": row_meta.get("unit_of_tracking"),
                "clusterId": cluster_id,
                "fusedEventId": fused_event_id,
                "eventIdentifier": cluster.get("eventID") or cluster.get("id"),
                "eventTrigger": trigger,
                "eventTiming": cluster.get("eventTiming"),
                "eventTimestamp": cluster.get("timestamp"),
                "eventLocationCode": loc_code,
                "eventLocationFunction": None,
                "provider": cluster.get("dataProvider") or cluster.get("provider"),
                "jobId": cluster.get("jobId"),
                "selectedLeg": selected_leg,
                "candidateLegs": candidate_legs,
                "matchedReason": matched_reason,
                "appliedRules": applied_rules[:5] if isinstance(applied_rules, list) else applied_rules,
            })

    return events


def parse_subscription_data(sub_data, row_meta):
    """Walk the subscription_data JSON to extract all cluster events."""
    all_events = []
    if not isinstance(sub_data, dict):
        return all_events

    # Try top-level clusters list
    for clusters_key in ("clusters", "eventClusters", "milestoneGroups", "groups"):
        clusters = sub_data.get(clusters_key)
        if isinstance(clusters, list):
            for i, cluster in enumerate(clusters):
                if not isinstance(cluster, dict):
                    continue
                cluster_id = cluster.get("clusterId") or cluster.get("id") or f"cluster_{i}"
                all_events.extend(extract_events_from_cluster(cluster, cluster_id, row_meta))
            if all_events:
                return all_events

    # Try flat events list (no cluster grouping)
    for ev_key in ("events", "milestones", "sourceEvents"):
        evs = sub_data.get(ev_key)
        if isinstance(evs, list):
            for ev in evs:
                if not isinstance(ev, dict):
                    continue
                cluster_id = ev.get("clusterId") or ev.get("cluster_id") or "unknown"
                all_events.extend(extract_events_from_cluster(ev, cluster_id, row_meta))
            if all_events:
                return all_events

    return all_events


def main():
    args = parse_args()
    with open(args.file) as f:
        data = json.load(f)

    rows = unwrap_mcp_response(data)
    all_events = []
    blob_url = None
    storage_type = "UNKNOWN"
    row_meta = {}

    for row in rows:
        row_meta = {
            "journey_id": row.get("journey_id"),
            "unit_of_tracking": row.get("unit_of_tracking"),
        }
        storage_type = row.get("storage_type") or "IN_MEMORY"
        blob_url = row.get("subscription_data_blob_url")

        sub_data = safe_json(row.get("subscription_data"))
        if sub_data:
            all_events.extend(parse_subscription_data(sub_data, row_meta))

    # Apply filters
    if args.trigger:
        all_events = [e for e in all_events if e.get("eventTrigger") == args.trigger]
    if args.location:
        all_events = [e for e in all_events if e.get("eventLocationCode") == args.location]

    if storage_type == "BLOB" and not all_events:
        result = {
            "status": "BLOB_STORAGE",
            "message": "subscription_data is stored in external blob — cannot parse in-session",
            "blobUrl": blob_url,
            "journeyId": row_meta.get("journey_id"),
            "unitOfTracking": row_meta.get("unit_of_tracking"),
            "events": [],
        }
    else:
        result = {
            "status": "OK" if all_events else "NOT_FOUND",
            "storageType": storage_type,
            "journeyId": row_meta.get("journey_id"),
            "unitOfTracking": row_meta.get("unit_of_tracking"),
            "count": len(all_events),
            "events": all_events,
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
