#!/usr/bin/env python3
"""
Parse a JourneySubscription JSON (from Milestone Processor API) and print a
cluster-by-cluster breakdown showing constituent events, their data providers,
timestamps, and location codes.

Usage:
    curl ... | python3 parse_clusters.py
"""
import json
import sys
from collections import defaultdict


def get_unloc(locations):
    if not locations:
        return "-"
    loc = locations[0].get("location") or {}
    return loc.get("unLocCode") or "-"


def get_cluster_alt_codes(cluster_locations):
    if not cluster_locations:
        return "none"
    loc = cluster_locations[0].get("location") or {}
    alt_codes = loc.get("alternativeCodes") or []
    if not alt_codes:
        return "none"
    parts = [
        f"{a.get('alternativeCodeType', '?')}={a.get('alternativeCode', '?')}"
        for a in alt_codes
        if a.get("isActive", True)
    ]
    return " | ".join(parts) if parts else "none"


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse JSON response: {e}", file=sys.stderr)
        sys.exit(1)

    journey_id = data.get("journeyId", "?")
    uot = data.get("unitOfTracking", "?")

    # cluster_id -> {meta, events}
    clusters = defaultdict(lambda: {"clusterTimestamp": None, "clusterLocation": None, "altCodes": None, "events": []})
    unclustered = []

    for sub in data.get("subscriptionData") or []:
        for dpd in sub.get("dataProviderData") or []:
            dp_name = dpd.get("dataProviderName", "UNKNOWN")
            for event in dpd.get("events") or []:
                cluster = event.get("cluster")
                event_trigger = event.get("eventTrigger", "-")
                event_timing = event.get("eventTiming", "-")
                event_ts = event.get("timestamp", "-")
                event_loc = get_unloc(event.get("locations") or [])
                event_id = event.get("eventIdentifier", "-")

                row = {
                    "eventTrigger": event_trigger,
                    "eventTiming": event_timing,
                    "dataProvider": dp_name,
                    "timestamp": event_ts,
                    "location": event_loc,
                    "eventId": event_id,
                }

                if cluster is None:
                    unclustered.append(row)
                    continue

                cid = cluster.get("clusterIdentifier", "?")
                c = clusters[cid]
                c["events"].append(row)
                if c["clusterTimestamp"] is None:
                    c["clusterTimestamp"] = cluster.get("clusterTimestamp", "-")
                if c["clusterLocation"] is None:
                    c["clusterLocation"] = get_unloc(cluster.get("clusterLocations") or [])
                if c["altCodes"] is None:
                    c["altCodes"] = get_cluster_alt_codes(cluster.get("clusterLocations") or [])

    # sort clusters by clusterTimestamp then by id
    sorted_ids = sorted(clusters.keys(), key=lambda cid: (clusters[cid]["clusterTimestamp"] or "", cid))

    total = len(sorted_ids) + (1 if unclustered else 0)
    print(f"\n## Cluster Report — {journey_id} / {uot}")
    print(f"Total clusters: {len(sorted_ids)}" + (f"  (+{len(unclustered)} unclustered events)" if unclustered else ""))
    print()

    for cid in sorted_ids:
        c = clusters[cid]
        events = sorted(c["events"], key=lambda e: (e["eventTrigger"], e["dataProvider"]))

        # Derive cluster label from constituent events (most common trigger/timing)
        triggers = [e["eventTrigger"] for e in events]
        timings = [e["eventTiming"] for e in events]
        label_trigger = max(set(triggers), key=triggers.count) if triggers else "-"
        label_timing = max(set(timings), key=timings.count) if timings else "-"

        print(f"### Cluster {cid} — {label_trigger} / {label_timing}")
        print(f"Cluster Timestamp : {c['clusterTimestamp']}")
        print(f"Cluster Location  : {c['clusterLocation']}")
        print(f"Alt Codes         : {c['altCodes']}")
        print()

        col_widths = {
            "num": 3,
            "trigger": max(len("Event Trigger"), max((len(e["eventTrigger"]) for e in events), default=0)),
            "timing": max(len("Timing"), max((len(e["eventTiming"]) for e in events), default=0)),
            "dp": max(len("Data Provider"), max((len(e["dataProvider"]) for e in events), default=0)),
            "ts": max(len("Event Timestamp"), max((len(e["timestamp"]) for e in events), default=0)),
            "loc": max(len("Location"), max((len(e["location"]) for e in events), default=0)),
        }

        def row_fmt(num, trigger, timing, dp, ts, loc):
            return (
                f"| {str(num):<{col_widths['num']}} "
                f"| {trigger:<{col_widths['trigger']}} "
                f"| {timing:<{col_widths['timing']}} "
                f"| {dp:<{col_widths['dp']}} "
                f"| {ts:<{col_widths['ts']}} "
                f"| {loc:<{col_widths['loc']}} |"
            )

        sep = (
            f"| {'-'*col_widths['num']} "
            f"| {'-'*col_widths['trigger']} "
            f"| {'-'*col_widths['timing']} "
            f"| {'-'*col_widths['dp']} "
            f"| {'-'*col_widths['ts']} "
            f"| {'-'*col_widths['loc']} |"
        )

        print(row_fmt("#", "Event Trigger", "Timing", "Data Provider", "Event Timestamp", "Location"))
        print(sep)
        for i, e in enumerate(events, 1):
            print(row_fmt(i, e["eventTrigger"], e["eventTiming"], e["dataProvider"], e["timestamp"], e["location"]))
        print()

    if unclustered:
        print("### UNCLUSTERED events")
        print()
        events = sorted(unclustered, key=lambda e: (e["eventTrigger"], e["dataProvider"]))
        col_widths = {
            "num": 3,
            "trigger": max(len("Event Trigger"), max((len(e["eventTrigger"]) for e in events), default=0)),
            "timing": max(len("Timing"), max((len(e["eventTiming"]) for e in events), default=0)),
            "dp": max(len("Data Provider"), max((len(e["dataProvider"]) for e in events), default=0)),
            "ts": max(len("Event Timestamp"), max((len(e["timestamp"]) for e in events), default=0)),
            "loc": max(len("Location"), max((len(e["location"]) for e in events), default=0)),
        }

        def row_fmt2(num, trigger, timing, dp, ts, loc):
            return (
                f"| {str(num):<{col_widths['num']}} "
                f"| {trigger:<{col_widths['trigger']}} "
                f"| {timing:<{col_widths['timing']}} "
                f"| {dp:<{col_widths['dp']}} "
                f"| {ts:<{col_widths['ts']}} "
                f"| {loc:<{col_widths['loc']}} |"
            )

        sep2 = (
            f"| {'-'*col_widths['num']} "
            f"| {'-'*col_widths['trigger']} "
            f"| {'-'*col_widths['timing']} "
            f"| {'-'*col_widths['dp']} "
            f"| {'-'*col_widths['ts']} "
            f"| {'-'*col_widths['loc']} |"
        )

        print(row_fmt2("#", "Event Trigger", "Timing", "Data Provider", "Event Timestamp", "Location"))
        print(sep2)
        for i, e in enumerate(events, 1):
            print(row_fmt2(i, e["eventTrigger"], e["eventTiming"], e["dataProvider"], e["timestamp"], e["location"]))
        print()


if __name__ == "__main__":
    main()
