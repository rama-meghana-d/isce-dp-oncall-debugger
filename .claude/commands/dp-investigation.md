# Data Provider Investigation (Parent)

Resolves all active data-provider jobs for a container + journey, port-forwards dp-response-consumer, and dispatches to the appropriate per-provider child skill to produce a side-by-side raw↔transformed picture. Use this to determine whether any issue (wrong event, missing timestamp, wrong transport plan, extra/missing leg, wrong vessel, etc.) originates at the data provider or inside DAS-Jobs transformation.

## Input

```
container:  <CONTAINER_NUMBER>    e.g. CSLU6273978
journey_id: <JOURNEY_ID>
```

$ARGUMENTS

---

## Step 1 — Resolve Jobs + Data Provider (SIR only, single query)

Load `mcp__isce-di-db__query` via ToolSearch.

`data_aggregator_request_mapping` (DARM) lives in the SIR database and holds both `job_id` and `data_provider`, eliminating any need to query DAS separately.

```sql
-- db: sir
SELECT
    s.subscription_id,
    s.journey_id,
    s.status                AS sub_status,
    darm.job_id,
    darm.data_provider,
    darm.status             AS job_status,
    darm.created_on
FROM subscription s
JOIN data_aggregator_request_mapping darm
  ON darm.subscription_ids @> jsonb_build_array(s.subscription_id)
WHERE s.journey_id = '<journey_id>'
  AND s.unit_of_tracking::text LIKE '%<container>%'
ORDER BY darm.created_on DESC
LIMIT 20;
```

If empty, retry without the `journey_id` filter:

```sql
-- db: sir
SELECT
    s.subscription_id,
    s.journey_id,
    s.status                AS sub_status,
    darm.job_id,
    darm.data_provider,
    darm.status             AS job_status,
    darm.created_on
FROM subscription s
JOIN data_aggregator_request_mapping darm
  ON darm.subscription_ids @> jsonb_build_array(s.subscription_id)
WHERE s.unit_of_tracking::text LIKE '%<container>%'
ORDER BY darm.created_on DESC
LIMIT 20;
```

Print a summary table:

| subscription_id | journey_id | sub_status | job_id | data_provider | job_status | created_on |
|----------------|-----------|-----------|--------|--------------|-----------|-----------|

Flag rows where:
- `job_id IS NULL` → job not yet created; note as **NO JOB**
- `job_status = FAILED` → job failed; note as **FAILED**
- `sub_status = TRACKING_STOPPED` → subscription expired; note as **EXPIRED**

Proceed with all rows that have a non-null `job_id`.

---

## Step 2 — Port-Forward dp-response-consumer

```bash
kubectl port-forward deployment/dp-response-consumer 8085:8080 -n isce-data-sourcing-prod &
PF_PID=$!
sleep 3
curl -sf http://localhost:8085/actuator/health && echo "Port-forward OK" || echo "Port-forward FAILED — check kubectl context"
```

If health check fails: verify `kubectl config current-context` is pointing at the prod cluster.

---

## Step 3 — Dispatch to Per-Provider Child Skills

**Data Provider ID reference:**

| data_provider | data_provider_id |
|--------------|----------------|
| VIZION | 4677b314-13bf-47ad-a859-b65165839616 |
| GNOSIS | 4677b314-13bf-47ad-a859-b65165839617 |
| P44_NEW | 4677b314-13bf-47ad-a859-b65165839623 |
| P44_ROAD_NEW | dfffaac1-770c-4434-b5ca-14721dd5a9f2 |
| MAERSKTNT | ec65ad85-ddde-438a-b953-eae97d5ca376 |
| INTTRA | e4c1e004-c98d-41cf-841b-ada912d26a1d |
| PCA | e48d7d48-4e5e-4ac2-b5ec-b3cecbc1d27a |
| SEEBURGER | 86dc318d-81f6-471f-b7c1-ec4edaced98c |
| PORTCAST | 7e2b1c8a-4f3d-4c2a-9e6a-2b1f8e7d9c3a |
| CONEKSION | 4677b314-13bf-47ad-a859-b65165839625 |
| SUBSCRIPTIONLESS | 4677b314-13bf-47ad-a859-b65165839618 |
| SUBSCRIPTIONLESS_BOL | 4677b314-13bf-47ad-a859-b65165839621 |
| SUBSCRIPTIONLESS_BOLCONTAINER | 4677b314-13bf-47ad-a859-b65165839622 |
| SUBSCRIPTIONLESS_CBCN | 4677b314-13bf-47ad-a859-b65165839624 |
| BLUEBOX | 4677b314-13bf-47ad-a859-b65165839620 |

For each job row (non-null `job_id`), invoke the matching child skill:

- **VIZION** → `/dp-vizion` with `job_id`, `container`
- **GNOSIS** → `/dp-gnosis` with `job_id`, `container`
- **P44_NEW** or **P44_ROAD_NEW** → `/dp-p44` with `job_id`, `container`, `dp_name`
- **MAERSKTNT** → `/dp-maersktnt` with `job_id`, `container`
- **INTTRA** → `/dp-inttra` with `job_id`, `container`
- **PCA** → `/dp-pca` with `job_id`, `container`
- **SEEBURGER** → `/dp-seeburger` with `job_id`, `container`
- **PORTCAST** → `/dp-portcast` with `job_id`, `container`
- **CONEKSION** → `/dp-coneksion` with `job_id`, `container`
- **SUBSCRIPTIONLESS / SUBSCRIPTIONLESS_BOL / SUBSCRIPTIONLESS_BOLCONTAINER / SUBSCRIPTIONLESS_CBCN** → `/dp-subscriptionless` with `job_id`, `container`, `dp_name`
- **BLUEBOX** → `/dp-bluebox` with `job_id`, `container`
- Any other `data_provider` → note as **UNKNOWN PROVIDER — no child skill**, show raw blob directly via curl

---

## Step 4 — Synthesize Findings + Cleanup

After all child skills complete, print a consolidated summary:

| Provider | Job ID (short) | Raw Events | Transformed Events | Dropped | Root Cause Layer |
|----------|---------------|-----------|-------------------|---------|-----------------|
| VIZION   | abc…123       | 8         | 6                 | 2 (AIS) | PROVIDER / TRANSFORMATION / DOWNSTREAM |

**Layer verdict:**
- **DATA PROVIDER** → raw payload is missing the data entirely, or contains incorrect values
- **DAS TRANSFORMATION** → raw data is correct but was dropped or corrupted during mapping
- **DOWNSTREAM** → both raw and transformed look correct; issue is in MP/MCE/DOS/GIS

Kill the port-forward:
```bash
kill $PF_PID 2>/dev/null || pkill -f "port-forward.*dp-response-consumer"
```
