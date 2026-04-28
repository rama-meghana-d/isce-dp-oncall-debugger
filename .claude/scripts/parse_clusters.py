#!/usr/bin/env python3
"""
Full clustering + normalization report for a MCE JourneySubscription.

Outputs:
  1. Top-level summary   — journey/container, cluster count, provider breakdown
  2. Cluster index table — one row per cluster with key attributes
  3. Per-cluster detail  — for every cluster:
       a) Header block   (id, timestamp, timing, location, alt-codes, event count)
       b) Normalization trace (MP→MCE event→MCE cluster, deduped by unique value set)
       c) Complete events table (all constituent events, duplicates flagged)

Usage:
    # MCE-only (no MP comparison)
    curl -sf -H "api-version: 1" "http://localhost:8087/journey/ID/unitOfTracking/UOT" \\
      | python3 parse_clusters.py

    # Full three-level trace
    curl -sf -H "api-version: 1" "http://localhost:8081/journey/ID/unitOfTracking/UOT" \\
      > /tmp/mp_response.json
    curl -sf -H "api-version: 1" "http://localhost:8087/journey/ID/unitOfTracking/UOT" \\
      | python3 parse_clusters.py --mp /tmp/mp_response.json
"""
import argparse
import json
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone


# ── Time helpers ─────────────────────────────────────────────────────────────

def parse_ts(v):
    if not v or v == "-":
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_ts(v):
    dt = parse_ts(v)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else (str(v) if v else "-")


def ts_delta(a, b):
    """Signed human-readable delta: a − b."""
    da, db = parse_ts(a), parse_ts(b)
    if da is None or db is None:
        return ""
    s = (da - db).total_seconds()
    if s == 0:
        return "±0s"
    if abs(s) < 60:
        return f"{s:+.0f}s"
    if abs(s) < 3600:
        return f"{s / 60:+.0f}m"
    return f"{s / 3600:+.1f}h"


# ── Location helpers ──────────────────────────────────────────────────────────

def get_unloc(locs):
    if not locs:
        return "-"
    return (locs[0].get("location") or {}).get("unLocCode") or "-"


def get_alt_codes(locs):
    if not locs:
        return "none"
    parts = [
        f"{a.get('alternativeCodeType','?')}={a.get('alternativeCode','?')}"
        for a in ((locs[0].get("location") or {}).get("alternativeCodes") or [])
        if a.get("isActive", True)
    ]
    return " | ".join(parts) if parts else "none"


# ── Table helpers ─────────────────────────────────────────────────────────────

def col_widths(headers, rows):
    w = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            w[i] = max(w[i], len(str(cell)))
    return w


def tbl_sep(widths):
    return "| " + " | ".join("-" * w for w in widths) + " |"


def tbl_row(cells, widths):
    return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"


def print_table(headers, rows):
    w = col_widths(headers, rows)
    print(tbl_row(headers, w))
    print(tbl_sep(w))
    for r in rows:
        print(tbl_row(r, w))


def rule(char="─", width=100):
    print(char * width)


# ── MP index ──────────────────────────────────────────────────────────────────

def build_mp_index(mp_data):
    idx = {}
    for sub in mp_data.get("subscriptionData") or []:
        for dpd in sub.get("dataProviderData") or []:
            dp = dpd.get("dataProviderName", "UNKNOWN")
            for ev in dpd.get("events") or []:
                locs = ev.get("locations") or []
                key = (dp, ev.get("eventIdentifier")) if ev.get("eventIdentifier") else (
                    dp, ev.get("eventTrigger", "-"), ev.get("eventTiming", "-"), get_unloc(locs)
                )
                idx[key] = {
                    "trigger":   ev.get("eventTrigger", "-"),
                    "timing":    ev.get("eventTiming",  "-"),
                    "timestamp": fmt_ts(ev.get("timestamp", "-")),
                    "location":  get_unloc(locs),
                    "altCodes":  get_alt_codes(locs),
                }
    return idx


