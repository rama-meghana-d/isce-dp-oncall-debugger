# ISCE On-Call — Master Runbook

> **Start from the symptom table (§5). Each row names the first service to check and links to the sub-service CLAUDE.md for deep detail. MCP tools must be loaded via `ToolSearch` before first use.**

---

## 0. Skills — Slash Commands

Start every unfamiliar incident with `/incident-root-cause-investigator`. Use the sub-skills directly only when you already know the fault category.

---

### `/incident-root-cause-investigator`

**Use when:** You have an incident report and don't yet know where the fault is.

**Required input:** `container`, `journey_id` (or `subscription_id` to resolve it), `expected`, `actual`

**Exact steps:**

**Phase A — Bind identifiers**
- If `journey_id` is provided → use it directly, skip DB query.
- If `journey_id` is missing → query SIR `subscription` WHERE `subscription_id = X AND unit_of_tracking LIKE '%container%'` → pick the most recent row → extract `journey_id`.
- Record subscription `status`. If `TRACKING_STOPPED`, carry that as context into the final verdict.

**Phase B — Check DOS output layer**
- Query DOS `intelligent_journey` WHERE `journey_id` → read `status`, `journey_type`, `transport_legs` JSON.
- Query DOS `milestone_failed_event` WHERE `journey_id` → check for any events that failed processing and landed in the DLQ.
- Manually compare `transport_legs` content and milestone records against the `expected` description from your input. Decide: does DOS output match what should have happened?

**Phase C — Check DFS guardrail layer**
- Call `GET /guardRail/journeyId/{journey_id}/unitOfTracking/{container}`.
- The response maps to one of three states:
  - **`validation_passed_result` row exists** → all 9 rules passed; message was published to `validatedIntelligentJourneyTopic`.
  - **`validation_result` row exists** → a rule failed; message was dropped. Read `reason` to see which of the 9 rules fired and the exact reason string (e.g. "Latest ACTUAL VESSEL_DEPARTURE is after(relaxed by 2 days) earliest ACTUAL VESSEL_ARRIVAL, Leg info: CNSHA DEHAM").
  - **Neither table has a row** → DFS has not processed this journey yet; check Kafka consumer lag on `intelligentJourneyTopic`.

**Phase D — Verdict**
- **DOS output matches expected AND DFS `validation_passed_result` exists** → Data platform is correct. Output the `intelligent_journey` row and DFS result as evidence. Declare no platform fault. Recommend investigation at CP / SCP / provider layer.
- **DOS output does NOT match expected** → Platform issue at DOS or upstream. Proceed to Phase E.
- **DFS `validation_result` exists (rule fired, message dropped)** → Platform dropped the message. Determine if the rule fired correctly (legitimate data quality issue) or is a false positive. Either way this is a platform finding. Proceed to Phase E.

**Phase E — Classify and route to sub-skill**
- Read the incident `actual` description and the DOS/DFS findings. Match keywords to route:
  - *"transport plan / legs / route / missing leg / extra leg / wrong port / booking / reconciliation"* → invoke `/transport-plan-investigation` with `journey_id`, `container`, `job_id` (if known)
  - *"vessel name / voyage number / wrong vessel / wrong voyage"* → invoke `/vessel-event-investigation` with `problem: vessel_voyage`
  - *"ETA / ETD / ATA / ATD / timestamp wrong / arrival date / departure date"* → invoke `/vessel-event-investigation` with `problem: timestamp`
  - *"milestone / missing event / triangulation / wrong location"* → Milestone investigation (not yet a skill — follow §4: DOS → GIS → MCE → MP → DUST chain)
  - *"stitching / duplicate journey / container merge / split shipment"* → Stitching investigation (not yet a skill — check SIR subscription split events)
- Pass all resolved identifiers (`journey_id`, `container`, `subscription_id`, `job_id` if known) into the sub-skill invocation.

**Output:** Verdict (platform fault / no fault) with evidence query rows. If fault: sub-skill invocation command with all identifiers pre-filled.

---

