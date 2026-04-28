#!/usr/bin/env python3
"""
Parse MCE journey_subscription query result.

Traverses subscriptionData → dataProviderData → events → cluster to extract
JourneyCluster data as DOS sees it. Groups events by clusterIdentifier and assigns
clusterSequenceNumber by sorting on clusterTimestamp (mirrors ClusteredShipmentJourneyUtility).

Handles both IN_MEMORY (subscription_data present) and BLOB storage cases.

Usage:
  python3 parse_mce_cluster_output.py <result_file.json> \
    [--trigger VESSEL_DEPARTURE] \
    [--location CNSHA]

Output: JSON with per-cluster data including clusterIdentifier, clusterTimestamp,
        clusterLocationCode, clusterSequenceNumber, eventTrigger, providers, events[].
"""
import json
import sys
import argparse
from collections import defaultdict


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("file", help="Path to MCP query result JSON file")
    p.add_argument("--trigger", help="Filter clusters by eventTrigger of representative event")
    p.add_argument("--location", help="Filter clusters by clusterLocationCode (UNLOC)")
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


def extract_unloc(location_wrapper):
    """Extract UNLOC code from a LocationWrapper object (mirrors LocationMatchingUtility)."""
    if not isinstance(location_wrapper, dict):
        return None
    location = location_wrapper.get("location") or {}
    if not isinstance(location, dict):
        return None

    # Try alternativeCodes for PORT_UN_LOCODE first
    for alt in (location.get("alternativeCodes") or []):
        if isinstance(alt, dict) and alt.get("alternativeCodeType") == "PORT_UN_LOCODE":
            code = alt.get("alternativeCode")
            if code:
                return code

    # Then unLocCode directly
    unloc = location.get("unLocCode") or location.get("UNLocationCode")
    if unloc:
        return unloc

    # Then countryCityCode or cityCode
    return location.get("countryCityCode") or location.get("cityCode") or None


def extract_facility_code(location_wrapper):
    """Extract facility code from a LocationWrapper."""
    if not isinstance(location_wrapper, dict):
        return None
    facility = location_wrapper.get("facility") or {}
    if isinstance(facility, dict):
        return facility.get("facilityCode")
    return None


def extract_cluster_location(cluster_locations):
    """Get the representative location code from a clusterLocations list."""
    if not cluster_locations or not isinstance(cluster_locations, list):
        return None, None
    first = cluster_locations[0] if cluster_locations else {}
    unloc = extract_unloc(first)
    facility = extract_facility_code(first)
    # UNLOC takes priority over facility code (mirrors LocationMatchingUtility priority)
    return unloc or facility, facility


