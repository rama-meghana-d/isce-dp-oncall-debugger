# Vessel Event Investigation

Investigates incorrect vessel name/voyage number or incorrect ETA/ATA/ETD/ATD timestamps for a specific vessel event on an ocean leg at a given location. Traces from the Intelligent Journey down through Milestone Processor, DUST, and Shipment Visibility to pinpoint the root cause.

## Identifier Clarification

| Identifier | What it is | Where it appears |
|-----------|-----------|-----------------|
| `journey_id` (input) | Shipment Journey ID — the external, cross-service identifier | `intelligent_journey.journey_id`, `sv_journey.shipment_journey_id` |
| SV internal `journey_id` | SV-internal UUID PK for the journey row | `sv_journey.journey_id` — must be resolved from `shipment_journey_id + container` before joining SV tables |

## Event Type Mapping

| Input | eventTriggerCode | eventTimingCode |
|-------|-----------------|-----------------|
| ETA | VESSEL_ARRIVAL | ESTIMATED |
| ATA | VESSEL_ARRIVAL | ACTUAL |
| ETD | VESSEL_DEPARTURE | ESTIMATED |
| ATD | VESSEL_DEPARTURE | ACTUAL |

## Input

```
journey_id: <SHIPMENT_JOURNEY_ID>      (shipment journey ID — external cross-service key)
container: <CONTAINER_NUMBER>          (unit of tracking)
leg_sequence: <LEG_SEQUENCE_NUMBER>
event_type: ETA | ATA | ETD | ATD
location_code: <LOCATION_CODE>         (UNLOC or port code where the event occurs)
problem: vessel_voyage | timestamp     (what is incorrect)

# Required when problem = vessel_voyage
expected_vessel_name: <EXPECTED_VESSEL_NAME>
expected_voyage_number: <EXPECTED_VOYAGE_NUMBER>

# Required when problem = timestamp
vessel_name: <VESSEL_NAME>             (vessel to look up in SV port call)
voyage_number: <VOYAGE_NUMBER>         (voyage to look up in SV port call)
expected_date: <YYYY-MM-DD>            (date only — comparison is date-level, not full timestamp)
```

$ARGUMENTS

Derive from `event_type` before proceeding:
- `event_trigger` = `VESSEL_ARRIVAL` for ETA/ATA, `VESSEL_DEPARTURE` for ETD/ATD
- `event_timing`  = `ESTIMATED` for ETA/ETD, `ACTUAL` for ATA/ATD

---

## Step 1 — Check Intelligent Journey at Event Connection Level

Load `mcp__dos-db__query` via ToolSearch.

```sql
SELECT journey_id, unit_of_tracking, journey::text, updated_on
FROM intelligent_journey
WHERE journey_id = '<SHIPMENT_JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY updated_on DESC LIMIT 1;
```

In the returned `journey` JSON (`ShipmentJourneyV2`), navigate to:

```
shipmentJourneyLegs
  → [ leg where sequence = <LEG_SEQUENCE> ]
    → events
      → [ event where eventTrigger.eventTriggerCode = <EVENT_TRIGGER>
                  AND eventTiming.eventTimingCode   = <EVENT_TIMING>
                  AND locations contains <LOCATION_CODE> ]
        → eventConnections
            messageLevelCode = "VESSEL_NAME"   → IJ vessel name
            messageLevelCode = "VOYAGE_NUMBER" → IJ voyage number
        → timestamp  ← ETA / ATA / ETD / ATD value
```

Record and carry forward into all subsequent steps:
- **IJ vessel name** and **IJ voyage number** from `eventConnections`
- **IJ timestamp** from `timestamp`

### Step 1a — Discrepancy Gate for Path A (vessel_voyage only)

*Only applies when `problem: vessel_voyage`. Path B (timestamp) starts directly at SV — skip this gate.*

Compare IJ vessel name and voyage number against `expected_vessel_name` and `expected_voyage_number`:
- Either value differs → **Discrepancy at IJ.** Proceed to Node A2 (trace MP → DUST).
- Both match → **No discrepancy at IJ.** Proceed to Node A3 (check SV layer only).

> If after all investigation no discrepancy is found at any queried layer, output: **"No discrepancy found — all layers match expected values. No platform issue detected."** and stop.

---

## Path A — Vessel Name or Voyage Number is Incorrect

*Follow this path when `problem: vessel_voyage`.*

### Node A1: Is vessel/voyage correct in the Intelligent Journey?

