# Milestone Clustering Inspector

Fetches the clustered `JourneySubscription` from MCE's own storage (via `FusedMilestoneController`) for a given journey + container, and renders a cluster-by-cluster breakdown: which events are grouped together, the cluster timestamp, cluster location (with alternative codes), and each constituent event's data provider name, individual timestamp, and location code.

**Flow context:**
- Milestone Processor API → raw, unclustered events (MCE *input*)
- **MCE `GET /journey/{journeyId}/unitOfTracking/{unitOfTracking}`** → clustered result stored in MCE's `journey_subscription` table (MCE *output*) ← this skill calls this

## Input

```
journey_id:        <UUID>
unit_of_tracking:  <CONTAINER_NUMBER>    e.g. CSLU6273978
```

$ARGUMENTS

---

## Step 1 — Port-Forward MCE

```bash
kubectl port-forward deployment/mce 8087:8080 -n isce-data-intelligence-prod &
PF_PID=$!
sleep 3
curl -sf http://localhost:8087/actuator/health && echo "Port-forward OK" || echo "Port-forward FAILED — check kubectl context"
```

If health check fails: verify `kubectl config current-context` is pointing at the prod cluster.

---

## Step 2 — Fetch Clustered JourneySubscription + Parse

```bash
curl -sf -H "api-version: 1" \
  "http://localhost:8087/journey/<journey_id>/unitOfTracking/<unit_of_tracking>" \
  | python3 .claude/scripts/parse_clusters.py
```

**Error handling:**
- `204 No Content` → MCE has not yet processed this journey; check Kafka consumer lag on `milestone-topic`, or check if MCE errored and republished to `sjg-topic` / `milestone-topic` fallback
- `404` → journey + container not found in MCE's `journey_subscription` table; confirm identifiers are correct and that MCE has processed at least one event for this journey
- `500` or connection refused → port-forward may have dropped; re-run Step 1

---

## Step 3 — Interpret the Cluster Output

The script groups all events by `cluster.clusterIdentifier` and prints one block per cluster:

```
### Cluster N — <EVENT_TRIGGER> / <EVENT_TIMING>
Cluster Timestamp : <ISO datetime in UTC>
Cluster Location  : <UN LOCODE>
Alt Codes         : <GEOID=...> | <CARGOWISE1=...> | ...

| # | Event Trigger | Timing | Data Provider | Event Timestamp | Location |
```

**Why a particular event ended up in a given cluster — what to look for:**
- **Same trigger + same timing + same UN LOCODE** → core clustering criteria; events from different providers matching on all three are grouped together
- **Close timestamps across providers** → if timestamps diverge significantly (hours apart) but the cluster still formed, the clustering algorithm accepted them as the same physical event by location + trigger
- **Single-provider cluster alongside a multi-provider cluster for the same trigger** → the solo cluster's timestamp was too far from the others to merge; compare timestamps to see the gap
- **UNCLUSTERED events** → `cluster` field absent; MCE has not processed these yet, or the event arrived after the last MCE run — check Kafka lag and trigger a `/replay` if needed
- **Unexpected cluster location** → the location on the cluster differs from the event-level location; check whether the reference-data-locations mapping for that UN LOCODE resolves differently per provider

**Cross-check with MCE DB (optional):**
If you want to see MCE triangulation audit (which event was selected as authoritative from each cluster), query MCE DB:
```sql
-- db: mce
SELECT isce_event_id, applied_rules, data_provider, event_trigger, event_timing, location_code, created_at
FROM isce_triangulation_audit
WHERE journey_id = '<journey_id>'
  AND unit_of_tracking = '<unit_of_tracking>'
ORDER BY created_at DESC
LIMIT 50;
```
Load `mcp__isce-di-db__query` via ToolSearch before running.

---

## Step 4 — Cleanup

```bash
kill $PF_PID 2>/dev/null || pkill -f "port-forward.*mce"
```