### `/transport-plan-investigation`

**Use when:** You know the issue is a wrong transport plan — incorrect legs, wrong port of loading/discharge, missing or extra leg, wrong vessel on a leg.

**Required input:** `journey_id`, `container`. Optional: `subscription_id`, `job_id`.

**What it does — in order:**
1. **Step 1 — Resolve job_id:** If not provided, queries SIR `subscription_job_tracking` to find the most recent active job for this container.
2. **Step 2 — Inspect TPP state:** Three queries on `mcp__transport-plan-db__query`:
   - `journey` table: fetches **both** the booking-level row (`unit_of_tracking = NULL`, written by CP from SCP booking) and the execution-level row (`unit_of_tracking = container`, the live transport plan that DPs and CP execution updates overwrite).
   - `journey_updates` table: fetches the last 5 DP-originated transport plan updates only (CP/SCP updates never appear here). The `data_provider` column identifies which provider sent each update.
   - `journey_reconciliation_updates` table: fetches the last 5 Route Reconciliation outputs — the merged result of DP transport plan + TPG milestone-inferred legs, with `suggested_journey_update` and `reconcile_actions` (VESSEL_UPDATE, TRANSPORT_MODE_UPDATE, JOURNEY_UPDATE, BROKEN_JOURNEY_REVIEW, etc.).
3. **Step 3 — Raw DP payload:** Queries DUST `transportplanpipeline` for the raw provider payload before TPP processed it.
4. **Step 4 — 3-node decision tree:**
   - **Node 1:** Is the execution-level `journey` row correct? If yes → transport plan layer is clean, traces downstream (DOS → GIS). If no → Node 2.
   - **Node 2:** Is `suggested_journey_update` from Route Reconciliation correct? If correct + no `BROKEN_JOURNEY_REVIEW` + journey wrong → **CP did not provide correct execution transport plan** (CP team). If correct + `BROKEN_JOURNEY_REVIEW` → **broken journey** (DP tracking and SCP booking describe different routes — SCP/CP team). If wrong → Node 3.
   - **Node 3:** Are the `journey_updates` legs from the named `data_provider` correct? If wrong → **Data provider sent incorrect transport plan** (DP team). If correct but RR output wrong → **Route Reconciliation merge bug** (DI engineering).

**Output:** Structured RCA report: ROOT CAUSE / CULPRIT / EVIDENCE (query rows) / RECOMMENDED ACTION.

---

### `/vessel-event-investigation`

**Use when:** A specific vessel event on an ocean leg is wrong — either the vessel name / voyage number is incorrect, or an ETA / ATA / ETD / ATD timestamp is wrong at a specific port.

**Required input:** `journey_id`, `container`, `leg_sequence`, `event_type` (ETA/ATA/ETD/ATD), `location_code`, `problem` (`vessel_voyage` or `timestamp`).
- For `vessel_voyage`: also `expected_vessel_name`, `expected_voyage_number`.
- For `timestamp`: also `vessel_name`, `voyage_number`, `expected_date` (YYYY-MM-DD).

**Path A — vessel_voyage:**
1. Queries DOS `intelligent_journey` JSON → navigates to the specified leg + event trigger/timing + location → extracts vessel name and voyage number from `eventConnections`.
2. **Discrepancy gate:** If IJ values match expected → checks Shipment Visibility `event_connection` only. If IJ values differ → traces back to Milestone Processor `enriched_data` → DUST `milestonepipeline` raw payload → identifies DP via DAS.
3. **Culprit:** DP sent wrong vessel/voyage | MP enrichment corrupted the value | SV `event_connection` out of sync with IJ | CP layer.

**Path B — timestamp:**
1. Resolves SV internal `journey_id` from `shipment_journey_id + container` in `mcp__shipment-visibility-db__query`.
2. **If journey found in SV:** Queries `vessel_voyage_port_call_timestamp` for both `timestamp` (committed) and `pending_timestamp` (uncommitted update).
   - `sv_timestamp` date matches `expected_date` → DFS guardrail check via REST. If DFS passing → **no platform fault** (CP/consumer). If DFS dropped → **DFS guardrail failure**.
   - `sv_timestamp` wrong but `pending_timestamp` matches `expected_date` → **manual port call update required via SV UI** (pending timestamp needs promotion).
   - Both wrong or null → checks IJ (Step 3).
