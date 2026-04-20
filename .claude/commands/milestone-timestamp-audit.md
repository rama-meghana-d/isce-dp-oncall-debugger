# Milestone Timestamp Audit

For one or more containers, audits every actual milestone — showing the event timestamp (when the physical event occurred, as reported by the provider) versus when it was ingested into the ISCE platform. Useful for answering: "when did this event happen and when did we know about it?" Also checks VRDAN for raw payload coverage per container.

## Input

```
containers:     <CONTAINER_1>, <CONTAINER_2>, ...     (required — comma-separated list)
event_timing:   ALL | ACTUAL | ESTIMATED               (optional — default: ALL)
event_trigger:  ALL | <TRIGGER_NAME>                   (optional — default: ALL)
```

**`event_timing` values:** `ACTUAL` (ATA/ATD), `ESTIMATED` (ETA/ETD), `ALL` (both)

**Common `event_trigger` values:**
`EMPTY_CONTAINER_DISPATCHED`, `GATE_IN`, `GATE_OUT`, `LOADED_ON_VESSEL`, `VESSEL_DEPARTURE`, `VESSEL_ARRIVAL`, `UNLOADED_FROM_VESSEL`, `CUSTOMS_RELEASED`, `DELIVERED`

If `event_timing` or `event_trigger` is omitted or set to `ALL`, do not add that filter to the query.

$ARGUMENTS

---

## Step 1 — Load MCP Tools

Load these tools via `ToolSearch select:<name>` before first use:
- `mcp__sir-db__query`
- `mcp__gis-db__query`
- `mcp__das-db__query`
- `mcp__vrdan-db__query`

---

## Step 2 — Resolve Journey IDs (SIR)

Query SIR `subscription` table. Use `LIKE` (not `IN`) because `unit_of_tracking` is stored as JSONB and must be cast to text first.

```sql
-- mcp__sir-db__query
SELECT DISTINCT ON (unit_of_tracking::text)
  journey_id,
  unit_of_tracking::text AS container,
  status,
  created_on
FROM subscription
WHERE unit_of_tracking::text LIKE '%<CONTAINER_1>%'
   OR unit_of_tracking::text LIKE '%<CONTAINER_2>%'
   -- ... one OR clause per container
ORDER BY unit_of_tracking::text, created_on DESC;
```

**If the result hits the 100-row cap:** run again with `OFFSET 100` for remaining containers.

For each container, take the **most recent** `journey_id` (highest `created_on`). Note any containers with no result — they have no SIR subscription and cannot be traced further.

---

## Step 3 — Query GIS Milestone Events

Use all resolved `journey_id`s in one IN clause.

> **Key schema facts for `audit_trail`:**
> - Event type: `change_log->>'eventTrigger'`
> - Event timestamp: `to_timestamp((change_log->>'timestamp')::double precision)` — **must cast to `double precision`, NOT `bigint`** (stored as float, bigint cast will error)
> - When added to system: `created_on`
> - Event timing filter: `event_timing` column — values: `ACTUAL`, `ESTIMATED`
> - Always filter added (not rejected): `change_log->>'transactionType' = 'ADDED'`

Build the WHERE clause based on inputs:

| Input | Add to WHERE |
|---|---|
| `event_timing = ACTUAL` | `AND event_timing = 'ACTUAL'` |
| `event_timing = ESTIMATED` | `AND event_timing = 'ESTIMATED'` |
| `event_timing = ALL` or omitted | *(no timing filter)* |
| `event_trigger = <TRIGGER>` | `AND change_log->>'eventTrigger' = '<TRIGGER>'` |
| `event_trigger = ALL` or omitted | *(no trigger filter)* |