Compare IJ vessel name and voyage number (from Step 1a) against `expected_vessel_name` and `expected_voyage_number`.

**INCORRECT at IJ** (either value differs from expected) → Proceed to Node A2 (trace back to Milestone Processor).

**CORRECT at IJ** (both values match expected) → Proceed to Node A3 (check Shipment Visibility layer).

---

### Node A2 — Trace Incorrect Vessel/Voyage Back to Milestone Processor

Load `mcp__milestone-processor-db__query` via ToolSearch.

```sql
SELECT smm.job_id, smm.unit_of_tracking, smm.journey_leg_id, smm.status,
       m.enriched_data::text,
       m.updated_at
FROM subscription_milestone_metadata smm
JOIN milestone m
  ON m.job_id = smm.job_id
 AND m.unit_of_tracking = smm.unit_of_tracking
WHERE smm.journey_id = '<SHIPMENT_JOURNEY_ID>'
  AND smm.unit_of_tracking = '<CONTAINER>'
  AND smm.status = 'ACTIVE'
ORDER BY m.updated_at DESC;
```

In `enriched_data` (Map<String, Event> JSONB), find the event matching `<EVENT_TRIGGER>` + `<EVENT_TIMING>` + `<LOCATION_CODE>`. Extract vessel name and voyage number from that event's connections.

| Finding | Root Cause | Action |
|---------|-----------|--------|
| MP `enriched_data` has **wrong** vessel/voyage | Data provider sent wrong data → trace to DUST (Node A2a) | Proceed to Node A2a with `job_id` |
| MP `enriched_data` has **correct** vessel/voyage | MP → IJ enrichment corrupted the value | Flag to DI engineering: MP event vs IJ `eventConnections` diff |

#### Node A2a — Confirm in DUST (raw provider payload)

Load `mcp__dust-db__query` via ToolSearch.

```sql
SELECT pipeline_id, job_id, identifier_id, data::text AS raw_milestone, created_at, updated_at
FROM milestonepipeline
WHERE (pipeline_id LIKE '%<JOB_ID>%' OR identifier_id = '<CONTAINER>')
ORDER BY updated_at DESC LIMIT 5;
```

In `data`, find the vessel event at `<LOCATION_CODE>` for `<EVENT_TRIGGER>` + `<EVENT_TIMING>`. Check vessel name and voyage number.

| Finding | Root Cause | Action |
|---------|-----------|--------|
| DUST `data` has **wrong** vessel/voyage | **Data provider sent incorrect vessel/voyage** | Load `mcp__das-db__query`: `SELECT job_id, dp_name, gathering_status FROM das_request_store WHERE job_id = '<JOB_ID>'`. Report provider name and raw value sent |
| DUST `data` has **correct** vessel/voyage but MP has wrong | **Milestone Processor enrichment bug** | Provide DUST raw event vs MP `enriched_data` diff; flag to DI engineering |

---

### Node A3 — Check Shipment Visibility Layer (IJ vessel/voyage is correct)

Load `mcp__shipment-visibility-db__query` via ToolSearch.

**Sub-step: Resolve SV internal journey_id** from shipment_journey_id + container:

```sql
SELECT j.journey_id        AS sv_journey_id,
       j.shipment_journey_id,
       j.status,
       j.updated_at
FROM journey j
JOIN container c ON c.shipment_unit_id = j.shipment_unit_id
WHERE c.container_number   = '<CONTAINER>'
  AND j.shipment_journey_id::text = '<SHIPMENT_JOURNEY_ID>'
ORDER BY j.updated_at DESC LIMIT 1;
```

**Not found** → SV does not hold this journey. Since IJ is correct, escalate to CP team — issue is in CP layer or downstream consumer.

**Found** → Use `sv_journey_id` (SV internal PK) for the next query. Check vessel/voyage at event connection level for the leg + event + location:

```sql
SELECT ec.connection_type, ec.entity_identifier,
       e.event_trigger, e.event_timing
FROM event_connection ec
JOIN event e        ON e.event_id   = ec.event_id
JOIN transport_leg tl ON tl.leg_id  = e.leg_id
WHERE tl.journey_id      = '<SV_JOURNEY_ID>'
  AND tl.sequence_number = <LEG_SEQUENCE>
  AND e.event_trigger    = '<EVENT_TRIGGER>'
  AND e.event_timing     = '<EVENT_TIMING>'
  AND ec.connection_type IN ('VESSEL_NAME', 'VOYAGE_NUMBER');
```