3. **If journey not in SV, or SV wrong:** Queries DOS `intelligent_journey` for vessel, voyage, and timestamp at the specified event. If IJ correct → **SV sync issue** (DI/SV engineering). If IJ wrong → queries MP `enriched_data` to identify the data provider that sent the wrong value.
4. **Output:** RCA report with values at every queried layer (IJ, SV, MP), each annotated MATCH/MISMATCH vs expected, plus culprit and recommended action.

---

### `/milestone-timestamp-audit`

**Use when:** You need to audit when actual events occurred (per provider) vs when the ISCE platform ingested them — for one or more containers. Useful for SLA checks, backfill detection, or investigating late data.

**Required input:** `containers` (comma-separated list). Optional: `event_timing` (ALL/ACTUAL/ESTIMATED), `event_trigger` (ALL or a specific trigger name e.g. `VESSEL_ARRIVAL`).

**What it does — in order:**
1. **Step 1 — Resolve journey_ids:** Queries SIR `subscription` for all containers in one query. Notes containers with no SIR subscription (cannot be traced).
2. **Step 2 — Query GIS audit trail:** Queries `audit_trail` for all ADDED events (not rejected). Returns `event_timestamp` (when the physical event occurred, from provider data — Unix epoch float cast to timestamp) and `added_to_system` (when GIS wrote the record — i.e., when the platform processed it through DUST → MP → MCE → DOS → GIS). Paginates if result exceeds 100 rows.
3. **Step 3 — VRDAN coverage:** Queries DAS `das_request_store` for job_ids per container, then queries VRDAN `versioned_payload_data` for raw payload summary (provider, total versions, first/latest received). Note: containers tracked via Hook Service (provider push) will have no DAS/VRDAN records.
4. **Anomaly detection:** Automatically flags — containers with no SIR journey, containers with no GIS actuals, missing VESSEL_ARRIVAL or UNLOADED_FROM_VESSEL, backfill delays >24h (`event_timestamp` vs `added_to_system` gap), missing VESSEL_DEPARTURE (possible GIS IS_CONSISTENT=UNKNOWN rejection), shared journey_id across containers, no VRDAN coverage (Hook Service tracked).

**Output:** Table 1 — all matching events per container (event trigger, event timestamp, added-to-system timestamp). Table 2 — VRDAN raw payload coverage per container. Flagged anomaly list.

---

**Adding a new sub-skill:** Create `.claude/commands/<name>.md` → add one entry to this section → add one row to the Phase E routing table in `incident-root-cause-investigator.md`.

---

## 1. Platform Overview

| Repo | Role | CLAUDE.md |
|---|---|---|
| `isce-data-sourcing` | Ingestion: raw provider data → typed events | *(monorepo; see sub-service files below)* |
| `isce-data-intelligence` | Processing: typed events → intelligent journeys | [isce-data-intelligence/CLAUDE.md](isce-data-intelligence/CLAUDE.md) |
| `themis-visibility-reference-data-locations` | Location ref data (UNLOC/RKST/GeoID) | [themis-visibility-reference-data-locations/CLAUDE.md](themis-visibility-reference-data-locations/CLAUDE.md) |

**Kafka topic chain (DS → DI):**
```
inputrequest → dpupdatehandler → dust.topic
→ milestone.topic → MP → MCE → DOS → GIS → intelligentJourneyTopic → DFS → Consumer Platform
→ transportplan.topic → TPP → Route Reconciliation → DOS
→ position.topic → Position Processor
```

---

## 2. Key Identifiers