def mp_lookup(idx, dp, eid, trigger, timing, loc):
    return idx.get((dp, eid)) or idx.get((dp, trigger, timing, loc))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mp", metavar="FILE",
                        help="Path to Milestone Processor response JSON")
    args = parser.parse_args()

    try:
        mce_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"ERROR: Cannot parse MCE JSON: {e}", file=sys.stderr)
        sys.exit(1)

    mp_index = {}
    if args.mp:
        try:
            with open(args.mp) as f:
                mp_index = build_mp_index(json.load(f))
        except Exception as e:
            print(f"WARNING: Cannot load MP data: {e}", file=sys.stderr)

    journey_id = mce_data.get("journeyId", "?")
    uot_raw    = mce_data.get("unitOfTracking", "?")
    # unitOfTracking may be a dict {"unitOfTrackingIdentifier": ..., "unitOfTrackingType": ...}
    if isinstance(uot_raw, dict):
        uot = uot_raw.get("unitOfTrackingIdentifier", str(uot_raw))
    else:
        uot = str(uot_raw)

    # ── Parse MCE response ────────────────────────────────────────────────────
    clusters = defaultdict(lambda: {
        "clusterTimestamp": None, "clusterLocation": None,
        "clusterAltCodes": None,  "events": [],
    })
    unclustered = []

    for sub in mce_data.get("subscriptionData") or []:
        for dpd in sub.get("dataProviderData") or []:
            dp = dpd.get("dataProviderName", "UNKNOWN")
            for ev in dpd.get("events") or []:
                locs    = ev.get("locations") or []
                cluster = ev.get("cluster")
                row = {
                    "trigger":  ev.get("eventTrigger", "-"),
                    "timing":   ev.get("eventTiming",  "-"),
                    "dp":       dp,
                    "ts":       fmt_ts(ev.get("timestamp", "-")),
                    "loc":      get_unloc(locs),
                    "altCodes": get_alt_codes(locs),
                    "eid":      ev.get("eventIdentifier", "-"),
                }
                if cluster is None:
                    unclustered.append(row)
                    continue
                cid = cluster.get("clusterIdentifier", "?")
                c   = clusters[cid]
                c["events"].append(row)
                if c["clusterTimestamp"] is None:
                    c["clusterTimestamp"] = fmt_ts(cluster.get("clusterTimestamp", "-"))
                if c["clusterLocation"] is None:
                    c["clusterLocation"] = get_unloc(cluster.get("clusterLocations") or [])
                if c["clusterAltCodes"] is None:
                    c["clusterAltCodes"] = get_alt_codes(cluster.get("clusterLocations") or [])

    sorted_ids = sorted(
        clusters.keys(),
        key=lambda cid: (clusters[cid]["clusterTimestamp"] or "", cid),
    )
    total_events = sum(len(clusters[cid]["events"]) for cid in sorted_ids)

    # ── Provider summary ──────────────────────────────────────────────────────
    provider_counts: Counter = Counter()
    for cid in sorted_ids:
        for ev in clusters[cid]["events"]:
            provider_counts[ev["dp"]] += 1

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 1 — Top-level summary
    # ─────────────────────────────────────────────────────────────────────────
    rule("═")
    print(f"  CLUSTER REPORT")
    print(f"  Journey ID        : {journey_id}")
    print(f"  Unit of Tracking  : {uot}")
    print(f"  Total Clusters    : {len(sorted_ids)}"
          + (f"   (+{len(unclustered)} unclustered events)" if unclustered else ""))
    print(f"  Total Events      : {total_events}")
    print(f"  Data Providers    : "
          + ", ".join(f"{dp} ({n} events)" for dp, n in provider_counts.most_common()))
    if mp_index:
        print(f"  MP Events Indexed : {len(mp_index)}")
        print(f"  Trace Mode        : MP → MCE event → MCE cluster (three-level)")
    else:
        print(f"  Trace Mode        : MCE event → MCE cluster (run with --mp for full trace)")
    rule("═")
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 2 — Cluster index table
    # ─────────────────────────────────────────────────────────────────────────
    print("## Cluster Index\n")
    idx_headers = ["#", "Cluster ID", "Event Trigger", "Timing",
                   "Cluster Timestamp", "Location", "Alt Codes", "Events", "Providers"]
    idx_rows = []
    for i, cid in enumerate(sorted_ids, 1):
        c      = clusters[cid]
        evts   = c["events"]
        trigs  = [e["trigger"] for e in evts]
        tims   = [e["timing"]  for e in evts]
        label_t = max(set(trigs), key=trigs.count) if trigs else "-"
        label_m = max(set(tims),  key=tims.count)  if tims  else "-"
        dps    = sorted(set(e["dp"] for e in evts))
        idx_rows.append([
            i, cid, label_t, label_m,
            c["clusterTimestamp"], c["clusterLocation"], c["clusterAltCodes"],
            len(evts), ", ".join(dps),
        ])
    print_table(idx_headers, idx_rows)
    print()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 3 — Per-cluster detail
    # ─────────────────────────────────────────────────────────────────────────
    print("## Cluster Detail\n")

    for seq, cid in enumerate(sorted_ids, 1):
        c         = clusters[cid]
        all_evts  = sorted(c["events"], key=lambda e: (e["trigger"], e["dp"], e["ts"]))
        cluster_ts  = c["clusterTimestamp"]
        cluster_loc = c["clusterLocation"]

        trigs  = [e["trigger"] for e in all_evts]
        tims   = [e["timing"]  for e in all_evts]
        label_t = max(set(trigs), key=trigs.count) if trigs else "-"
        label_m = max(set(tims),  key=tims.count)  if tims  else "-"

        rule("─")
        print(f"### Cluster {seq} of {len(sorted_ids)}  (ID: {cid})  —  {label_t} / {label_m}\n")

        # ── 3a Header block ──────────────────────────────────────────────────
        print(f"  Cluster ID        : {cid}")
        print(f"  Cluster Timestamp : {cluster_ts}")
        print(f"  Cluster Timing    : {label_m}")
        print(f"  Cluster Location  : {cluster_loc}")
        print(f"  Cluster Alt Codes : {c['clusterAltCodes']}")
        dps_in_cluster = sorted(set(e["dp"] for e in all_evts))
        print(f"  Data Providers    : {', '.join(dps_in_cluster)}")
        print(f"  Event Count       : {len(all_evts)}")

        # Duplicate summary
        sig_counter: Counter = Counter(
            (e["dp"], e["trigger"], e["timing"], e["ts"], e["loc"]) for e in all_evts
        )
        n_dupes = sum(v - 1 for v in sig_counter.values())
        n_unique = len(sig_counter)
        print(f"  Unique Signatures : {n_unique}  ({n_dupes} duplicate event(s) in this cluster)")
        print()

        # ── 3b Normalization trace ───────────────────────────────────────────
        print("  #### Normalization Trace\n")

        # Check if any event is the timestamp source (Δ ±0s)
        ts_source_dps = [e["dp"] for e in all_evts if ts_delta(e["ts"], cluster_ts) == "±0s"]

        if mp_index:
            # Group events by unique (dp, trigger, timing, ts, loc) and process once per group
            seen_sigs: dict = {}
            for e in all_evts:
                sig = (e["dp"], e["trigger"], e["timing"], e["ts"], e["loc"])
                if sig not in seen_sigs:
                    seen_sigs[sig] = 0
                seen_sigs[sig] += 1

            for sig, count in sorted(seen_sigs.items(),
                                     key=lambda x: (x[0][0], x[0][2], x[0][3])):
                dp, trigger, timing, ev_ts, ev_loc = sig
                mp = mp_lookup(mp_index, dp, None, trigger, timing, ev_loc)
                dup_note = f"  [×{count} identical events in this cluster]" if count > 1 else ""

                # MP layer
                if mp:
                    print(f"  {dp}{dup_note}")
                    print(f"    MP output  : trigger={mp['trigger']:<30} timing={mp['timing']:<12} "
                          f"ts={mp['timestamp']:<22} loc={mp['location']}")
                else:
                    print(f"  {dp}{dup_note}  [no MP match — event arrived after MP snapshot or id mismatch]")

                # MCE event layer — diff vs MP
                mce_notes = []
                if mp:
                    if trigger  != mp["trigger"]:  mce_notes.append(f"trigger {mp['trigger']}→{trigger}")
                    if timing   != mp["timing"]:   mce_notes.append(f"timing {mp['timing']}→{timing}")
                    if ev_loc   != mp["location"]: mce_notes.append(f"loc {mp['location']}→{ev_loc}")
                    if ev_ts    != mp["timestamp"]: mce_notes.append(f"ts delta {ts_delta(ev_ts, mp['timestamp'])}")
                mce_note_str = "; ".join(mce_notes) if mce_notes else "(matches MP — no changes)"
                print(f"    MCE event  : trigger={trigger:<30} timing={timing:<12} "
                      f"ts={ev_ts:<22} loc={ev_loc}  →  {mce_note_str}")

                # MCE cluster layer — diff vs event
                cluster_notes = []
                delta = ts_delta(ev_ts, cluster_ts)
                if ev_loc not in ("-", cluster_loc):
                    cluster_notes.append(f"loc {ev_loc}→{cluster_loc}")
                if delta == "±0s":
                    cluster_notes.append("ts: THIS EVENT selected as authoritative")
                else:
                    cluster_notes.append(f"ts: {delta} from cluster (not selected as authoritative)")
                print(f"    MCE cluster: trigger={label_t:<30} timing={label_m:<12} "
                      f"ts={cluster_ts:<22} loc={cluster_loc}  →  {'; '.join(cluster_notes)}")
                print()

        else:
            # MCE-only trace (no MP data)
            seen_sigs = {}
            for e in all_evts:
                sig = (e["dp"], e["trigger"], e["timing"], e["ts"], e["loc"])
                seen_sigs[sig] = seen_sigs.get(sig, 0) + 1

            for sig, count in sorted(seen_sigs.items()):
                dp, trigger, timing, ev_ts, ev_loc = sig
                dup_note = f"  [×{count}]" if count > 1 else ""
                delta    = ts_delta(ev_ts, cluster_ts)
                loc_note = f"loc {ev_loc}→{cluster_loc}" if ev_loc not in ("-", cluster_loc) else "loc: matches cluster"
                ts_note  = ("ts: THIS EVENT selected as authoritative" if delta == "±0s"
                            else f"ts: {delta} from cluster (not selected)")
                print(f"  {dp}{dup_note}")
                print(f"    Event  : trigger={trigger}  timing={timing}  ts={ev_ts}  loc={ev_loc}")
                print(f"    Cluster: {loc_note};  {ts_note}")
                print()

        # Timestamp authority note
        if ts_source_dps:
            print(f"  Timestamp authority : {', '.join(set(ts_source_dps))} (Δ ±0s)")
        else:
            print(f"  Timestamp authority : No provider at Δ ±0s — MCE derived cluster timestamp")
            print(f"                        algorithmically (timezone correction or interpolation).")
        print()

        # ── 3c Complete events table ─────────────────────────────────────────
        print("  #### All Constituent Events\n")

        # Mark duplicates: events with the same (dp, trigger, timing, ts, loc) get DUP flag
        sig_seen: set = set()
        ev_headers = ["#", "Data Provider", "Event Trigger", "Timing",
                      "Event Timestamp", "Δ vs Cluster", "Event Location",
                      "Event Alt Codes", "Loc Changed", "Dup"]
        ev_rows = []
        for i, e in enumerate(all_evts, 1):
            sig  = (e["dp"], e["trigger"], e["timing"], e["ts"], e["loc"])
            dup  = "YES" if sig in sig_seen else "NO"
            sig_seen.add(sig)
            delta    = ts_delta(e["ts"], cluster_ts)
            loc_chg  = "YES" if e["loc"] not in ("-", cluster_loc) else "NO"
            ev_rows.append([
                i, e["dp"], e["trigger"], e["timing"],
                e["ts"], delta, e["loc"], e["altCodes"], loc_chg, dup,
            ])

        # indent table
        w = col_widths(ev_headers, ev_rows)
        print("  " + tbl_row(ev_headers, w))
        print("  " + tbl_sep(w))
        for r in ev_rows:
            print("  " + tbl_row(r, w))
        print()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 4 — Unclustered events
    # ─────────────────────────────────────────────────────────────────────────
    if unclustered:
        rule("─")
        print(f"### UNCLUSTERED EVENTS  ({len(unclustered)} total)\n")
        evts   = sorted(unclustered, key=lambda e: (e["trigger"], e["dp"]))
        hdrs   = ["#", "Data Provider", "Event Trigger", "Timing",
                  "Event Timestamp", "Event Location", "Event Alt Codes"]
        rows   = [[i, e["dp"], e["trigger"], e["timing"],
                   e["ts"], e["loc"], e["altCodes"]]
                  for i, e in enumerate(evts, 1)]
        print_table(hdrs, rows)
        print()
        print("  These events have not been clustered by MCE yet.")
        print("  Check Kafka consumer lag on milestone-topic, or trigger a /replay.\n")

    rule("═")
    print(f"  END OF REPORT  |  {len(sorted_ids)} clusters  |  {total_events} events")
    rule("═")


if __name__ == "__main__":
    main()