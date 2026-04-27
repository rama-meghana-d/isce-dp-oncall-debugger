# Example: Specific Event Investigation

## Input

```
journey_id:      3f8a1c2d-4b56-7e89-abcd-1234567890ef
container:       MSKU1234567
event_trigger:   VESSEL_DEPARTURE
event_timestamp: 2024-11-15T14:00:00Z
event_location:  CNSHA
```

---

## Step 0 — Resolve journey_id

Already provided — skip SIR/DOS resolution.

---

## Step 1 — GIS Stitching State

```sql
SELECT id, journey_id, unit_of_tracking, journey_hash,
       journey_request::text,
       milestone_triangulation_result::text,
       triangulated_journey::text,
       latest_timestamp,
       journey_correlation_id
FROM shipment_journey_transaction
WHERE journey_id = '3f8a1c2d-4b56-7e89-abcd-1234567890ef'
  AND unit_of_tracking = 'MSKU1234567'
ORDER BY latest_timestamp DESC NULLS LAST LIMIT 1;
```

MCP auto-saved result to `/tmp/gis_sjt_result.json`.

```bash
SCRIPTS=".claude/skills/event-stitching-root-cause-investigator/scripts"
python3 $SCRIPTS/parse_gis_shipment_journey_transaction.py /tmp/gis_sjt_result.json \
  --trigger VESSEL_DEPARTURE \
  --location CNSHA
```

**Parsed output:**
```json
{
  "status": "OK",
  "journeyId": "3f8a1c2d-4b56-7e89-abcd-1234567890ef",
  "unitOfTracking": "MSKU1234567",
  "count": 1,
  "events": [
    {
      "eventTrigger": "VESSEL_DEPARTURE",
      "eventTiming": "ACTUAL",
      "eventTimestamp": "2024-11-15T14:00:00Z",
      "eventLocationCode": "CNSHA",
      "eventLocationFunction": "PORT",
      "mode": "SEA",
      "vesselName": "MAERSK ELBA",
      "vesselImo": "9632437",
      "voyage": "412E",
      "stitchedLegSequence": 2,
      "stitchedLegFrom": "CNSHA",
      "stitchedLegTo": "DEHAM"
    }
  ]
}
```

---

## Step 2 — DOS Transport Plan

```sql
SELECT journey_id, journey::text, updated_at
FROM shipment_journey
WHERE journey_id = '3f8a1c2d-4b56-7e89-abcd-1234567890ef'
ORDER BY updated_at DESC LIMIT 1;
```

MCP auto-saved result to `/tmp/dos_tp_result.json`.

```bash
python3 $SCRIPTS/parse_dos_transport_plan.py /tmp/dos_tp_result.json
```

**Parsed output (legs):**
```json
{
  "status": "OK",
  "legCount": 3,
  "legs": [
    {
      "legSequence": 1, "mode": "ROAD",
      "fromLocation": "CNSHA-CFS", "toLocation": "CNSHA",
      "plannedDeparture": "2024-11-14T08:00:00Z",
      "plannedArrival": "2024-11-14T12:00:00Z",
      "isSelfLoopLeg": false
    },
    {
      "legSequence": 2, "mode": "SEA",
      "fromLocation": "CNSHA", "toLocation": "DEHAM",
      "vesselName": "MAERSK ELBA", "voyage": "412E",
      "plannedDeparture": "2024-11-15T16:00:00Z",
      "plannedArrival": "2024-12-01T06:00:00Z",
      "isSelfLoopLeg": false
    },
    {
      "legSequence": 3, "mode": "ROAD",
      "fromLocation": "DEHAM", "toLocation": "DEHAM-CY",
      "plannedDeparture": "2024-12-01T10:00:00Z",
      "plannedArrival": "2024-12-01T14:00:00Z",
      "isSelfLoopLeg": false
    }
  ]
}
```

---

## Step 3 — MCE Cluster Data

```sql
SELECT id, journey_id, unit_of_tracking, unit_of_tracking_type,
       subscription_data::text,
       subscription_data_blob_url,
       storage_type,
       created_at, updated_at
FROM journey_subscription
WHERE journey_id = '3f8a1c2d-4b56-7e89-abcd-1234567890ef'
  AND unit_of_tracking = 'MSKU1234567'
ORDER BY updated_at DESC LIMIT 1;
```

