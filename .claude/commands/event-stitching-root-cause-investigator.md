# Event Stitching Root Cause Investigator

Investigates why one or more events were stitched to a particular transport plan leg. Traces from the final GIS stitched state back through the DOS transport plan and MCE cluster data to identify whether the stitching is correct, suspicious, or wrong — and which layer introduced the mismatch.

## When to use this skill

| Scenario | Use |
|---|---|
| An event appears on the wrong leg (wrong origin/destination, wrong vessel, wrong mode) | **this skill** |
| You want to understand ALL stitching decisions for a container journey | **this skill** (omit event params) |
| You want to audit event timestamps vs platform ingestion time | `/milestone-timestamp-audit` |
| The wrong event trigger type was received (e.g. VESSEL_ARRIVAL at inland) | `/incorrect-milestone-tracer` |

## Input

```
journey_id:       <JOURNEY_ID>
container:        <CONTAINER>          e.g. MSKU1234567
event_trigger:    <TRIGGER>            (optional) e.g. VESSEL_DEPARTURE
event_timestamp:  <TIMESTAMP>          (optional) e.g. 2024-11-15T14:00:00Z
event_location:   <LOCODE>             (optional) e.g. CNSHA
```

$ARGUMENTS

---

## Mode Selection

**Specific event mode** — triggered when any of `event_trigger`, `event_timestamp`, or `event_location` is provided. Explains why that one event was stitched to its current leg.

**Full journey mode** — triggered when only `journey_id` + `container` are provided. Explains stitching for every event in the journey and flags anomalies.

---

## GIS Table Schema (from Liquibase — no discovery query needed)

**`shipment_journey_transaction`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `journey_id` | UUID | NOT NULL |
| `unit_of_tracking` | VARCHAR(255) | NOT NULL |
| `journey_hash` | VARCHAR(255) | NOT NULL |
| `journey_request` | JSONB | NOT NULL — request sent to GDA; contains events stitched to legs |
| `milestone_triangulation_result` | JSONB | nullable — triangulation output |
| `latest_timestamp` | TIMESTAMP | nullable |
| `journey_correlation_id` | UUID | NOT NULL |
| `triangulated_journey` | JSONB | nullable — final triangulated journey |

Unique index: `(journey_id, unit_of_tracking, journey_hash)`.  Always filter by **both** `journey_id` AND `unit_of_tracking`.

**`audit_trail`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `journey_id` | UUID | NOT NULL |
| `unit_of_tracking` | VARCHAR(255) | NOT NULL |
| `event_id` | VARCHAR(255) | NOT NULL |
| `change_log` | JSONB | NOT NULL |
| `created_on` | TIMESTAMP | nullable |

---

## MCE Table Schema (from Liquibase — no discovery query needed)

**`journey_subscription`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `journey_id` | UUID | NOT NULL |
| `unit_of_tracking` | VARCHAR(255) | NOT NULL (unique with journey_id) |
| `unit_of_tracking_type` | VARCHAR(100) | nullable |
| `subscription_data` | JSON | nullable — null when `storage_type = BLOB` |
| `subscription_data_blob_url` | VARCHAR(500) | nullable — external blob URL |
| `storage_type` | VARCHAR(20) | `IN_MEMORY` or `BLOB` |
| `created_at` | TIMESTAMP | nullable |
| `updated_at` | TIMESTAMP | nullable |

---

## Helper Script Paths

```
SCRIPTS=".claude/skills/event-stitching-root-cause-investigator/scripts"
```

---

## Step 0 — Resolve journey_id (skip if already provided)

Load `mcp__dos-db__query` via ToolSearch.

```sql
SELECT journey_id, unit_of_tracking, status, journey_type, updated_at
FROM intelligent_journey
WHERE unit_of_tracking = '<CONTAINER>'
ORDER BY updated_at DESC LIMIT 5;
```

`unit_of_tracking` is a plain text column — use `=`, no casting or LIKE.

Pick the most recent row. Record `journey_id`, `status`, and `journey_type`. If `status = TRACKING_STOPPED`, carry that as context in the final verdict.

---

## Step 1 — Fetch GIS Stitching State

Load `mcp__gis-db__query` via ToolSearch.

```sql
SELECT id, journey_id, unit_of_tracking, journey_hash,
       journey_request::text,
       milestone_triangulation_result::text,
       triangulated_journey::text,
       latest_timestamp,
       journey_correlation_id
FROM shipment_journey_transaction
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY latest_timestamp DESC NULLS LAST LIMIT 1;
```

The MCP tool auto-saves large responses to a file. Parse with:

```bash
python3 $SCRIPTS/parse_gis_shipment_journey_transaction.py <SAVED_FILE> \
  [--trigger <EVENT_TRIGGER>] \
  [--location <LOCATION_CODE>]
```

**Output fields per event:** `eventTrigger`, `eventTiming`, `eventTimestamp`, `eventLocationCode`, `eventLocationFunction`, `mode`, `vesselName`, `vesselImo`, `voyage`, `stitchedLegSequence`, `stitchedLegFrom`, `stitchedLegTo`.