| Finding | Root Cause | Action |
|---------|-----------|--------|
| SV `event_connection` has **wrong** vessel/voyage | **SV has incorrect vessel/voyage at event level** | Provide IJ `eventConnections` (correct) vs SV `event_connection` (wrong); flag to DI/SV engineering |
| SV `event_connection` has **correct** vessel/voyage | SV data correct — issue is in CP query or presentation layer | Escalate to CP team |

---

## Path B — ETA / ATA / ETD / ATD Timestamp is Incorrect

*Follow this path when `problem: timestamp`.*

Investigation starts at Shipment Visibility (if present), not at IJ. `vessel_name` and `voyage_number` are required inputs for this path.

---

### Node B1 — Check if Journey Exists in Shipment Visibility

Load `mcp__shipment-visibility-db__query` via ToolSearch.

**Resolve SV internal journey_id from shipment_journey_id + container:**

```sql
SELECT j.journey_id        AS sv_journey_id,
       j.shipment_journey_id,
       j.status,
       j.updated_at
FROM journey j
JOIN container c ON c.shipment_unit_id = j.shipment_unit_id
WHERE c.container_number         = '<CONTAINER>'
  AND j.shipment_journey_id::text = '<SHIPMENT_JOURNEY_ID>'
ORDER BY j.updated_at DESC LIMIT 1;
```

**Not found in SV** → SV flow does not apply. Proceed directly to Node B4 (IJ check).

**Found** → Record `sv_journey_id`. Proceed to Node B2.

---

### Node B2 — Fetch SV Port Call Timestamp and Verify Linkage

Using `vessel_name` + `voyage_number` (from input) + `location_code` + `sv_journey_id` — fetch the committed and pending timestamps and verify port call is linked to this journey's leg:

```sql
SELECT tl.sequence_number,
       vv.vessel_name,
       vv.voyage_number,
       vvpct.event_trigger,
       vvpct.event_timing,
       vvpct.timestamp            AS sv_timestamp,
       vvpct.pending_timestamp,
       vvpct.source_carrier,
       vvpct.source_container,
       vvpct.updated_at
FROM transport_leg tl
JOIN vessel_voyage vv               ON vv.vessel_voyage_id    = tl.vessel_voyage_id
JOIN vessel_voyage_port_call vvpc   ON vvpc.vessel_voyage_id  = vv.vessel_voyage_id
JOIN location l                     ON l.location_id          = vvpc.location_id
JOIN vessel_voyage_port_call_timestamp vvpct ON vvpct.port_call_id = vvpc.id
WHERE tl.journey_id        = '<SV_JOURNEY_ID>'
  AND tl.sequence_number   = <LEG_SEQUENCE>
  AND vv.vessel_name       = '<VESSEL_NAME>'
  AND vv.voyage_number     = '<VOYAGE_NUMBER>'
  AND l.location_code      = '<LOCATION_CODE>'
  AND vvpct.event_trigger  = '<EVENT_TRIGGER>'
  AND vvpct.event_timing   = '<EVENT_TIMING>'
ORDER BY vvpct.updated_at DESC LIMIT 5;
```

Compare `sv_timestamp` date (YYYY-MM-DD) against `expected_date`:

---

#### Branch B2-CORRECT — SV timestamp matches `expected_date`

SV data is correct. The issue is downstream of SV. Check DFS guardrail layer:

> **No `mcp__dfs-db__query` MCP tool configured.** Use the DFS REST endpoint:
> `GET http://<dfs-host>/guardRail/journeyId/<SHIPMENT_JOURNEY_ID>/unitOfTracking/<CONTAINER>`
>
> Or check Grafana (`tag: isce`) → DFS service → validation failure counters, or DFS pod logs for `GUARDRAIL_FAILED`.

| DFS Result | Root Cause | Action |
|-----------|-----------|--------|
| All guardrails **PASSED**, message published | **Issue is NOT at data platform layer** — SV and DFS are both correct | Declare no platform fault; escalate to CP / consumer team |
| Guardrail **FAILED**, message dropped | **DFS guardrail failure** — data platform dropped the message at the DFS gate | Flag which rule failed; provide `sv_timestamp` (correct) as evidence; flag to DI engineering for rule review |

---

#### Branch B2-INCORRECT — SV timestamp does NOT match `expected_date`

SV data is wrong. Check `pending_timestamp` from the query above:

| `pending_timestamp` | Action |
|--------------------|--------|
| `pending_timestamp` date **matches** `expected_date` | **Manual port call update required** — the correct timestamp exists as a pending update in SV but has not been committed. Action: promote the pending timestamp via the SV UI / port call management tool |
| `pending_timestamp` is NULL or date **does not match** `expected_date` | SV has no correct value anywhere → Proceed to Node B4 (check IJ) |

---

### Node B4 — Check Intelligent Journey

> Reach here either from: (a) journey not found in SV, or (b) SV `pending_timestamp` also incorrect.

Load `mcp__dos-db__query` via ToolSearch.

```sql
SELECT journey_id, unit_of_tracking, journey::text, updated_on
FROM intelligent_journey
WHERE journey_id      = '<SHIPMENT_JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY updated_on DESC LIMIT 1;
```

Navigate in the JSON to the target event at `leg_sequence` + `<EVENT_TRIGGER>` + `<EVENT_TIMING>` + `<LOCATION_CODE>`. Extract:
- vessel name and voyage number from `eventConnections`
- timestamp date (YYYY-MM-DD)

Compare all three against `vessel_name`, `voyage_number`, and `expected_date` from input:

| IJ Finding | Root Cause | Action |
|-----------|-----------|--------|
| IJ vessel name, voyage number, and timestamp date all **correct** | **SV sync issue** — IJ has correct data but SV port call is not reflecting it | Provide IJ values (correct) vs SV `sv_timestamp` (wrong); flag to DI/SV engineering |
| IJ vessel name, voyage number, or timestamp date **incorrect** | **DOS data is incorrect** — IJ itself has wrong values → Proceed to Node B5 to identify the data provider at Milestone Processor |

---

### Node B5 — Identify Incorrect Data Provider at Milestone Processor

Load `mcp__milestone-processor-db__query` via ToolSearch.

```sql
SELECT smm.job_id, smm.unit_of_tracking, smm.journey_leg_id,
       m.enriched_data::text,
       m.updated_at
FROM subscription_milestone_metadata smm
JOIN milestone m
  ON m.job_id = smm.job_id
 AND m.unit_of_tracking = smm.unit_of_tracking
WHERE smm.journey_id      = '<SHIPMENT_JOURNEY_ID>'
  AND smm.unit_of_tracking = '<CONTAINER>'
  AND smm.status = 'ACTIVE'
ORDER BY m.updated_at DESC;
```

In `enriched_data`, find the event matching `<EVENT_TRIGGER>` + `<EVENT_TIMING>` + `<LOCATION_CODE>`. Identify:
- Which data provider (`data_provider` field) sent this event
- What timestamp value they provided
- Whether vessel name and voyage number in the event also match the expected values

| Finding | Root Cause | Action |
|---------|-----------|--------|
| MP `enriched_data` has **wrong** timestamp / vessel / voyage | **Data provider sent incorrect event** — flag the provider | Report: `job_id`, `data_provider` name, wrong values sent vs expected; escalate to provider team |
| MP `enriched_data` has **correct** values but IJ is wrong | **MP → IJ enrichment bug** — correct at MP, corrupted during DOS enrichment pipeline | Provide MP values (correct) vs IJ values (wrong); flag to DI engineering |

---

## RCA Report

Always populate every field below with the actual queried values. Do not leave a layer blank if it was queried — write the value or "not found / not applicable".

