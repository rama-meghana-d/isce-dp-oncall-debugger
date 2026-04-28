# ISCE On-Call — Master Runbook

> **Start from the symptom table (§5). Each row names the first service to check and links to the sub-service CLAUDE.md for deep detail. MCP tools must be loaded via `ToolSearch` before first use.**

---

## 0. Skills — Slash Commands

**Start every new ticket with `/triage-ticket`.** It reads the Jira description and comments, extracts the problem statement, and either routes to `/incident-root-cause-investigator` or sends a clarification comment back if the ticket is incomplete.

Use the sub-skills directly only when you already have a clear problem statement and know the fault category.

When the investigation reaches the data-provider layer (Phase D routes to a DP issue, or you want to confirm whether the problem originates at the provider vs. our transformation code), use `/dp-investigation`.

---

### `/triage-ticket`

**Use when:** You have a Jira ticket key and want to determine whether it's ready for data platform investigation.

**Required input:** `ticket: <JIRA_KEY>`  e.g. `ticket: ISCE-14280`

**Exact steps:**
1. Fetch the ticket (description, status, assignee) via Atlassian MCP.
2. Fetch the last 5 comments ordered newest-first. Comments may refine or correct the description — always prefer the most recent information.
3. Extract `container`, `expected`, `actual`, `subscription_id`, `journey_id` from description + comments combined. Mark each FOUND / MISSING.
4. Classify issue category: `TRANSPORT_PLAN | VESSEL_VOYAGE | TIMESTAMP | MILESTONE | STITCHING | UNCLEAR`.
5. **If all three required fields found and category is not UNCLEAR** → invoke `/incident-root-cause-investigator` with the resolved identifiers.
6. **If any required field is missing or category is UNCLEAR** → post a clarification comment on the Jira ticket listing exactly what's missing, then reassign to reporter.

**Output:** Triage summary table (field extraction) + routing decision or confirmation that clarification comment was posted.

---

### `/incident-root-cause-investigator`

**Use when:** You have an incident report and don't yet know where the fault is.

**Required input:** `container`, `journey_id` (or `subscription_id` to resolve it), `expected`, `actual`

**Exact steps:**

**Phase 0 — DFS guardrail pre-check (always first)**
- If `journey_id` provided → query `mcp__dfs-db__query` `validation_passed_result` and `validation_result` for that journey + container.
- If `journey_id` not provided → query DOS `intelligent_journey` WHERE `unit_of_tracking = container` → collect all `journey_id` values → check DFS for each.
- Fetch the latest row from both `validation_passed_result` and `validation_result` for each journey + container. Compare `updated_at` — the **most recent timestamp across both tables** is the current state.
- **`validation_result.updated_at` is more recent (or only that row exists)** → STOP. Guardrail is dropping the latest update. Report the failing rule, `reason`, and `updated_at`. No update reaches Consumer Platform — do not proceed to internal layers.
- **Neither table has a row** → DFS not yet processed; check Kafka consumer lag on `intelligentJourneyTopic`. Stop.
- **`validation_passed_result.updated_at` is more recent (or only that row exists)** → guardrail passing; latest update reached downstream. Proceed to Phase A.

**Phase A — Bind identifiers**
- If `journey_id` is provided → use it directly, skip DB query.
- If `journey_id` is missing → query SIR `subscription` WHERE `subscription_id = X AND unit_of_tracking LIKE '%container%'` → pick the most recent row → extract `journey_id`.
- Record subscription `status`. If `TRACKING_STOPPED`, carry that as context into the final verdict.

**Phase B — Check DOS output layer**
- Query DOS `intelligent_journey` WHERE `journey_id` → read `status`, `journey_type`, `transport_legs` JSON.
- Query DOS `milestone_failed_event` WHERE `journey_id` → check for any events that failed processing and landed in the DLQ.
- Manually compare `transport_legs` content and milestone records against the `expected` description from your input. Decide: does DOS output match what should have happened?

**Phase C — Verdict**
- **DOS output matches expected** → Data platform is correct. Output the `intelligent_journey` row as evidence. Declare no platform fault. Recommend investigation at CP / SCP / provider layer.
- **DOS output does NOT match expected** → Platform issue at DOS or upstream. Proceed to Phase D.