**No rows** → GIS has never processed this journey+container pair. Check `audit_trail` for rejections:

```sql
SELECT id, journey_id, unit_of_tracking, event_id, change_log::text, created_on
FROM audit_trail
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY created_on DESC LIMIT 10;
```

---

## Step 2 — Fetch DOS Transport Plan

Load `mcp__dos-db__query` via ToolSearch (already loaded in Step 0).

```sql
SELECT journey_id, journey::text, updated_at
FROM shipment_journey
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY updated_at DESC LIMIT 1;
```

Parse with:

```bash
python3 $SCRIPTS/parse_dos_transport_plan.py <SAVED_FILE>
```

**Output fields per leg:** `legSequence`, `mode`, `fromLocation`, `toLocation`, `fromLocationFunction`, `toLocationFunction`, `plannedDeparture`, `estimatedDeparture`, `actualDeparture`, `plannedArrival`, `estimatedArrival`, `actualArrival`, `vesselName`, `vesselImo`, `voyage`, `isSelfLoopLeg`.

---

## Step 3 — Fetch MCE Cluster Data

Load `mcp__mce-db__query` via ToolSearch.

```sql
SELECT id, journey_id, unit_of_tracking, unit_of_tracking_type,
       subscription_data::text,
       subscription_data_blob_url,
       storage_type,
       created_at, updated_at
FROM journey_subscription
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY updated_at DESC LIMIT 1;
```

**If `storage_type = BLOB` and `subscription_data IS NULL`:** The subscription data is in external blob storage at `subscription_data_blob_url`. Report the URL and note that in-DB parsing is not possible — the MCE cluster context will be unavailable; proceed with GIS + DOS evidence only.

**If `subscription_data` is present:** Parse with:

```bash
python3 $SCRIPTS/parse_mce_cluster_output.py <SAVED_FILE> \
  [--trigger <EVENT_TRIGGER>] \
  [--location <LOCATION_CODE>]
```

**Output fields per event:** `clusterId`, `eventTrigger`, `eventTiming`, `eventLocationCode`, `provider`, `matchedReason`, `selectedLeg`, `candidateLegs`, `appliedRules`.

**No row at all** → MCE has no subscription for this journey+container. Check whether the IJ journey was ever sent to GIS.

---

## Step 4 — Compare Event Against Transport Plan Legs

Run the comparison script with parsed outputs from Steps 1–3:

```bash
python3 $SCRIPTS/compare_event_to_transport_plan.py \
  --event <GIS_EVENTS_JSON_FILE> \
  --transport-plan <DOS_TP_JSON_FILE> \
  [--mce-cluster <MCE_CLUSTER_JSON_FILE>]
```

The script scores each candidate leg against every event and outputs:

- `selectedLeg` — leg the event is currently stitched to in GIS
- `candidateLegScores` — score breakdown for all legs
- `bestCandidateLeg` — highest-scoring leg (may differ from selectedLeg)
- `explanation` — natural language reason for the selected leg
- `warnings` — flags: self-loop, wrong mode, timestamp far outside window, adjacent leg candidate, vessel mismatch
- `confidence` — High / Medium / Low

---

## Step 5 — Stitching Decision Logic

For each event, Claude evaluates these signals in order:

**1. Location match (strongest signal)**
- Departure triggers (`VESSEL_DEPARTURE`, `LOAD_ON_VESSEL`, `GATE_OUT_EMPTY`, `ROAD_DEPARTURE`, `RAIL_DEPARTURE`, `BARGE_DEPARTURE`) → event location should match `leg.fromLocation`
- Arrival triggers (`VESSEL_ARRIVAL`, `UNLOADED_FROM_VESSEL`, `GATE_IN`, `ROAD_ARRIVAL`, `RAIL_ARRIVAL`, `BARGE_ARRIVAL`) → event location should match `leg.toLocation`
- Generic triggers (`STUFFED`, `STRIPPED`, `WEIGHED`, `INSPECTED`, `CUSTOMS_RELEASE`) → match either side

**2. Mode compatibility**
- `VESSEL_*` → SEA / OCEAN legs; `RAIL_*` → RAIL; `ROAD_*` / `TRUCK_*` → ROAD; `BARGE_*` → BARGE / INLAND_WATERWAY

**3. Timestamp proximity**
- Event timestamp should fall within the leg's planned/estimated window ± 2 days
- Flag as anomaly if >7 days outside the leg window

**4. Vessel / voyage match**
- If the event carries vessel name or voyage, it must match the leg's vessel/voyage
- Mismatch is a strong stitching anomaly signal

**5. Self-loop leg risk**
- If `leg.fromLocation == leg.toLocation`, the leg is a self-loop
- Stitching a non-terminal event to a self-loop leg requires explicit justification (e.g. CFS stuffing)