Result: `storage_type = IN_MEMORY`, `subscription_data` present.

```bash
python3 $SCRIPTS/parse_mce_cluster_output.py /tmp/mce_js_result.json \
  --trigger VESSEL_DEPARTURE \
  --location CNSHA
```

**Parsed output:**
```json
{
  "status": "OK",
  "count": 1,
  "events": [
    {
      "clusterId": "cluster_7",
      "eventTrigger": "VESSEL_DEPARTURE",
      "eventTiming": "ACTUAL",
      "eventLocationCode": "CNSHA",
      "provider": "MAERSKTNT",
      "selectedLeg": 2,
      "matchedReason": "Origin location match + vessel/voyage match",
      "appliedRules": ["Global Provider Precedence: MAERSKTNT > P44_NEW"]
    }
  ]
}
```

---

## Step 4 — Compare

```bash
python3 $SCRIPTS/compare_event_to_transport_plan.py \
  --event /tmp/gis_events.json \
  --transport-plan /tmp/dos_tp.json \
  --mce-cluster /tmp/mce_cluster.json
```

**Comparison result:**
```json
{
  "assessment": "Correct",
  "confidence": "High",
  "selectedLeg": 2,
  "selectedLegScore": 38,
  "bestCandidateLeg": 2,
  "bestCandidateScore": 38,
  "bestIsSelected": true,
  "explanation": "Event (VESSEL_DEPARTURE @ CNSHA) is currently stitched to leg 2 (CNSHA → DEHAM). Supporting signals: eventLocation CNSHA matches leg origin CNSHA; Departure trigger VESSEL_DEPARTURE correctly associated with leg origin; Event timestamp within 0.8 days of leg scheduled time; Trigger VESSEL_DEPARTURE is compatible with leg mode SEA; Vessel name matches: MAERSK ELBA; Voyage matches: 412E."
}
```

---

## Final RCA Output

```
Event Stitching RCA
===================

Input:
  journeyId:       3f8a1c2d-4b56-7e89-abcd-1234567890ef
  unitOfTracking:  MSKU1234567
  eventTrigger:    VESSEL_DEPARTURE
  eventTimestamp:  2024-11-15T14:00:00Z
  eventLocation:   CNSHA

Current GIS Stitching (from journey_request):
  stitchedLeg: 2
  from:        CNSHA
  to:          DEHAM

Transport Plan Context:
  Leg 1: ROAD  | CNSHA-CFS → CNSHA | plannedDep: 2024-11-14T08:00:00Z
  Leg 2: SEA   | CNSHA → DEHAM     | MAERSK ELBA / 412E | plannedDep: 2024-11-15T16:00:00Z ← STITCHED
  Leg 3: ROAD  | DEHAM → DEHAM-CY  | plannedDep: 2024-12-01T10:00:00Z

MCE Cluster Context:
  storage_type: IN_MEMORY
  clusterId:    cluster_7
  provider:     MAERSKTNT
  selectedLeg:  2
  appliedRules: Global Provider Precedence (MAERSKTNT > P44_NEW)

Why it stitched to leg 2:
  1. Event location CNSHA matches leg 2 origin (CNSHA) — strongest signal
  2. VESSEL_DEPARTURE is a departure trigger → correctly associated with leg origin
  3. Event timestamp (2024-11-15T14:00Z) is 0.8 days before planned departure (2024-11-15T16:00Z) — within window
  4. SEA mode matches VESSEL_DEPARTURE trigger
  5. Vessel MAERSK ELBA and voyage 412E match leg 2
  6. MCE selected leg 2 via origin location + vessel/voyage match; GIS persisted the same

Alternative candidate legs:
  Leg 1 (ROAD | CNSHA-CFS → CNSHA): rejected — VESSEL_DEPARTURE incompatible with ROAD mode; CNSHA is destination not origin
  Leg 3 (ROAD | DEHAM → DEHAM-CY): rejected — location CNSHA does not match DEHAM; timestamp 16 days outside leg window

Assessment:    Correct

Likely Root Cause:  N/A — stitching is correct

Recommended Action: No action needed

Confidence:  High
```