| Identifier | Description | How to get it |
|---|---|---|
| `journey_id` | Primary key across DI services | SIR: `subscription.journey_id` WHERE `unit_of_tracking` |
| `unit_of_tracking` | Container # or BL # | Given by reporter |
| `job_id` | Links DS job → DI processing | DAS: `das_request_store.job_id` |
| `pipeline_id` | DUST pipeline key | `{job_id}_{container}` |
| `isce_event_id` | MCE internal event UUID | MCE: `isce_triangulation_audit` |

**Step 0 — Resolve journey_id:**
```sql
-- mcp__sir-db__query
SELECT journey_id, unit_of_tracking::text, subscription_id, status, created_on
FROM subscription
WHERE unit_of_tracking::text LIKE '%<CONTAINER_OR_BL>%'
ORDER BY created_on DESC;
```

**Step 0b — Resolve job_id:**
```sql
-- mcp__das-db__query
SELECT job_id, dp_name, gathering_status, publishing_status, tracking_status, created_at
FROM das_request_store
WHERE identifiers::text LIKE '%<CONTAINER_OR_BL>%'
ORDER BY created_at DESC;
```

---

## 3. MCP Tools Quick Reference

All tools are read-only (BEGIN TRANSACTION READ ONLY). Use `ToolSearch select:<name>` before first use.

| MCP Tool | Service | Key Tables |
|---|---|---|
| `mcp__das-db__query` | DAS-KC / DAS-Jobs | `das_request_store` |
| `mcp__dpcs-db__query` | DPCS | `carrier`, `data_provider`, `data_provider_carrier` |
| `mcp__dust-db__query` | DUST | `milestonepipeline`, `positionpipeline`, `transportplanpipeline` |
| `mcp__vrdan-db__query` | VRDAN | `versioned_payload_data` |
| `mcp__sir-db__query` | SIR | `subscription`, `subscription_job_tracking` |
| `mcp__milestone-processor-db__query` | MP | `milestone`, `subscription_milestone_metadata` |
| `mcp__mce-db__query` | MCE | `isce_triangulation_audit`, `isce_triangulation_shadow` |
| `mcp__dos-db__query` | DOS | `intelligent_journey`, `shipment_journey`, `milestone_failed_event` |
| `mcp__gis-db__query` | GIS | `audit_trail`, `shipment_journey_transaction` |
| `mcp__transport-plan-db__query` | TPP | `journey`, `journey_reconciliation_updates`, `journey_updates` |
| `mcp__shipment-visibility-db__query` | Shipment Visibility | `container`, `journey`, `transport_leg` |
| `mcp__reference-data-locations-db__query` | Reference Data | `unloc_details`, `map_rkst_unloc`, `isce_alt_codes` |
| `mcp__dps-db__query` | DPS | `rules_store` |
| `mcp__position-processor-db__query` | Position Processor | `position_detail` |

---

## 4. Symptom → First Steps