**Phase D — Classify and route to sub-skill**
- Read the incident `actual` description and the DOS/DFS findings. Match keywords to route:
  - *"transport plan / legs / route / missing leg / extra leg / wrong port / booking / reconciliation"* → invoke `/transport-plan-investigation` with `journey_id`, `container`, `job_id` (if known)
  - *"vessel name / voyage number / wrong vessel / wrong voyage"* → invoke `/vessel-event-investigation` with `problem: vessel_voyage`
  - *"ETA / ETD / ATA / ATD / timestamp wrong / arrival date / departure date"* — choose based on available detail:
    - Leg sequence, vessel name, voyage number, and location are all known → invoke `/vessel-event-investigation` with `problem: timestamp`
    - ETD stale / ATD not received / estimated timestamp not updating / bulk containers / leg/vessel unknown → invoke `/vessel-timestamp-debug`
  - *"milestone / missing event / triangulation / wrong location / wrong event type / incorrect trigger / vessel arrival at inland"* → invoke `/incorrect-milestone-tracer` with `incorrect_trigger`, `event_timing`, `location_code`
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

---

### `/vessel-timestamp-debug`

**Use when:** An ETD is stale (in the past but not updated), ATD has not been received, or estimated/actual timestamps are missing or incorrect and you do not yet know the specific leg, vessel name, or voyage number. Also use for bulk container checks.

**Required input:** `container`. Optional: `journey_id`, `location_code`, `expected_timestamp`, `event_type` (ETD/ATD/ETA/ATA/ALL).

**What it does — in order:**
1. **Step 1 — Resolve journey_id** from SIR if not provided.
2. **Step 2 — Check SV port call timestamps** — fetches all committed (`timestamp`) and pending (`pending_timestamp`) values across all legs for the journey.
3. **Step 3 — Evaluate SV state:**
   - **Correct** → DFS guardrail check → no platform fault, or DFS dropped the message.
   - **Pending only** → pending timestamp exists but hasn't been promoted → manual SV UI update required.
   - **Missing/wrong** → traces DOS IJ → Milestone Processor `enriched_data` → DUST `milestonepipeline` → identifies whether provider hasn't sent the event, sent the wrong value, or whether the issue is in an intermediate processing layer.
4. **Output:** Layer-by-layer RCA report with culprit and recommended action.

---

### `/incorrect-milestone-tracer`

**Use when:** A specific milestone event has the wrong `eventTrigger` or `eventTiming` (e.g. `VESSEL_ARRIVAL` appearing at an inland location that should be `TRUCK_ARRIVAL`). You know the incorrect trigger, timing, and UNLOC.

**Required input:** `container`, `incorrect_trigger`, `event_timing`, `location_code`. Optional: `journey_id`.

**What it does — in order:**
1. **Step 1** — Resolves `journey_id` from SIR if not provided.
2. **Step 2** — Checks DOS `intelligent_journey`: searches the IJ JSON for the incorrect trigger+timing+location using a compact Python parse script. If absent → GIS/MCE rejection upstream.
3. **Step 3** — Checks MCE `isce_triangulation_audit` for the same event. If absent → GIS rejected it. If trigger differs from IJ → MCE changed the type (MCE bug).
4. **Step 4** — Checks MP `milestone` for the same event. If trigger differs → MP enrichment changed the type (MP bug).
5. **Step 5** — Checks DUST `milestonepipeline` for the container. Extracts `job_id` and `data_provider`. If wrong trigger is present in DUST output → root cause is in DAS transformation layer. If DUST was correct → divergence is in MP/MCE.
6. **Step 6 (interactive)** — Reports `job_id` + `data_provider`, asks user to supply the raw VRDAN payload, reads the DAS transformer for that provider, cross-references the payload field to the static mapping, and declares: **DI Engineering transformation bug** or **data provider data quality issue**.

**Output:** Layer-by-layer divergence trace with the exact layer where the wrong trigger was introduced, culprit, and recommended action.

---

---

### `/dp-investigation`

**Use when:** You want to determine whether any issue (missing event, wrong timestamp, wrong vessel, wrong transport plan, extra/missing leg, etc.) originates at the **data provider** or inside **DAS-Jobs transformation**. Requires no upfront knowledge of which provider or what type of issue.

**Required input:** `container`, `journey_id`

**Exact steps:**
1. Queries SIR with a single JOIN: `subscription × data_aggregator_request_mapping` → returns all job IDs + data provider names for the container (no DAS query needed).
2. Port-forwards `deployment/dp-response-consumer 8085:8080 -n isce-data-sourcing-prod`.
3. For each job, fetches raw blob via `GET http://localhost:8085/data-provider/{dpId}/job/{jobId}/data/{container}`.
4. Dispatches to per-provider child skill (`/dp-vizion`, `/dp-gnosis`, `/dp-p44`, `/dp-maersktnt`, `/dp-inttra`, `/dp-pca`, `/dp-seeburger`, `/dp-portcast`, `/dp-coneksion`, `/dp-subscriptionless`, `/dp-bluebox`).
5. Each child skill produces a side-by-side table: raw field → eventTrigger / eventTiming → kept or dropped (with reason).
6. Synthesizes a consolidated verdict: DATA PROVIDER / DAS TRANSFORMATION / DOWNSTREAM.

