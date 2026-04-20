# Transport Plan Investigation

Decision-tree RCA skill for transport plan issues. Traces the transport plan through TPP's booking-level and execution-level rows, Route Reconciliation output, and raw DP updates to pinpoint whether the root cause is a data provider, CP/SCP, or a merge logic bug.

## Input

```
journey_id: <JOURNEY_ID>               (mandatory)
container: <CONTAINER_NUMBER>          (unit of tracking — mandatory)
subscription_id: <SUBSCRIPTION_ID>     (optional)
job_id: <JOB_ID>                       (optional — speeds up DUST query; resolved from SIR if missing)
```

$ARGUMENTS

---

## Step 1 — Resolve job_id (skip if already provided)

If `job_id` is not in the input, load `mcp__sir-db__query` via ToolSearch:

```sql
SELECT sj.job_id, sj.subscription_id, sj.unit_of_tracking_value, sj.status, sj.created_on
FROM subscription_job_tracking sj
JOIN subscription s ON s.subscription_id = sj.subscription_id
WHERE s.journey_id = '<JOURNEY_ID>'
ORDER BY sj.created_on DESC LIMIT 5;
```

Pick the most recent active `job_id` for the given `unit_of_tracking_value` matching `<CONTAINER>`.

---

## Step 2 — Inspect TPP Current State

Load `mcp__transport-plan-db__query` via ToolSearch. Run all three queries.

### Query 1 — journey table: booking-level AND execution-level rows

The `journey` table holds two rows per journey:
- `unit_of_tracking = NULL` — booking-level base written by CP from the `cp-sjs-data` topic (SCP booking legs)
- `unit_of_tracking = '<CONTAINER>'` — execution-level row; seeded from booking on first DP update, then overwritten when CP execution journey arrives via `cp-sjs-journey-data` topic

```sql
SELECT id, journey_id, unit_of_tracking, source, journey::text, created_on, updated_on
FROM journey
WHERE journey_id = '<JOURNEY_ID>'
  AND (unit_of_tracking = '<CONTAINER>' OR unit_of_tracking IS NULL)
ORDER BY updated_on DESC;
```

### Query 2 — journey_updates table: DP transport plan updates only

This table stores **only Data Provider originated transport plans**. CP/SCP updates never appear here. Use it to inspect what each DP sent and which provider sent it.

Only the 2 newest records per `(journey_id, unit_of_tracking)` are retained.

```sql
SELECT id, journey_id, unit_of_tracking, data_provider,
       data::text AS dp_journey_update,
       created_at
FROM journey_updates
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY created_at DESC LIMIT 5;
```

### Query 3 — journey_reconciliation_updates table: Route Reconciliation output

This is the merged output of DP transport plan + TPG milestone-inferred legs, compared against the SCP booking-level base. Compare `suggested_journey_update` against what CP ultimately wrote to the execution-level `journey` row.

```sql
SELECT id, journey_id, unit_of_tracking, data_provider,
       suggested_journey_update::text,
       reconcile_actions::text,
       created_on
FROM journey_reconciliation_updates
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY created_on DESC LIMIT 5;
```

`reconcile_actions` values: `VESSEL_UPDATE`, `TRANSPORT_MODE_UPDATE`, `JOURNEY_UPDATE`, `INTERMEDIATE_LOCATION_UPDATE`, `BROKEN_JOURNEY_REVIEW`.

---

## Step 3 — Inspect Raw DP Transport Plan (DUST)

Load `mcp__dust-db__query` via ToolSearch. Fetch the raw payload the data provider sent before TPP processed it:

```sql
SELECT pipeline_id, job_id, transport_plan::text, status, created_at, updated_at
FROM transportplanpipeline
WHERE pipeline_id LIKE '%<JOB_ID>%'
   OR job_id::text = '<JOB_ID>'
ORDER BY updated_at DESC LIMIT 5;
```

---

## Step 4 — Decision Tree

The investigation always targets the **execution-level `journey` row** (`unit_of_tracking = '<CONTAINER>'`). The booking-level row (`unit_of_tracking = NULL`) is reference only.

---

### Node 1: Is the execution-level journey row correct?