```sql
-- mcp__gis-db__query
SELECT
  unit_of_tracking                                                   AS container,
  event_timing,
  change_log->>'eventTrigger'                                        AS event_trigger,
  to_timestamp((change_log->>'timestamp')::double precision)         AS event_timestamp,
  created_on                                                         AS added_to_system
FROM audit_trail
WHERE journey_id IN (
  '<JOURNEY_ID_1>',
  '<JOURNEY_ID_2>'
  -- ...
)
AND change_log->>'transactionType' = 'ADDED'
-- AND event_timing = '<ACTUAL|ESTIMATED>'           ← include only if not ALL
-- AND change_log->>'eventTrigger' = '<TRIGGER>'     ← include only if not ALL
ORDER BY unit_of_tracking, created_on;
```

**If the result hits the 100-row cap:** paginate with `OFFSET 100` until all rows are retrieved.

---

## Step 4 — Check VRDAN for Raw Payload Coverage

### Step 4a — Get job_ids from DAS

```sql
-- mcp__das-db__query
SELECT job_id, dp_name, identifiers::text AS container_ref,
       gathering_status, publishing_status, created_at
FROM das_request_store
WHERE identifiers::text LIKE '%<CONTAINER_1>%'
   OR identifiers::text LIKE '%<CONTAINER_2>%'
   -- ... one OR clause per container
ORDER BY created_at DESC;
```

Note: Only containers tracked via DAS **pull jobs** will appear here. Containers that receive data via the **Hook Service** (provider push/webhooks) will not have DAS records and therefore will not have VRDAN entries.

### Step 4b — Query VRDAN for raw payload summary

Use the `job_id`s from Step 4a.

```sql
-- mcp__vrdan-db__query
SELECT
  job_id,
  data_provider,
  COUNT(*)        AS total_versions,
  MIN(created_at) AS first_received,
  MAX(created_at) AS latest_received
FROM versioned_payload_data
WHERE job_id IN (
  '<JOB_ID_1>',
  '<JOB_ID_2>'
  -- ...
)
GROUP BY job_id, data_provider
ORDER BY job_id;
```

Cross-reference `job_id` back to container name using the DAS `identifiers` field from Step 4a.

---

## Output

### Table 1 — Actual Events per Container

| Container | Actual Event | Event Timestamp (UTC) | Added to System (UTC) |
|---|---|---|---|
| ... | ... | ... | ... |

Sorted by container, then `created_on` ascending (chronological event order).

### Table 2 — VRDAN Raw Payload Coverage

| Container | Job ID | Provider | Total Versions | First Received (UTC) | Latest Received (UTC) |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | ... |

Containers with no VRDAN row → data arrives via Hook Service, not DAS pull.

---

## Anomaly Checks

After presenting the tables, check and flag:

1. **Containers with no SIR journey** — not subscribed, cannot be traced.
2. **Containers with no GIS actual events** — subscription exists but no actuals received yet, or all events were rejected.
3. **Containers missing VESSEL_ARRIVAL / UNLOADED_FROM_VESSEL** — likely still in transit.
4. **Large backfill delays** — `added_to_system` is much later than `event_timestamp` (e.g., >24 hours). This indicates the provider reported the event late or data was backfilled in batch.
5. **Missing VESSEL_DEPARTURE on a leg** — gap between LOADED_ON_VESSEL and UNLOADED_FROM_VESSEL without a departure event; may indicate GIS rejection (IS_CONSISTENT = UNKNOWN) or missing provider data.
6. **Shared journey_id across containers** — multiple containers on the same journey (BL-level subscription); note which containers share a journey.
7. **No VRDAN coverage** — container is Hook-Service tracked; raw payload is not archived and must be investigated via Hook Service logs or DAS-Hook.

---

## Timestamp Interpretation Guide

| Column | Source field | Meaning |
|---|---|---|
| Event Timestamp | `change_log->>'timestamp'` (Unix epoch, float) | When the physical event occurred, as reported by the data provider |
| Added to System | `audit_trail.created_on` | When GIS wrote this event record — i.e., when the platform received and processed it through the pipeline (DUST → MP → MCE → DOS → GIS) |

A gap between the two timestamps represents **provider reporting latency + pipeline processing time**. A large gap (hours to days) usually indicates a provider backfill or delayed push.