| Symptom | Start at | First query target | Sub-service runbook |
|---|---|---|---|
| Milestone missing from intelligent journey | DOS → GIS → MCE → MP → DUST → VRDAN | `dos-db: intelligent_journey` | [dos](isce-data-intelligence/dos/CLAUDE.md), [gis](isce-data-intelligence/gis/CLAUDE.md), [mce](isce-data-intelligence/mce/CLAUDE.md), [milestone-processor](isce-data-intelligence/milestone-processor/CLAUDE.md) |
| Milestone rejected by GIS | GIS audit_trail | `gis-db: audit_trail` WHERE `transactionType=REJECTED` | [gis](isce-data-intelligence/gis/CLAUDE.md) — IS_CONSISTENT=UNKNOWN means TP has no vessel data |
| Transport plan wrong / legs incorrect | TPP → Route Reconciliation → DOS | `transport-plan-db: journey` + `journey_reconciliation_updates` | [tpp](isce-data-intelligence/tpp/CLAUDE.md), [route-reconciliation](isce-data-intelligence/route-reconciliation/CLAUDE.md) |
| Journey not created | SIR → MP → DARC | `sir-db: subscription` | [sir](isce-data-intelligence/sir/CLAUDE.md), [darc](isce-data-sourcing/darc/CLAUDE.md) |
| No raw data from provider | VRDAN → DAS → DPCS | `das-db: das_request_store` gathering/publishing_status | [das-jobs](isce-data-sourcing/das-jobs/CLAUDE.md), [vrdan](isce-data-sourcing/vrdan/CLAUDE.md), [dpcs](isce-data-sourcing/dpcs/CLAUDE.md) |
| Wrong location / UNLOC on event | GIS → Reference Data | `gis-db: audit_trail` change_log.location | [reference-data-locations](themis-visibility-reference-data-locations/CLAUDE.md) |
| Event stuck in pipeline (Kafka lag) | RETinA → stuck service pod | Check consumer group lag, then pod readiness | [dust](isce-data-sourcing/dust/CLAUDE.md) (dedup), [dos](isce-data-intelligence/dos/CLAUDE.md) (retry queue) |
| Journey stuck as TRACKING_STOPPED | DOS → SIR → DAS | `dos-db: intelligent_journey` status + `sir-db: subscription` reason | [tracking-stopped-scheduler](isce-data-intelligence/tracking-stopped-scheduler/CLAUDE.md) |
| Duplicate milestones | DUST → MP → MCE | `dust-db: milestonepipeline` duplicate entries | [dust](isce-data-sourcing/dust/CLAUDE.md), [mce](isce-data-intelligence/mce/CLAUDE.md) |
| Pod crash / restart loop | Pod describe → Liquibase lock | `SELECT locked FROM databasechangeloglock` on affected DB | §5 below |

---

## 5. Key Reference Facts

**DAS job status lifecycle:**
- `GATHERING_PENDING` → `GATHERING_IN_PROGRESS` → `GATHERING_COMPLETED` (publishing: `PUBLISHED` / `NO_DATA_RECEIVED` / `NOT_PUBLISHED`) or `GATHERING_FAILED`
- Jobs expire after 30 days → `tracking_status = TRACKING_STOPPED`

**MCE provider precedence (highest → lowest):**
`Manual > MaerskTnT > P44_NEW > VIZION > GNOSIS > Portcast > (others)`
- Check `isce_triangulation_audit.applied_rules` for `Global Provider Precedence`
- Feature flag `IN_HOUSE_MILESTONE_TRIANGULATION_ENABLED` — if false, MCE delegates to GDA

**GIS IS_CONSISTENT:**
- `YES` / `NO` — vessel found in TP; event ADDED regardless
- `UNKNOWN` — **TP leg has no vessel data** → ACTUAL events REJECTED
- GIS reads vessel from `leg.vessel` or `leg.transportActivity.vessel` — NOT from `leg.references[]`

**Liquibase lock (pod startup failure):**
```sql
SELECT locked, lockedby, lockgranted FROM databasechangeloglock;
-- If locked=true and no migration running: UPDATE SET LOCKED=false WHERE ID=1;
```

---

## 6. Environments, Monitoring & Contacts

| Env | Data Sourcing NS | Data Intelligence NS |
|---|---|---|
| dev | `isce-data-sourcing-dev` | `isce-data-intelligence-dev` |
| preprod | `isce-data-sourcing-preprod` | `isce-data-intelligence-preprod` |
| prod | `isce-data-sourcing-prod` | `isce-data-intelligence-prod` |

| Tool | Purpose |
|---|---|
| Grafana `http://grafana.maersk.io/dashboards?tag=isce` | All service dashboards |
| Hedwig DS `https://hedwig.maersk.io/teams/isce-data-sourcing` | DS alerts |
| Hedwig DI `https://hedwig.maersk.io/teams/isce-data-intelligence` | DI alerts |
| RETinA (internal) | Kafka consumer group lag |
| Zipkin (`ZIPKIN_ENDPOINT` per service) | Distributed tracing via `traceId` |

**Contacts:** DS: isce-data-sourcing@maersk.com · DI: isce-data-intelligence@maersk.com · Jira: https://maersk-tools.atlassian.net/jira/software/c/projects/ISCE/boards/13885

