#!/usr/bin/env python3
"""
Parse GIS shipment_journey_transaction query result.

Reads the MCP query output (from journey_request JSONB column) and extracts
normalized event records showing which event is stitched to which transport plan leg.

Usage:
  python3 parse_gis_shipment_journey_transaction.py <result_file.json> \
    [--trigger VESSEL_DEPARTURE] \
    [--location CNSHA]

Output: compact JSON with status, count, and list of normalized event records.
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
    """Handle [{"text": "..."}] wrapper or plain row list."""
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


def extract_location_function(loc):
    if not isinstance(loc, dict):
        return None
    return (
        loc.get("locationType")
        or loc.get("locationTypeEnum")
        or loc.get("locationFunction")
        or loc.get("location_function")
    )


def extract_ref_values(refs):
    vessel_name, vessel_imo, voyage = None, None, None
    if not isinstance(refs, list):
        return vessel_name, vessel_imo, voyage
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_type = ref.get("referenceTypeEnum") or ref.get("type") or ""
        ref_val = ref.get("reference") or ref.get("value") or ""
        if "VESSEL_NAME" in ref_type:
            vessel_name = ref_val
        elif "VESSEL_IMO" in ref_type:
            vessel_imo = ref_val
        elif "VOYAGE" in ref_type:
            voyage = ref_val
    return vessel_name, vessel_imo, voyage


def normalize_event(ev, leg_seq, leg_from, leg_to, row_meta):
    trigger = ev.get("eventTrigger") or ev.get("event_trigger")
    if not trigger:
        return None

    ts = ev.get("timestamp") or ev.get("eventTimestamp") or ev.get("event_timestamp")
    timing = ev.get("eventTiming") or ev.get("event_timing")
    mode = ev.get("transportMode") or ev.get("mode") or ev.get("transport_mode")

    locs = ev.get("locations") or ev.get("location") or []
    if isinstance(locs, dict):
        locs = [locs]
    loc_code, loc_func = None, None
    for loc in locs:
        loc_code = extract_location_code(loc) or loc_code
        loc_func = extract_location_function(loc) or loc_func

    refs = ev.get("references") or ev.get("eventConnections") or []
    vessel_name, vessel_imo, voyage = extract_ref_values(refs)

    return {
        "journeyId": row_meta.get("journey_id"),
        "unitOfTracking": row_meta.get("unit_of_tracking"),
        "eventIdentifier": ev.get("eventID") or ev.get("id") or ev.get("eventIdentifier"),
        "eventTrigger": trigger,
        "eventTiming": timing,
        "eventTimestamp": ts,
        "eventLocationCode": loc_code,
        "eventLocationFunction": loc_func,
        "mode": mode,
        "vesselName": vessel_name,
        "vesselImo": vessel_imo,
        "voyage": voyage,
        "stitchedLegSequence": leg_seq,
        "stitchedLegFrom": leg_from,
        "stitchedLegTo": leg_to,
        "rawReference": json.dumps(refs)[:300] if refs else None,
    }


def extract_events_from_journey(journey_obj, row_meta):
    """Walk the journey JSON and extract events with their leg context."""
    events = []
    if not isinstance(journey_obj, dict):
        return events

    # Try standard leg structures
    for legs_key in ("shipmentJourneyLegs", "legs", "transportLegs", "transportPlan"):
        legs = journey_obj.get(legs_key)
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            leg_seq = leg.get("legSequence") or leg.get("sequence") or leg.get("leg_sequence")
            from_loc = extract_location_code(leg.get("fromLocation") or leg.get("from") or {})
            to_loc = extract_location_code(leg.get("toLocation") or leg.get("to") or {})
            for ev_key in ("events", "milestones", "transportEvents", "portCallEvents"):
                for ev in (leg.get(ev_key) or []):
                    record = normalize_event(ev, leg_seq, from_loc, to_loc, row_meta)
                    if record:
                        events.append(record)
        if events:
            return events

    # Fallback: look for a top-level events list (no leg context)
    for ev_key in ("events", "milestones"):
        for ev in (journey_obj.get(ev_key) or []):
            record = normalize_event(ev, None, None, None, row_meta)
            if record:
                events.append(record)

    return events


def process_row(row):
    meta = {
        "journey_id": row.get("journey_id"),
        "unit_of_tracking": row.get("unit_of_tracking"),
    }
    events = []

    # Primary source: journey_request
    journey_req = safe_json(row.get("journey_request"))
    if journey_req:
        events.extend(extract_events_from_journey(journey_req, meta))

    # Secondary source: triangulated_journey (if journey_request yielded nothing)
    if not events:
        tri = safe_json(row.get("triangulated_journey"))
        if tri:
            events.extend(extract_events_from_journey(tri, meta))

    # Tertiary: milestone_triangulation_result
    if not events:
        mtr = safe_json(row.get("milestone_triangulation_result"))
        if mtr:
            events.extend(extract_events_from_journey(mtr, meta))

    return events, meta


def main():
    args = parse_args()
    with open(args.file) as f:
        data = json.load(f)

    rows = unwrap_mcp_response(data)
    all_events = []
    row_meta = {}

    for row in rows:
        evs, meta = process_row(row)
        all_events.extend(evs)
        row_meta = meta  # use last row meta for top-level output

    # Apply filters
    if args.trigger:
        all_events = [e for e in all_events if e.get("eventTrigger") == args.trigger]
    if args.location:
        all_events = [e for e in all_events if e.get("eventLocationCode") == args.location]

    result = {
        "status": "OK" if all_events else "NOT_FOUND",
        "journeyId": row_meta.get("journey_id"),
        "unitOfTracking": row_meta.get("unit_of_tracking"),
        "count": len(all_events),
        "events": all_events,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
