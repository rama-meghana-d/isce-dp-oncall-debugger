# Event Stitching Root Cause Investigator

Investigates why one or more events were stitched to a particular transport plan leg. Traces from the MCE cluster data through the DOS stitching algorithm to explain exactly which priority rule attached a cluster to a leg — and whether the result in GIS matches.

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

**Full journey mode** — triggered when only `journey_id` + `container` are provided. Explains stitching for every cluster in the journey and flags anomalies.

---

## GIS Table Schema

**`shipment_journey_transaction`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `journey_id` | UUID | NOT NULL |
| `unit_of_tracking` | VARCHAR(255) | NOT NULL |
| `journey_hash` | VARCHAR(255) | NOT NULL |
| `journey_request` | JSONB | NOT NULL — CorrelatedShipmentJourney sent to GDA; contains events stitched to legs |
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

## MCE Table Schema

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

**`subscription_data` JSON structure relevant to stitching:**
```
{
  "subscriptionData": [                      // alias: SubscriptionEvents, subscriptionEvents
    {
      "subscriptionId": "...",
      "dataProviderData": [                  // alias: DataProviderEvents, dataProviderEvents
        {
          "dataProviderName": "P44_NEW",
          "events": [                        // alias: Events
            {
              "eventTrigger": "VESSEL_DEPARTURE",
              "eventTiming": "ACTUAL",
              "timestamp": "2024-11-15T14:00:00Z",
              "locations": [ { "location": { "unLocCode": "CNSHA" }, ... } ],
              "cluster": {                   // alias: Cluster
                "clusterIdentifier": 42,
                "clusterTimestamp": "2024-11-15T14:00:00Z",
                "clusterLocations": [ { "location": { "unLocCode": "CNSHA" }, ... } ]
              }
            }
          ]
        }
      ]
    }
  ]
}
```

**Stitching unit is the cluster, not the individual event.** DOS groups all events sharing the same `clusterIdentifier` into one `JourneyCluster`, then stitches the cluster to a leg. All events in the same cluster land on the same leg.

---

## Helper Script Paths

```
SCRIPTS=".claude/skills/event-stitching-root-cause-investigator/scripts"
```

---

## Step 0 — Resolve journey_id (skip if already provided)

Load `mcp__isce-di-db__query` via ToolSearch.

```sql
-- db: dos
SELECT journey_id, unit_of_tracking, status, journey_type, updated_at
FROM intelligent_journey
WHERE unit_of_tracking = '<CONTAINER>'
ORDER BY updated_at DESC LIMIT 5;
```

Pick the most recent row. Record `journey_id`, `status`, and `journey_type`. If `status = TRACKING_STOPPED`, carry that as context in the final verdict.

---

## Step 1 — Fetch GIS Stitching State

Load `mcp__isce-di-db__query` via ToolSearch.

```sql
-- db: gis
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
-- db: gis
SELECT id, journey_id, unit_of_tracking, event_id, change_log::text, created_on
FROM audit_trail
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY created_on DESC LIMIT 10;
```

---

## Step 2 — Fetch DOS Transport Plan

Load `mcp__isce-di-db__query` via ToolSearch (already loaded in Step 0).

```sql
-- db: dos
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

Load `mcp__isce-di-db__query` via ToolSearch.

```sql
-- db: mce
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

**If `storage_type = BLOB` and `subscription_data IS NULL`:** Report the URL and note that blob storage is not parseable in-session — proceed with GIS + DOS evidence only.

**If `subscription_data` is present:** Parse with:

```bash
python3 $SCRIPTS/parse_mce_cluster_output.py <SAVED_FILE> \
  [--trigger <EVENT_TRIGGER>] \
  [--location <LOCATION_CODE>]
```

**Output fields per cluster** (clusters are the stitching unit in DOS):

