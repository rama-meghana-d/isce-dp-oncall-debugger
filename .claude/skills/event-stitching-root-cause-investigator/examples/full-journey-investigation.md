# Example: Full Journey Investigation (Stitching Anomaly Detected)

## Input

```
journey_id: 9a1b2c3d-5e6f-7890-abcd-ef1234567890
container:  TCKU9876543
```

No event-specific parameters → Full Journey Mode.

---

## Step 0 — Resolve journey_id

Already provided. Skip.

---

## Step 1 — GIS Stitching State (all events)

```sql
SELECT id, journey_id, unit_of_tracking, journey_hash,
       journey_request::text,
       milestone_triangulation_result::text,
       triangulated_journey::text,
       latest_timestamp,
       journey_correlation_id
FROM shipment_journey_transaction
WHERE journey_id = '9a1b2c3d-5e6f-7890-abcd-ef1234567890'
  AND unit_of_tracking = 'TCKU9876543'
ORDER BY latest_timestamp DESC NULLS LAST LIMIT 1;
```

```bash
SCRIPTS=".claude/skills/event-stitching-root-cause-investigator/scripts"
python3 $SCRIPTS/parse_gis_shipment_journey_transaction.py /tmp/gis_sjt_full.json
```

**Parsed: 6 events**

| # | Trigger | Timing | Timestamp | Location | StitchedLeg | From | To |
|---|---|---|---|---|---|---|---|
| 1 | VESSEL_DEPARTURE | ACTUAL | 2024-10-10T08:00Z | CNNGB | 1 | CNNGB | SGSIN |
| 2 | VESSEL_ARRIVAL | ACTUAL | 2024-10-18T06:00Z | SGSIN | 1 | CNNGB | SGSIN |
| 3 | VESSEL_DEPARTURE | ESTIMATED | 2024-10-20T12:00Z | SGSIN | 2 | SGSIN | NLRTM |
| 4 | VESSEL_ARRIVAL | ACTUAL | 2024-11-05T09:00Z | NLRTM | 2 | SGSIN | NLRTM |
| 5 | ROAD_DEPARTURE | ACTUAL | 2024-11-05T14:00Z | NLRTM | **2** | SGSIN | NLRTM |← anomaly
| 6 | ROAD_ARRIVAL | ACTUAL | 2024-11-06T10:00Z | NLRTM-CY | **2** | SGSIN | NLRTM |← anomaly

Events 5 and 6 are stitched to leg 2 (SEA SGSIN→NLRTM) but are ROAD triggers at NLRTM — suspicious.

---

## Step 2 — DOS Transport Plan

```bash
python3 $SCRIPTS/parse_dos_transport_plan.py /tmp/dos_tp_full.json
```

**Parsed: 3 legs**

| Leg | Mode | From | To | Vessel | Voyage | PlannedDep | PlannedArr | SelfLoop |
|---|---|---|---|---|---|---|---|---|
| 1 | SEA | CNNGB | SGSIN | EVER GLORY | 0123N | 2024-10-10T08:00Z | 2024-10-18T06:00Z | false |
| 2 | SEA | SGSIN | NLRTM | MSC GAIA | 456W | 2024-10-20T12:00Z | 2024-11-05T09:00Z | false |
| 3 | ROAD | NLRTM | NLRTM-CY | — | — | 2024-11-05T13:00Z | 2024-11-06T10:00Z | false |

Leg 3 exists (ROAD NLRTM → NLRTM-CY) and events 5 + 6 should have stitched there.

---

## Step 3 — MCE Cluster Data

```sql
SELECT id, journey_id, unit_of_tracking, unit_of_tracking_type,
       subscription_data::text,
       subscription_data_blob_url,
       storage_type,
       created_at, updated_at
FROM journey_subscription
WHERE journey_id = '9a1b2c3d-5e6f-7890-abcd-ef1234567890'
  AND unit_of_tracking = 'TCKU9876543'
ORDER BY updated_at DESC LIMIT 1;
```

Result: `storage_type = IN_MEMORY`.

```bash
python3 $SCRIPTS/parse_mce_cluster_output.py /tmp/mce_js_full.json
```

**MCE cluster for events 5 + 6:**
```json
{
  "clusterId": "cluster_12",
  "eventTrigger": "ROAD_DEPARTURE",
  "eventLocationCode": "NLRTM",
  "selectedLeg": 2,
  "matchedReason": "Destination location match (NLRTM == leg 2 toLocation)",
  "appliedRules": ["Destination location match"]
}
```

MCE also selected leg 2 — destination match (NLRTM is the arrival port of leg 2). However, ROAD_DEPARTURE is a departure trigger and should match the **origin** of a ROAD leg, not the destination of a SEA leg. Leg 3 (ROAD, NLRTM → NLRTM-CY) is the correct match.

---

## Step 4 — Compare

```bash
python3 $SCRIPTS/compare_event_to_transport_plan.py \
  --event /tmp/gis_events_full.json \
  --transport-plan /tmp/dos_tp_full.json \
  --mce-cluster /tmp/mce_cluster_full.json
```