---

## 7. Sub-Service CLAUDE.md Index

| Service | Role | CLAUDE.md |
|---|---|---|
| DARC | Dedup + gate before DAS | [darc/CLAUDE.md](isce-data-sourcing/darc/CLAUDE.md) |
| DAS-KC | Kafka consumer → das_request_store | [das-kafka-consumers/CLAUDE.md](isce-data-sourcing/das-kafka-consumers/CLAUDE.md) |
| DAS-Jobs | K8s jobs → provider API calls | [das-jobs/CLAUDE.md](isce-data-sourcing/das-jobs/CLAUDE.md) |
| Hook | REST ingestion + Redis dedup | [das-hook-service/CLAUDE.md](isce-data-sourcing/das-hook-service/CLAUDE.md) |
| DPRC | subscription→job mapping | [dp-response-consumer/CLAUDE.md](isce-data-sourcing/dp-response-consumer/CLAUDE.md) |
| DUST | Transform + dedup → 3 typed pipelines | [dust/CLAUDE.md](isce-data-sourcing/dust/CLAUDE.md) |
| DPCS | Provider configs + carrier mappings | [dpcs/CLAUDE.md](isce-data-sourcing/dpcs/CLAUDE.md) |
| VRDAN | Raw payload archive | [vrdan/CLAUDE.md](isce-data-sourcing/vrdan/CLAUDE.md) |
| SIR | Subscription registry | [sir/CLAUDE.md](isce-data-intelligence/sir/CLAUDE.md) |
| MP | Milestone enrichment chain | [milestone-processor/CLAUDE.md](isce-data-intelligence/milestone-processor/CLAUDE.md) |
| MCE | Clustering + triangulation | [mce/CLAUDE.md](isce-data-intelligence/mce/CLAUDE.md) |
| DOS | Journey orchestrator + fan-out | [dos/CLAUDE.md](isce-data-intelligence/dos/CLAUDE.md) |
| GIS | GDA triangulation + audit_trail | [gis/CLAUDE.md](isce-data-intelligence/gis/CLAUDE.md) |
| TPP | Transport plan ingestion + reconciliation | [tpp/CLAUDE.md](isce-data-intelligence/tpp/CLAUDE.md) |
| TPG | Inferred TP from milestones | [tpg/CLAUDE.md](isce-data-intelligence/tpg/CLAUDE.md) |
| Route Reconciliation | Leg mode + vessel updates | [route-reconciliation/CLAUDE.md](isce-data-intelligence/route-reconciliation/CLAUDE.md) |
| DIS | Themis visibility event splitter | [dis/CLAUDE.md](isce-data-intelligence/dis/CLAUDE.md) |
| DFS | Journey fidelity validation | [dfs/CLAUDE.md](isce-data-intelligence/dfs/CLAUDE.md) |
| Position Processor | Vessel position aggregation | [position-processor/CLAUDE.md](isce-data-intelligence/position-processor/CLAUDE.md) |
| Shipment Visibility | Vessel/port/leg reference data | [shipment-visibility/CLAUDE.md](isce-data-intelligence/shipment-visibility/CLAUDE.md) |
| DPS | Provider selection rules | [dps/CLAUDE.md](isce-data-intelligence/dps/CLAUDE.md) |
| DQS | Consumer Platform query facade | [dqs/CLAUDE.md](isce-data-intelligence/dqs/CLAUDE.md) |
| Tracking Stopped Scheduler | Aging conditions → TRACKING_STOPPED | [tracking-stopped-scheduler/CLAUDE.md](isce-data-intelligence/tracking-stopped-scheduler/CLAUDE.md) |
| Data Enrichment Service | Terminal/port mapping from GDA | [data-enrichment-service/CLAUDE.md](isce-data-intelligence/data-enrichment-service/CLAUDE.md) |
| Reference Data Locations | UNLOC/RKST/GeoID lookup | [themis-visibility-reference-data-locations/CLAUDE.md](themis-visibility-reference-data-locations/CLAUDE.md) |