| Field | Description |
|---|---|
| `clusterIdentifier` | Integer cluster ID |
| `clusterTimestamp` | Representative timestamp for the cluster (used for sort/sequence assignment) |
| `clusterLocationCode` | UNLOC code from `clusterLocations[0]` |
| `clusterSequenceNumber` | Sequence assigned after sorting all clusters by `clusterTimestamp` |
| `eventTrigger` | Trigger from the first event in the cluster (determines stitching side) |
| `providers` | List of data providers contributing events to this cluster |
| `events` | All events: trigger, timing, timestamp, locationCode, provider |

**No row at all** → MCE has no subscription for this journey+container. Check whether DOS ever received a `JourneySubscription`.

---

## Step 4 — Simulate DOS Stitching and Compare

Run the comparison script with parsed outputs from Steps 1–3:

```bash
python3 $SCRIPTS/compare_event_to_transport_plan.py \
  --clusters <MCE_CLUSTERS_JSON_FILE> \
  --transport-plan <DOS_TP_JSON_FILE> \
  [--gis-events <GIS_EVENTS_JSON_FILE>]
```

The script simulates the DOS `ClusteredShipmentJourneyUtility` priority algorithm on each cluster and outputs:

- `clusterIdentifier` — cluster ID
- `clusterTimestamp` — cluster representative timestamp
- `clusterLocationCode` — cluster location (UNLOC)
- `clusterSequenceNumber` — sort order by timestamp
- `eventTrigger` — representative trigger for the cluster
- `simulatedLeg` — leg the DOS algorithm would assign this cluster to
- `stitchingPhase` — which priority rule matched (P1–P7, see Step 5)
- `phaseReason` — natural language explanation of why this phase fired
- `gisLeg` — leg the event actually appears on in GIS `journey_request` (if `--gis-events` provided)
- `discrepancy` — `true` if `simulatedLeg ≠ gisLeg`
- `warnings` — flags: self-loop leg, vessel cluster on non-ocean leg, no location, orphan

---

## Step 5 — DOS Stitching Priority Algorithm

DOS runs `ClusteredShipmentJourneyUtility.createCorrelatedShipmentJourney()` with the MCE `JourneySubscription` + the DOS `ShipmentJourney`. The algorithm:

1. **Groups events by `clusterIdentifier`** into `JourneyCluster` objects.
2. **Sorts clusters by `clusterTimestamp`** (then by `clusterIdentifier` as tiebreaker) and assigns a `clusterSequenceNumber` (1-based).
3. **Associates clusters to legs** using the strict priority order below.

---

### Priority 1 — Empty Container Hard-Wired Placement
- `EMPTY_CONTAINER_DISPATCHED` → **first leg**, ignoring location entirely
- `EMPTY_CONTAINER_RETURNED` → **last leg**, ignoring location entirely

### Priority 2 — Terminal Milestones Without Location → Last Ocean Leg
If trigger is one of **`CARRIER_RELEASE`, `IMPORT_CUSTOMS_CLEARED`, `PICKUP_APPOINTMENT`**  
AND `clusterLocations` is null/empty or `unLocCode` is blank:
→ **last ocean leg** (or last leg if no ocean leg exists)

### Priority 3 — Non-Duplicate Location + Matching Trigger Side
First, DOS identifies **duplicate leg-boundary locations** — any UNLOC that appears as the start or end of more than one leg. Clusters whose location is NOT a duplicate use this priority.

**START triggers** (when `enable_gate_in_gate_out_reversal` flag is **disabled** — the default):
`GATE_IN`, `EMPTY_CONTAINER_DISPATCHED`, `ARRIVED_AT_CUSTOMER_LOCATION`, `RECEIVED_AT_AIRPORT`,
`LOADED_ON_VESSEL`, `LOADED_ON_RAIL`, `LOADED_ON_BARGE`, `VESSEL_DEPARTURE`, `TRUCK_DEPARTURE`,
`RAIL_DEPARTURE_FROM_ORIGIN_INTERMODAL_RAMP`, `BARGE_DEPARTURE`, `FLIGHT_DEPARTURE`,
`BOOKED_ON_FLIGHT`, `MANIFEST_COMPLETED_AT_AIRPORT`, `RECEIVED_FROM_SHIPPER`,
`EXPORT_CUSTOMS_CLEARED`, `STUFFING`, `DEPARTURE`, `LOADED`

