#!/usr/bin/env python3
"""
Parse DOS shipment_journey transport plan query result.

Reads the MCP query output (journey JSONB column from shipment_journey table) and
extracts normalized transport plan legs for stitching comparison.

Usage:
  python3 parse_dos_transport_plan.py <result_file.json>

Output: compact JSON with journeyId, unitOfTracking, and list of normalized legs.
"""
import json
import sys


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
        or loc.get("facilityCode")
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


def extract_timestamp(ts_obj, timing):
    """Extract a specific timing (PLANNED, ESTIMATED, ACTUAL) from event or timestamp block."""
    if not isinstance(ts_obj, dict):
        return None
    # Direct timing field
    direct = ts_obj.get(timing.lower()) or ts_obj.get(timing)
    if direct and isinstance(direct, str):
        return direct
    # Nested events list
    for ev in (ts_obj.get("events") or []):
        if isinstance(ev, dict) and ev.get("eventTiming") == timing:
            return ev.get("timestamp")
    return None


def extract_vessel_info(leg):
    """Extract vessel name, IMO, and voyage from a leg object."""
    vessel_name, vessel_imo, voyage = None, None, None

    # Direct fields
    vessel = leg.get("vessel") or {}
    if isinstance(vessel, dict):
        vessel_name = vessel.get("vesselName") or vessel.get("name")
        vessel_imo = vessel.get("vesselIMONumber") or vessel.get("imo")
    elif isinstance(vessel, str):
        vessel_name = vessel

    voyage = leg.get("voyageNumber") or leg.get("voyage") or leg.get("carrierVoyageNumber")

    # From transportActivity
    ta = leg.get("transportActivity") or {}
    if isinstance(ta, dict):
        ta_vessel = ta.get("vessel") or {}
        if isinstance(ta_vessel, dict):
            vessel_name = vessel_name or ta_vessel.get("vesselName") or ta_vessel.get("name")
            vessel_imo = vessel_imo or ta_vessel.get("vesselIMONumber") or ta_vessel.get("imo")
        voyage = voyage or ta.get("carrierVoyageNumber") or ta.get("voyageNumber")

    # From references
    for ref in (leg.get("references") or []):
        if not isinstance(ref, dict):
            continue
        ref_type = ref.get("referenceTypeEnum") or ref.get("type") or ""
        ref_val = ref.get("reference") or ref.get("value") or ""
        if "VESSEL_NAME" in ref_type and not vessel_name:
            vessel_name = ref_val
        elif "VESSEL_IMO" in ref_type and not vessel_imo:
            vessel_imo = ref_val
        elif "VOYAGE" in ref_type and not voyage:
            voyage = ref_val

    return vessel_name, vessel_imo, voyage


def normalize_leg(leg, idx):
    leg_seq = leg.get("legSequence") or leg.get("sequence") or leg.get("leg_sequence") or (idx + 1)
    mode = (
        leg.get("transportMode")
        or leg.get("mode")
        or leg.get("transport_mode")
        or leg.get("modeOfTransport")
    )

    from_loc_obj = leg.get("fromLocation") or leg.get("from") or leg.get("departureLocation") or {}
    to_loc_obj = leg.get("toLocation") or leg.get("to") or leg.get("arrivalLocation") or {}
    from_loc = extract_location_code(from_loc_obj)
    to_loc = extract_location_code(to_loc_obj)
    from_func = extract_location_function(from_loc_obj)
    to_func = extract_location_function(to_loc_obj)

    # Timestamps — look in departure/arrival blocks or top-level
    dep_block = leg.get("departure") or leg.get("plannedDeparture") or {}
    arr_block = leg.get("arrival") or leg.get("plannedArrival") or {}

    planned_dep = extract_timestamp(dep_block, "PLANNED") or leg.get("plannedDeparture")
    estimated_dep = extract_timestamp(dep_block, "ESTIMATED") or leg.get("estimatedDeparture")
    actual_dep = extract_timestamp(dep_block, "ACTUAL") or leg.get("actualDeparture")
    planned_arr = extract_timestamp(arr_block, "PLANNED") or leg.get("plannedArrival")
    estimated_arr = extract_timestamp(arr_block, "ESTIMATED") or leg.get("estimatedArrival")
    actual_arr = extract_timestamp(arr_block, "ACTUAL") or leg.get("actualArrival")

    # Simple string timestamps at top level
    for field, var in [
        ("plannedDeparture", planned_dep), ("estimatedDeparture", estimated_dep),
        ("actualDeparture", actual_dep), ("plannedArrival", planned_arr),
        ("estimatedArrival", estimated_arr), ("actualArrival", actual_arr),
    ]:
        if var is None and isinstance(leg.get(field), str):
            locals()[field.replace("planned", "planned_").replace("estimated", "estimated_").replace("actual", "actual_")] = leg[field]

    vessel_name, vessel_imo, voyage = extract_vessel_info(leg)

    return {
        "legSequence": leg_seq,
        "mode": mode,
        "fromLocation": from_loc,
        "toLocation": to_loc,
        "fromLocationFunction": from_func,
        "toLocationFunction": to_func,
        "plannedDeparture": planned_dep,
        "estimatedDeparture": estimated_dep,
        "actualDeparture": actual_dep,
        "plannedArrival": planned_arr,
        "estimatedArrival": estimated_arr,
        "actualArrival": actual_arr,
        "vesselName": vessel_name,
        "vesselImo": vessel_imo,
        "voyage": voyage,
        "isSelfLoopLeg": (from_loc is not None and from_loc == to_loc),
    }


def extract_legs(journey_obj):
    if not isinstance(journey_obj, dict):
        return []
    for key in ("shipmentJourneyLegs", "legs", "transportLegs", "transportPlan", "routeLegs"):
        legs_raw = journey_obj.get(key)
        if isinstance(legs_raw, list) and legs_raw:
            return [normalize_leg(leg, i) for i, leg in enumerate(legs_raw) if isinstance(leg, dict)]
    return []


def main():
    if len(sys.argv) < 2:
        print("Usage: parse_dos_transport_plan.py <result_file.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        data = json.load(f)

    rows = unwrap_mcp_response(data)
    all_legs = []
    journey_id = None

    for row in rows:
        journey_id = journey_id or row.get("journey_id")
        journey_obj = safe_json(row.get("journey"))
        if journey_obj:
            all_legs.extend(extract_legs(journey_obj))

    # Sort by leg sequence
    all_legs.sort(key=lambda l: (l.get("legSequence") or 0))

    result = {
        "status": "OK" if all_legs else "NOT_FOUND",
        "journeyId": journey_id,
        "legCount": len(all_legs),
        "legs": all_legs,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