```
═══════════════════════════════════════════════════════
VESSEL EVENT INVESTIGATION REPORT
═══════════════════════════════════════════════════════

INPUT
  shipment_journey_id : <SHIPMENT_JOURNEY_ID>
  container           : <CONTAINER>
  leg_sequence        : <LEG_SEQUENCE>
  event_type          : <ETA|ATA|ETD|ATD>
  location_code       : <LOCATION_CODE>
  problem             : <vessel_voyage | timestamp>

───────────────────────────────────────────────────────
EXPECTED vs ACTUAL
───────────────────────────────────────────────────────

  [For problem: vessel_voyage]
  expected_vessel_name    : <expected_vessel_name from input>
  expected_voyage_number  : <expected_voyage_number from input>

  [For problem: timestamp]
  expected_date           : <expected_date from input (YYYY-MM-DD)>

───────────────────────────────────────────────────────
VALUES AT EACH LAYER
───────────────────────────────────────────────────────

INTELLIGENT JOURNEY (dos-db: intelligent_journey)
  vessel_name         : <value from eventConnections VESSEL_NAME>   [MATCH / MISMATCH vs expected]
  voyage_number       : <value from eventConnections VOYAGE_NUMBER> [MATCH / MISMATCH vs expected]
  ETA                 : <timestamp if event_trigger=VESSEL_ARRIVAL, event_timing=ESTIMATED — else N/A>   [date: MATCH / MISMATCH vs expected_date]
  ATA                 : <timestamp if event_trigger=VESSEL_ARRIVAL, event_timing=ACTUAL    — else N/A>   [date: MATCH / MISMATCH vs expected_date]
  ETD                 : <timestamp if event_trigger=VESSEL_DEPARTURE, event_timing=ESTIMATED — else N/A> [date: MATCH / MISMATCH vs expected_date]
  ATD                 : <timestamp if event_trigger=VESSEL_DEPARTURE, event_timing=ACTUAL    — else N/A> [date: MATCH / MISMATCH vs expected_date]
  location_code       : <LOCATION_CODE confirmed in event locations>
  updated_on          : <intelligent_journey.updated_on>

SHIPMENT VISIBILITY (shipment-visibility-db)   [if journey found in SV]
  sv_journey_id       : <SV internal journey PK resolved from shipment_journey_id + container>
  vessel_name         : <value from SV event_connection VESSEL_NAME — Path A>
  voyage_number       : <value from SV event_connection VOYAGE_NUMBER — Path A>
  ETA                 : <sv_timestamp where event_trigger=VESSEL_ARRIVAL, event_timing=ESTIMATED — else N/A>
  ATA                 : <sv_timestamp where event_trigger=VESSEL_ARRIVAL, event_timing=ACTUAL    — else N/A>
  ETD                 : <sv_timestamp where event_trigger=VESSEL_DEPARTURE, event_timing=ESTIMATED — else N/A>
  ATD                 : <sv_timestamp where event_trigger=VESSEL_DEPARTURE, event_timing=ACTUAL    — else N/A>
  location_code       : <LOCATION_CODE confirmed via location table>
  port_call_linked_to_leg : <yes — leg_sequence matches | no — linkage missing>
  source_carrier      : <vvpct.source_carrier>
  sv_updated_at       : <vvpct.updated_at>

  [If journey not found in SV: "Journey not present in Shipment Visibility — SV layer not applicable"]

MILESTONE PROCESSOR (milestone-processor-db: milestone)   [if traced]
  job_id              : <job_id from subscription_milestone_metadata>
  vessel_name         : <value in enriched_data event connections>
  voyage_number       : <value in enriched_data event connections>
  timestamp           : <timestamp in enriched_data for this event+timing+location>
  location_code       : <LOCATION_CODE confirmed in enriched_data event>
  mp_updated_at       : <milestone.updated_at>

DUST (dust-db: milestonepipeline)   [if traced]
  pipeline_id         : <value>
  vessel_name         : <raw value in data JSON>
  voyage_number       : <raw value in data JSON>
  timestamp           : <raw timestamp in data JSON>
  location_code       : <LOCATION_CODE confirmed in raw data>
  dust_updated_at     : <milestonepipeline.updated_at>

DATA PROVIDER (das-db: das_request_store)   [if DP identified as culprit]
  dp_name             : <provider name>
  job_id              : <job_id>
  gathering_status    : <value>

───────────────────────────────────────────────────────
ROOT CAUSE
───────────────────────────────────────────────────────
  [one sentence — e.g. "Data provider GNOSIS sent voyage number 502W instead of 503E
   for VESSEL_DEPARTURE ESTIMATED at CNSHA on leg 2"]

CULPRIT
  [Data Provider: <dp_name> |
   Milestone Processor enrichment |
   SV port call timestamp incorrect |
   SV port call linkage missing |
   SV event_connection vessel/voyage mismatch |
   CP/consumer layer — data platform is correct]

RECOMMENDED ACTION
  [escalate to DP provider team — job_id: <id>, dp_name: <name>, raw value sent: <x>, expected: <y> |
   flag MP enrichment bug — MP value: <x>, IJ value: <y> |
   flag SV port call timestamp — IJ value: <x>, SV value: <y>, source_carrier: <z> |
   flag SV port call not linked to leg <seq> for vessel+voyage+location |
   flag SV event_connection mismatch — IJ vessel/voyage: <x>, SV event_connection: <y> |
   escalate to CP team — all platform layers show correct values]
═══════════════════════════════════════════════════════
```