def parse_subscription_data(sub_data, row_meta):
    """
    Walk subscriptionData → dataProviderData → events → cluster.
    Groups events by clusterIdentifier and returns a cluster map.
    Returns: dict[int, dict] cluster_id → cluster_info
    """
    cluster_map = {}

    if not isinstance(sub_data, dict):
        return cluster_map

    # The root field is subscriptionData (aliases: SubscriptionEvents, subscriptionEvents)
    subscription_data_list = (
        sub_data.get("subscriptionData")
        or sub_data.get("SubscriptionEvents")
        or sub_data.get("subscriptionEvents")
        or []
    )

    if not isinstance(subscription_data_list, list):
        return cluster_map

    for subscription in subscription_data_list:
        if not isinstance(subscription, dict):
            continue

        data_provider_data_list = (
            subscription.get("dataProviderData")
            or subscription.get("DataProviderEvents")
            or subscription.get("dataProviderEvents")
            or []
        )

        if not isinstance(data_provider_data_list, list):
            continue

        for dp_data in data_provider_data_list:
            if not isinstance(dp_data, dict):
                continue

            provider_name = (
                dp_data.get("dataProviderName")
                or dp_data.get("DataProviderName")
                or "UNKNOWN"
            )

            events = (
                dp_data.get("events")
                or dp_data.get("Events")
                or []
            )

            if not isinstance(events, list):
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue

                cluster_obj = event.get("cluster") or event.get("Cluster")
                if not isinstance(cluster_obj, dict):
                    continue

                cluster_id = (
                    cluster_obj.get("clusterIdentifier")
                    or cluster_obj.get("ClusterIdentifier")
                )
                if cluster_id is None:
                    continue

                cluster_ts = (
                    cluster_obj.get("clusterTimestamp")
                    or cluster_obj.get("ClusterTimestamp")
                )
                cluster_locs = (
                    cluster_obj.get("clusterLocations")
                    or cluster_obj.get("ClusterLocations")
                    or []
                )
                cluster_loc_code, cluster_facility_code = extract_cluster_location(cluster_locs)

                if cluster_id not in cluster_map:
                    event_trigger = (
                        event.get("eventTrigger")
                        or event.get("EventTrigger")
                    )
                    cluster_map[cluster_id] = {
                        "clusterIdentifier": cluster_id,
                        "clusterTimestamp": cluster_ts,
                        "clusterLocationCode": cluster_loc_code,
                        "clusterFacilityCode": cluster_facility_code,
                        "clusterSequenceNumber": None,  # assigned after sort
                        "eventTrigger": event_trigger,  # from first event
                        "providers": [],
                        "events": [],
                    }

                cluster_entry = cluster_map[cluster_id]

                if provider_name not in cluster_entry["providers"]:
                    cluster_entry["providers"].append(provider_name)

                ev_loc_code = None
                ev_locs = event.get("locations") or event.get("Locations") or []
                if isinstance(ev_locs, list) and ev_locs:
                    ev_loc_code = extract_unloc(ev_locs[0])

                cluster_entry["events"].append({
                    "eventTrigger": event.get("eventTrigger") or event.get("EventTrigger"),
                    "eventTiming": event.get("eventTiming") or event.get("EventTiming"),
                    "eventTimestamp": event.get("timestamp") or event.get("eventTimestamp"),
                    "eventLocationCode": ev_loc_code,
                    "provider": provider_name,
                    "eventIdentifier": event.get("eventIdentifier") or event.get("eventCorrelationId"),
                })

    return cluster_map


def assign_sequence_numbers(clusters):
    """Sort clusters by clusterTimestamp (then clusterIdentifier) and assign 1-based sequence numbers."""
    def sort_key(c):
        ts = c.get("clusterTimestamp") or ""
        cid = c.get("clusterIdentifier") or 0
        return (ts, cid)

    sorted_clusters = sorted(clusters, key=sort_key)
    for i, cluster in enumerate(sorted_clusters):
        cluster["clusterSequenceNumber"] = i + 1
    return sorted_clusters


def main():
    args = parse_args()
    with open(args.file) as f:
        data = json.load(f)

    rows = unwrap_mcp_response(data)
    all_cluster_map = {}
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
            cluster_map = parse_subscription_data(sub_data, row_meta)
            all_cluster_map.update(cluster_map)

    all_clusters = list(all_cluster_map.values())
    all_clusters = assign_sequence_numbers(all_clusters)

    # Apply filters
    if args.trigger:
        all_clusters = [c for c in all_clusters if c.get("eventTrigger") == args.trigger]
    if args.location:
        all_clusters = [
            c for c in all_clusters
            if c.get("clusterLocationCode") == args.location
            or c.get("clusterFacilityCode") == args.location
        ]

    if storage_type == "BLOB" and not all_clusters:
        result = {
            "status": "BLOB_STORAGE",
            "message": "subscription_data is stored in external blob — cannot parse in-session",
            "blobUrl": blob_url,
            "journeyId": row_meta.get("journey_id"),
            "unitOfTracking": row_meta.get("unit_of_tracking"),
            "clusters": [],
        }
    else:
        result = {
            "status": "OK" if all_clusters else "NOT_FOUND",
            "storageType": storage_type,
            "journeyId": row_meta.get("journey_id"),
            "unitOfTracking": row_meta.get("unit_of_tracking"),
            "clusterCount": len(all_clusters),
            "clusters": all_clusters,
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()