**END triggers** (flag disabled):
`VESSEL_ARRIVAL`, `TRUCK_ARRIVAL`, `RAIL_ARRIVAL_AT_DESTINATION_INTERMODAL_RAMP`,
`BARGE_ARRIVAL`, `FLIGHT_ARRIVAL`, `UNLOADED_FROM_VESSEL`, `UNLOADED_FROM_A_RAIL_CAR`,
`UNLOADED_FROM_BARGE`, `CARRIER_RELEASE`, `GATE_OUT`, `EXIT_FACILITY`, `GATE_OUT_FROM_AIRPORT`,
`DELIVERY`, `EMPTY_CONTAINER_RETURNED`, `DELIVERY_CONFIRMED`,
`IN_TRANSIT_CUSTOMS_CLEARANCE_OPENED`, `IN_TRANSIT_CUSTOMS_CLEARANCE_CLOSED`,
`IN_TRANSIT_CUSTOMS_CLEARANCE_EXPIRY`, `IMPORT_CUSTOMS_ON_HOLD`, `IMPORT_CUSTOMS_CLEARED`,
`RECEIVED_FROM_FLIGHT`, `DELIVERED_AT_AIRPORT`, `STRIPPING`, `ARRIVAL`, `UNLOADED`

> **When `enable_gate_in_gate_out_reversal` is enabled**: `GATE_IN` moves to END triggers; `GATE_OUT` moves to START triggers.

**Rule**: iterate legs in sequence order; stitch to the **first** leg where:
- START trigger AND `clusterLocationCode == leg.startLocation` (not a duplicate start location), OR
- END trigger AND `clusterLocationCode == leg.endLocation` (not a duplicate end location)

### Priority 4 — Duplicate Location → Circular Leg Resolution by Cluster Sequence
If the cluster location appears at **multiple** leg boundaries and the trigger is START/END, DOS resolves the ambiguity using cluster sequence number proximity:

1. Walk legs in **reverse** up to the highest already-assigned cluster's leg sequence.
2. Filter by trigger side and location match.
3. **Vessel clusters** (`LOADED_ON_VESSEL`, `VESSEL_DEPARTURE`, `VESSEL_ARRIVAL`, `UNLOADED_FROM_VESSEL`) → remove non-OCEAN legs (controlled by `enableOceanModeVesselEvents` flag).
4. **Non-vessel clusters** → remove OCEAN legs (when `enable_non_ocean_mode_non_vessel_events` flag + dynamic flag both enabled).
5. If a cluster's `clusterSequenceNumber` falls **between** the min and max sequence of clusters already on a candidate leg → assign there.
6. Otherwise → assign to the candidate leg whose existing clusters are **closest in sequence number** to this cluster.
7. If no cluster has been assigned to a candidate leg yet (and `enableMinorImprovementsToStitchingLogic` is on) → leave it for P5.

### Priority 5 — Any Location Fallback
If still unassigned, iterate legs in order. Stitch to the first leg where:
`clusterLocationCode == leg.startLocation` OR `clusterLocationCode == leg.endLocation`
(ignoring trigger side)

### Priority 6 — DELIVERY_CONFIRMED Orphan
If `DELIVERY_CONFIRMED` cluster remains unassigned after all above → **last leg**.

### Priority 7 — True Orphan
No leg matched. Cluster appears in `CorrelatedShipmentJourney.events` (top-level orphan list), not on any leg.

---

## Step 6 — Identify the Mismatch Layer

Apply this decision tree:

| Simulated DOS leg | GIS `journey_request` leg | Transport Plan leg | Verdict |
|---|---|---|---|
| Correct | Correct | Correct | No platform fault — investigate consumer |
| Correct | Correct | **Wrong** | Transport plan issue → `/transport-plan-investigation` |
| **Wrong** | Correct | Correct | GIS persistence/sync issue — raise DI Engineering ticket |
| **Wrong** | **Wrong** | Correct | DOS stitching logic issue — check cluster data + feature flags |
| **Wrong** | **Wrong** | **Wrong** | Source event or transport plan issue — check DUST → MP → VRDAN |
| GIS has no row | — | — | GIS never processed; check `audit_trail` for rejections |
| — | BLOB storage | — | MCE data in blob; use GIS + DOS evidence only |

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

Cluster Context (from MCE journey_subscription):
  clusterIdentifier:   <CLUSTER_ID>
  clusterTimestamp:    <CLUSTER_TS>
  clusterLocationCode: <CLUSTER_LOC>
  clusterSequenceNum:  <SEQ>
  eventsInCluster:     <N>
  providers:           [<PROVIDER_1>, ...]

Transport Plan (from DOS shipment_journey):
  [table: legSeq | mode | from | to | vessel | voyage | plannedDep | plannedArr]

GIS Stitching (from journey_request):
  stitchedLeg:     <LEG_SEQUENCE>  (<FROM> → <TO>)

DOS Stitching Simulation:
  stitchingPhase:  <P1|P2|P3|P4|P5|P6|P7>
  simulatedLeg:    <LEG_SEQUENCE>  (<FROM> → <TO>)
  phaseReason:     <e.g. "P3: VESSEL_DEPARTURE is a START trigger; cluster location CNSHA
                    matches leg 2 startLocation CNSHA (unique boundary — not a duplicate)">

Discrepancy:       <YES | NO>
  [if YES: simulatedLeg=<X> but GIS shows leg=<Y> — possible persistence issue]

Alternative candidate legs:
  leg <N>: <from> → <to>
    skipped because: <location mismatch / wrong trigger side / duplicate location resolved to another leg>

Assessment:        Correct / Suspicious / Incorrect

Likely Root Cause:
  Source event data issue / transport plan issue / MCE clustering issue /
  DOS stitching feature flag / GIS persistence issue

Recommended Action:
  No action needed
  Raise provider data quality ticket
  Fix transport plan → /transport-plan-investigation
  Investigate MCE cluster composition
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

Cluster Stitching Table (simulated from MCE subscription_data):
  [table: clusterId | clusterTs | clusterLoc | seqNum | trigger | simulatedLeg | phase | gisLeg | discrepancy]

MCE Storage:  IN_MEMORY (parsed) / BLOB (unavailable — see blob_url)

Discrepancies / Suspicious Clusters:
  [list with reason: orphan cluster / wrong-side trigger / vessel on non-ocean leg /
   duplicate location resolved to unexpected occurrence / GIS vs simulation mismatch]

Overall Root Cause:
  [Pattern summary across all clusters]

Recommended Action:
  [Prioritized action plan]
```

---

## Verdict Reference

| Observation | Culprit | Action |
|---|---|---|
| No GIS row for journey+container | Event never reached GIS | Check `audit_trail WHERE unit_of_tracking = '<CONTAINER>'` for REJECTED |
| GIS `journey_request` has wrong leg; DOS simulation has correct leg | GIS persistence bug | Raise DI Engineering ticket |
| DOS simulation has wrong leg; transport plan and cluster are correct | DOS stitching logic / feature flag | Check feature flags; raise DI Engineering ticket with cluster data |
| MCE cluster has wrong location / wrong trigger | MCE clustering bug | Raise DI Engineering ticket with `subscription_data` cluster details |
| MCE selected wrong cluster location because source event has wrong location | Source data issue | Check DUST → MP → VRDAN for provider payload |
| Cluster is P7 orphan (no leg matched) | Location mismatch between cluster and all leg boundaries | Investigate transport plan completeness or cluster location accuracy |
| Vessel cluster stitched to non-ocean leg | `enableOceanModeVesselEvents` flag / duplicate location resolution | Check feature flags |
| MCE data in BLOB storage | Cannot parse in-session | Note blob URL; rely on GIS + DOS evidence only |