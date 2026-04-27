# Incident Root Cause Investigator

Master triage skill. Given an incident summary, verifies the DOS and DFS layers to determine whether the data platform is at fault, then classifies the issue and routes to the appropriate investigation sub-skill.

## Input

```
container: <CONTAINER_NUMBER>
subscription_id: <SUBSCRIPTION_ID>      (optional if journey_id provided)
journey_id: <JOURNEY_ID>               (optional if container + subscription_id provided)
expected: <what should have happened>
actual: <what actually happened>
```

$ARGUMENTS

---

## Phase 0 — DFS Guardrail Pre-check

**This is the first step, always.** If the guardrail is failing, no update reaches Consumer Platform regardless of what internal layers say — stop here and report without digging further.

Load `mcp__dfs-db__query` via ToolSearch.

### Step 0a — Resolve journey_ids to check

**If `journey_id` is provided:** use it directly — one journey to check.

**If `journey_id` is not provided:** load `mcp__dos-db__query` via ToolSearch and query:

```sql
-- mcp__dos-db__query
SELECT journey_id, unit_of_tracking, status, updated_on
FROM intelligent_journey
WHERE unit_of_tracking = '<CONTAINER>'
ORDER BY updated_on DESC;
```

Collect all returned `journey_id` values. Check DFS for each.

### Step 0b — Query DFS for each journey_id

Run both queries **in parallel** for each `journey_id`. Both tables upsert on re-processing, so `updated_at` is the timestamp of the **most recent** evaluation — not just whether a row exists.

```sql
-- Most recent passed evaluation
SELECT journey_id, unit_of_tracking, updated_at
FROM validation_passed_result
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>';

-- Most recent failed evaluation + reason
SELECT journey_id, unit_of_tracking, reason, updated_at
FROM validation_result
WHERE journey_id = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>';
```

### Step 0c — Determine current guardrail state by comparing timestamps

The **latest `updated_at` across both tables** is the ground truth for whether the most recent update passed or was dropped.

| Scenario | Current state |
|----------|--------------|
| Only `validation_passed_result` has a row | Guardrail **passing** — last update reached downstream |
| Only `validation_result` has a row | Guardrail **failing** — last update was dropped |
| Both rows exist, `validation_passed_result.updated_at` is **more recent** | Guardrail **passing** — last update reached downstream (earlier failure was resolved) |
| Both rows exist, `validation_result.updated_at` is **more recent** | Guardrail **failing** — last update was dropped; the pass record is stale |
| Neither table has a row | DFS has not yet processed this journey — check Kafka consumer lag on `intelligentJourneyTopic` |

### Step 0d — Act on current state

- **Guardrail passing** → proceed to Phase A.
- **Guardrail failing** → **STOP.** Output:
  - The `journey_id`, `reason` string, and `updated_at` from `validation_result`
  - Which of the 9 rules fired (matched from the reason string)
  - Verdict: *"DFS guardrail dropped the most recent update for this container (as of `<updated_at>`). No update will reach Consumer Platform until this is resolved. Investigate the data quality issue identified by the failing rule before proceeding to internal layers."*
- **Neither table has a row** → report Kafka consumer lag on `intelligentJourneyTopic` as the likely cause. Stop.

---

## Phase A — Bind Identifiers

Parse the input above. Extract `container`, `subscription_id`, `journey_id`, `expected`, `actual`.

If `journey_id` is already provided, skip to Phase B — no query needed.

If `journey_id` is missing, load `mcp__sir-db__query` via ToolSearch and resolve it:

```sql
SELECT journey_id, status, created_on
FROM subscription
WHERE subscription_id = '<SUBSCRIPTION_ID>'
  AND unit_of_tracking::text LIKE '%<CONTAINER>%'
ORDER BY created_on DESC LIMIT 1;
```

Note the subscription `status`. If `TRACKING_STOPPED`, include that as context in the final verdict.

---

## Phase B — Verify DOS Output Layer

Load `mcp__dos-db__query` via ToolSearch. Run both queries:

```sql
-- Final enriched journey produced by data platform
SELECT id, journey_id, status, journey_type, transport_legs::text, created_on, updated_on
FROM intelligent_journey
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY updated_on DESC LIMIT 1;
```

```sql
-- Failed events DLQ — catches processing failures before DFS
SELECT id, journey_id, event_type, failure_reason, retry_count, status, created_on
FROM milestone_failed_event
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY created_on DESC LIMIT 10;
```

Compare `transport_legs` and milestone data in the result against the **expected behavior** from the incident input.

---

## Phase C — Verdict Gate

| Condition | Verdict |
|-----------|---------|
| DOS output matches expected | **Data platform is correct.** Output the evidence (query results). Declare no platform fault. Recommend upstream investigation (SCP / CP / provider). |
| DOS output does NOT match expected | **Platform issue at DOS or upstream.** Proceed to Phase D to classify and route. |

---

## Phase D — Issue Classification and Sub-Skill Routing

Analyse the incident `actual` description and the DOS findings. Match against the table below and invoke the corresponding sub-skill.

| Keywords / Symptom | Sub-Skill |
|-------------------|-----------|
| "transport plan", "legs", "route", "SCP", "booking", "reconciliation", "missing leg", "extra leg", "wrong port" | `/transport-plan-investigation` |
| "vessel name", "voyage number", "wrong vessel", "wrong voyage", "vessel voyage incorrect" | `/vessel-event-investigation` with `problem: vessel_voyage` |
| "ETA", "ETD", "ATA", "ATD", "estimated arrival", "actual arrival", "estimated departure", "actual departure", "timestamp wrong" — leg sequence, vessel, and voyage all known | `/vessel-event-investigation` with `problem: timestamp` |
| "ETD stale", "ATD not received", "estimated timestamp not updating", "ETD in the past", or timestamp issue where leg/vessel/voyage unknown, or bulk containers | `/vessel-timestamp-debug` |
| "milestone", "missing event", "triangulation", "wrong location" | `/incorrect-milestone-tracer` with `incorrect_trigger`, `event_timing`, `location_code` |
| "wrong event type", "incorrect trigger", "vessel arrival at inland", "truck arrival mapped as vessel" | `/incorrect-milestone-tracer` with `incorrect_trigger`, `event_timing`, `location_code` |
| "stitching", "container merge", "split shipment", "duplicate journey" | *Stitching Investigation* — not yet implemented; check SIR subscription split events |
| Unclear / spans multiple categories | Ask: "Is this a transport plan, vessel event, milestone, or stitching issue?" then route accordingly |

Output the classification verdict and the sub-skill invocation with the resolved identifiers passed through:

```
/transport-plan-investigation
  journey_id: <JOURNEY_ID>
  container: <CONTAINER>
  job_id: <JOB_ID if known>
```

---

## Extensibility

To add a new investigation sub-skill:
1. Create `.claude/commands/<new-skill-name>.md`
2. Add one row to the Phase D routing table above
3. Add one row to the §0 Skills table in root `CLAUDE.md`

No other changes to this file are needed.