**Output:** Per-provider raw↔transformed table + final layer verdict.

---

### `/milestone-clustering-inspector`

**Use when:** You want to see the cluster breakdown for a specific journey + container — which events are grouped together, the cluster timestamp, cluster location (with alternative codes), and which data providers contributed each event.

**Required input:** `journey_id`, `unit_of_tracking`

**What it does:** Port-forwards to `milestone-processor` in prod, calls `GET /journey/{journeyId}/unitOfTracking/{unitOfTracking}` with `api-version=1`, and renders a per-cluster table showing constituent events with data provider name, individual timestamp, and location code.

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
-- mcp__isce-di-db__query  db: sir
SELECT journey_id, unit_of_tracking::text, subscription_id, status, created_on
FROM subscription
WHERE unit_of_tracking::text LIKE '%<CONTAINER_OR_BL>%'
ORDER BY created_on DESC;
```

**Step 0b — Resolve job_id:**
```sql
-- mcp__isce-ds-db__query  db: das
SELECT job_id, dp_name, gathering_status, publishing_status, tracking_status, created_at
FROM das_request_store
WHERE identifiers::text LIKE '%<CONTAINER_OR_BL>%'
ORDER BY created_at DESC;
```

---

## 3. MCP Tools Quick Reference

All tools are read-only (BEGIN TRANSACTION READ ONLY). Use `ToolSearch select:<name>` before first use.

Two MCP servers, each exposing a single `query({ db, sql })` tool. Pass `db` as the short name from the table below.

| MCP Tool | `db` value | Service | Key Tables |
|---|---|---|---|
| `mcp__isce-di-db__query` | `dos` | DOS | `intelligent_journey`, `shipment_journey`, `milestone_failed_event` |
| `mcp__isce-di-db__query` | `sir` | SIR | `subscription`, `subscription_job_tracking` |
| `mcp__isce-di-db__query` | `mce` | MCE | `isce_triangulation_audit`, `isce_triangulation_shadow` |
| `mcp__isce-di-db__query` | `milestone-processor` | MP | `milestone`, `subscription_milestone_metadata` |
| `mcp__isce-di-db__query` | `gis` | GIS | `audit_trail`, `shipment_journey_transaction` |
| `mcp__isce-di-db__query` | `tpp` | TPP | `journey`, `journey_reconciliation_updates`, `journey_updates` |
| `mcp__isce-di-db__query` | `shipment-visibility` | Shipment Visibility | `container`, `journey`, `transport_leg` |
| `mcp__isce-di-db__query` | `dps` | DPS | `rules_store` |
| `mcp__dfs-db__query` | — | DFS | `validation_result`, `validation_passed_result` |
| `mcp__isce-di-db__query` | `position-processor` | Position Processor | `position_detail` |
| `mcp__isce-ds-db__query` | `das` | DAS-KC / DAS-Jobs | `das_request_store` |
| `mcp__isce-ds-db__query` | `dpcs` | DPCS | `carrier`, `data_provider`, `data_provider_carrier` |
| `mcp__isce-ds-db__query` | `dust` | DUST | `milestonepipeline`, `positionpipeline`, `transportplanpipeline` |
| `mcp__isce-ds-db__query` | `vrdan` | VRDAN | `versioned_payload_data` |
| `mcp__isce-ds-db__query` | `reference-data-locations` | Reference Data | `unloc_details`, `map_rkst_unloc`, `isce_alt_codes` |
| `mcp__isce-ds-db__query` | `state-manager` | State Manager | `state` |
| `mcp__isce-ds-db__query` | `reference-data` | Reference Data (subscriptionless) | `themisReferenceDb` tables |

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
| ETD stale / ATD not received / estimated timestamp not updating | SV → DOS → MP → DUST → DAS | `shipment-visibility-db: vessel_voyage_port_call_timestamp` | `/vessel-timestamp-debug` |
| Clustering behaviour unclear / wrong cluster grouping / unexpected providers in a cluster | Milestone Processor API | `GET /journey/{journeyId}/unitOfTracking/{unitOfTracking}` | `/milestone-clustering-inspector` |
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