Compare the execution-level `journey.journey` legs from Step 2 Query 1 against the expected behavior from the incident:
- Check POL, POD, intermediate ports, vessel, transport mode, and leg count.

**YES — correct** → Transport plan layer is clean. Proceed to downstream tracing:
- Load `mcp__dos-db__query`: check `shipment_journey` and `intelligent_journey` for divergence from TPP output
- Load `mcp__gis-db__query`: check `audit_trail` WHERE `transactionType = 'REJECTED'` for `IS_CONSISTENT = UNKNOWN`
- Follow §4 of root CLAUDE.md for DOS → GIS → MCE chain

**NO — incorrect** → Proceed to Node 2.

---

### Node 2: Is the reconciled update correct?

From Step 2 Query 3, inspect `suggested_journey_update` and `reconcile_actions` from `journey_reconciliation_updates`.

Compare `suggested_journey_update` legs against the expected correct transport plan.

| Finding | Root Cause | Action |
|---------|-----------|--------|
| `suggested_journey_update` is **correct**, no `BROKEN_JOURNEY_REVIEW` in `reconcile_actions`, but execution-level `journey` row is **wrong** | **CP did not provide a correct execution transport plan** — the CP execution journey (`cp-sjs-journey-data` topic) overwrote the correctly reconciled data with bad legs | Escalate to CP / Consumer Platform team. Evidence: show the correct `suggested_journey_update` alongside the incorrect execution-level `journey` row with timestamps |
| `suggested_journey_update` is **correct** but `reconcile_actions` contains **`BROKEN_JOURNEY_REVIEW`** | **Broken journey** — start/end location mismatch between DP/TPG merged output and SCP booking-level base. Not a platform bug; DP tracking data and SCP booking describe different routes | Flag to SCP/CP team. Evidence: show `suggested_journey_update` legs vs booking-level `journey` (UOT=NULL) legs side by side; explain that `BROKEN_JOURNEY_REVIEW` means SCP booking and provider tracking data do not align on route |
| `suggested_journey_update` is also **wrong** | Route Reconciliation input is bad → Proceed to Node 3 | |

---

### Node 3: Which data provider caused the incorrect reconciled update?

From Step 2 Query 2, inspect `journey_updates` — the `data_provider` column identifies which provider sent each update.

```sql
SELECT id, journey_id, unit_of_tracking, data_provider,
       data::text AS journey_update,
       created_at
FROM journey_updates
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY created_at DESC LIMIT 5;
```

Cross-check the raw payload from DUST (Step 3) to confirm what was ingested.

| Finding | Root Cause | Action |
|---------|-----------|--------|
| `journey_updates` legs from the named `data_provider` are **wrong** (wrong vessel / ports / leg count vs expected) | **Data Provider Issue** — the named provider sent incorrect transport plan data | Load `mcp__das-db__query`: `SELECT job_id, dp_name, gathering_status, publishing_status FROM das_request_store WHERE job_id = '<JOB_ID>'`. Output: provider name, what was sent vs expected, job gathering status |
| `journey_updates` legs are **correct** but `suggested_journey_update` is still wrong | **Route Reconciliation merge bug** — `RouteMerger` produced incorrect output from correct inputs | Provide `journey_updates` input legs and `suggested_journey_update` output legs as diff evidence; escalate to DI engineering team |

---

## Step 5 — RCA Report

Output a structured verdict for whichever node was reached:

```
ROOT CAUSE:
  [one sentence — e.g. "Data provider P44 sent incorrect vessel on leg 2"]

CULPRIT:
  [Data Provider: <name> | CP execution transport plan | SCP broken journey | Route Reconciliation merge bug]

EVIDENCE:
  - journey_reconciliation_updates.suggested_journey_update: <key legs>
  - journey (execution-level) legs: <key legs>
  - journey_updates.data_provider: <provider name>, created_at: <timestamp>
  - transportplanpipeline.transport_plan: <relevant diff>
  - reconcile_actions: <list>

RECOMMENDED ACTION:
  [escalate to DP provider team with job_id + dp_name |
   escalate to CP team with suggested_journey_update vs journey diff |
   flag BROKEN_JOURNEY_REVIEW to SCP/CP with booking vs DP route comparison |
   raise Route Reconciliation bug with input/output diff]
```