**Comparison result for event 5 (ROAD_DEPARTURE @ NLRTM):**
```json
{
  "selectedLeg": 2,
  "selectedLegScore": 5,
  "bestCandidateLeg": 3,
  "bestCandidateScore": 24,
  "bestIsSelected": false,
  "assessment": "Suspicious",
  "confidence": "Low",
  "explanation": "Event (ROAD_DEPARTURE @ NLRTM) is stitched to leg 2 (SGSIN → NLRTM). Concerns: Departure trigger ROAD_DEPARTURE stitched to leg destination (wrong side); Trigger ROAD_DEPARTURE is NOT compatible with leg mode SEA. Higher-scoring leg 3 (NLRTM → NLRTM-CY) scored 24 vs selected leg score 5. MCE selected leg 2 via destination match — but ROAD_DEPARTURE should match leg origin, not destination."
}
```

---

## Final RCA Output

```
Journey Stitching Summary
=========================

journeyId:       9a1b2c3d-5e6f-7890-abcd-ef1234567890
unitOfTracking:  TCKU9876543

Transport Plan (from DOS shipment_journey):
  Leg | Mode | From      | To        | Vessel      | Voyage | PlannedDep          | PlannedArr
  ----+------+-----------+-----------+-------------+--------+---------------------+--------------------
   1  | SEA  | CNNGB     | SGSIN     | EVER GLORY  | 0123N  | 2024-10-10T08:00Z   | 2024-10-18T06:00Z
   2  | SEA  | SGSIN     | NLRTM     | MSC GAIA    | 456W   | 2024-10-20T12:00Z   | 2024-11-05T09:00Z
   3  | ROAD | NLRTM     | NLRTM-CY  | —           | —      | 2024-11-05T13:00Z   | 2024-11-06T10:00Z

Event Stitching Table (from GIS journey_request):
  Trigger           | Timing    | Timestamp          | Location  | StitchedLeg | Reason                                | Assessment
  ------------------+-----------+--------------------+-----------+-------------+---------------------------------------+-----------
  VESSEL_DEPARTURE  | ACTUAL    | 2024-10-10T08:00Z  | CNNGB     | 1           | Origin match + vessel/voyage          | Correct
  VESSEL_ARRIVAL    | ACTUAL    | 2024-10-18T06:00Z  | SGSIN     | 1           | Destination match + vessel/voyage     | Correct
  VESSEL_DEPARTURE  | ESTIMATED | 2024-10-20T12:00Z  | SGSIN     | 2           | Origin match + vessel/voyage          | Correct
  VESSEL_ARRIVAL    | ACTUAL    | 2024-11-05T09:00Z  | NLRTM     | 2           | Destination match + vessel/voyage     | Correct
  ROAD_DEPARTURE    | ACTUAL    | 2024-11-05T14:00Z  | NLRTM     | 2 ← WRONG   | Dest match leg 2 (wrong side/mode)    | Suspicious
  ROAD_ARRIVAL      | ACTUAL    | 2024-11-06T10:00Z  | NLRTM-CY  | 2 ← WRONG   | No match in leg 2 (wrong location)    | Suspicious

Suspicious Events:
  1. ROAD_DEPARTURE @ NLRTM → stitched to leg 2 (SEA, SGSIN→NLRTM)
     - ROAD_DEPARTURE is a departure trigger; should match leg ORIGIN, not destination
     - Mode mismatch: ROAD trigger on SEA leg
     - Correct candidate: leg 3 (ROAD, NLRTM→NLRTM-CY) — origin location NLRTM matches; mode ROAD matches; timestamp within 1h of plannedDeparture
     - MCE selected leg 2 via destination location match — MCE stitching logic applied wrong trigger-side rule

  2. ROAD_ARRIVAL @ NLRTM-CY → stitched to leg 2 (SEA, SGSIN→NLRTM)
     - Location NLRTM-CY does not match leg 2 origin (SGSIN) or destination (NLRTM)
     - Correct candidate: leg 3 (ROAD, NLRTM→NLRTM-CY) — destination NLRTM-CY matches; mode ROAD matches

Overall Root Cause:
  MCE `journey_subscription` cluster_12 applied a destination location match for ROAD_DEPARTURE @ NLRTM,
  associating it with leg 2 (SEA) because NLRTM is the arrival port of that leg. The MCE stitching logic
  did not apply the trigger-side check (departure trigger → should match ORIGIN of a leg, not destination).
  Leg 3 (ROAD, NLRTM → NLRTM-CY) is the correct leg for both road events.

  Both GIS `journey_request` and MCE `subscription_data` agree on the wrong leg —
  this is NOT a GIS persistence issue; the error originates in MCE clustering/stitching logic.

Recommended Action:
  1. Raise DI Engineering ticket: MCE stitching should apply trigger-side rule — departure triggers
     must match leg ORIGIN, not leg destination. Reference: journey_id 9a1b2c3d, cluster_12,
     events ROAD_DEPARTURE + ROAD_ARRIVAL @ NLRTM.
  2. After MCE fix is deployed: replay journey to re-stitch events 5 and 6 to leg 3.
  3. No provider data quality issue — the events themselves are correct.
```