**6. Adjacent leg pull**
- If the event's location matches both the destination of leg N and origin of leg N+1, evaluate both
- MCE cluster grouping may have pulled the event to the earlier leg

**7. MCE cluster influence**
- Review `appliedRules` from `journey_subscription.subscription_data`
- If provider precedence rules caused a higher-priority provider's event to displace a lower-priority event, note it
- If the cluster groups events from multiple providers at the same location, the winning `provider` may have contributed wrong vessel/voyage

---

## Step 6 — Identify the Mismatch Layer

Apply this decision tree using GIS (`journey_request`) vs MCE (`subscription_data`) vs DOS (`shipment_journey`):

| GIS stitchedLeg | MCE selectedLeg | Transport Plan leg | Verdict |
|---|---|---|---|
| Correct | Correct | Correct | No platform fault — investigate consumer |
| Correct | Correct | **Wrong** | Transport plan issue → `/transport-plan-investigation` |
| **Wrong** | Correct | Correct | GIS persistence/sync issue — raise DI Engineering ticket |
| **Wrong** | **Wrong** | Correct | MCE clustering/stitching logic issue — check `subscription_data` |
| **Wrong** | **Wrong** | **Wrong** | Source event or transport plan issue — check DUST → MP → VRDAN |
| GIS has no row | — | — | GIS never processed; check `audit_trail` for rejections |
| — | BLOB storage | — | MCE data in blob; can only use GIS + DOS evidence |

---

## Step 7 — RCA Output

### Specific Event Mode

```
Event Stitching RCA
===================

Input:
  journeyId:       <JOURNEY_ID>
  unitOfTracking:  <CONTAINER>
  eventTrigger:    <TRIGGER>
  eventTimestamp:  <TIMESTAMP>
  eventLocation:   <LOCODE>

Current GIS Stitching (from journey_request):
  stitchedLeg:     <LEG_SEQUENCE>
  from:            <FROM_LOCATION>
  to:              <TO_LOCATION>

Transport Plan Context (from shipment_journey):
  [table: legSeq | mode | from | to | vessel | voyage | plannedDep | plannedArr]

MCE Cluster Context (from journey_subscription):
  storage_type:    IN_MEMORY / BLOB
  clusterId:       <CLUSTER_ID>
  provider:        <DATA_PROVIDER>
  appliedRules:    [summary]
  selectedLeg:     <MCE_SELECTED_LEG>

Why it stitched to this leg:
  1. <reason — location match / trigger-side compatibility>
  2. <reason — timestamp within window / outside window>
  3. <reason — vessel/voyage match or mismatch>
  4. <reason — MCE cluster grouping influence>

Alternative candidate legs:
  leg <N>: <from> → <to>
    rejected because: <location mismatch / wrong mode / timestamp out of window>

Assessment:        Correct / Suspicious / Incorrect

Likely Root Cause:
  Source event data issue / transport plan issue / MCE clustering issue /
  GIS stitching logic issue / GIS persistence issue

Recommended Action:
  No action needed
  Raise provider data quality ticket
  Fix transport plan → /transport-plan-investigation
  Investigate MCE subscription_data clustering
  Investigate GIS persistence (wrong leg written to journey_request)
  Replay journey

Confidence:  High / Medium / Low
```

### Full Journey Mode

```
Journey Stitching Summary
=========================

journeyId:       <JOURNEY_ID>
unitOfTracking:  <CONTAINER>

Transport Plan (from DOS shipment_journey):
  [table: legSeq | mode | from | to | vessel | voyage | plannedDep | plannedArr]

Event Stitching Table (from GIS journey_request):
  [table: trigger | timing | timestamp | location | stitchedLeg | reason | assessment]

MCE Storage:  IN_MEMORY (parsed) / BLOB (unavailable — see blob_url)

Suspicious Events:
  [list with reason: self-loop mismatch / wrong mode / timestamp far outside window /
   adjacent leg candidate / vessel mismatch / cluster pull]

Overall Root Cause:
  [Pattern summary across all events]

Recommended Action:
  [Prioritized action plan]
```

---

## Verdict Reference

| Observation | Culprit | Action |
|---|---|---|
| No GIS row for journey+container | Event never reached GIS | Check `audit_trail WHERE unit_of_tracking = '<CONTAINER>'` for REJECTED |
| GIS `journey_request` has wrong leg; MCE had correct leg | GIS persistence bug | Raise DI Engineering ticket |
| MCE `subscription_data` has wrong leg; DOS TP and source event are correct | MCE stitching/clustering bug | Raise DI Engineering ticket with `appliedRules` |
| MCE selected wrong leg because TP has wrong/missing leg | Transport plan issue | `/transport-plan-investigation` |
| MCE selected wrong leg because source event has wrong location/trigger | Source data issue | Check DUST → MP → VRDAN for provider payload |
| Self-loop leg stitching with no justification | MCE logic gap or TP issue | Verify whether self-loop is intentional |
| MCE data in BLOB storage | Cannot parse in-session | Note blob URL; rely on GIS + DOS evidence only |
