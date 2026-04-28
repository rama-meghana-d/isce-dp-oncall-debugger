# Milestone Clustering Inspector

Fetches the `JourneySubscription` from **both** Milestone Processor (MP) and MCE for a given journey + container, and renders a full three-level normalization + clustering trace:

```
[MP output] → [MCE event] → [MCE cluster]
```

For each event, the trace shows what MP produced (post-DUST, pre-MCE), what MCE stored on the event itself, and what MCE's clustering algorithm normalised it to (location roll-up, authoritative timestamp). You also get a complete cluster-by-cluster summary with cluster ID, timestamp, timing, location + alt codes, and per-event alt codes.

**Flow context:**
```
Raw provider payload
  → DUST (transform)
    → Milestone Processor (enrich: trigger/timing/location normalisation)  ← port 8081
      → MCE (clustering + triangulation)                                   ← port 8087
        → DOS → GIS → intelligentJourneyTopic
```

## Input

```
journey_id:        <UUID>
unit_of_tracking:  <CONTAINER_NUMBER>    e.g. CSLU6273978
```

$ARGUMENTS

---

## Step 1 — Port-Forward MP and MCE

```bash
kubectl port-forward deployment/milestone-processor 8081:8080 -n isce-data-intelligence-prod &
PF_MP=$!

kubectl port-forward deployment/mce 8087:8080 -n isce-data-intelligence-prod &
PF_MCE=$!

sleep 4

curl -sf http://localhost:8081/actuator/health && echo "MP port-forward OK"   || echo "MP FAILED"
curl -sf http://localhost:8087/actuator/health && echo "MCE port-forward OK"  || echo "MCE FAILED"
```

If either health check fails: verify `kubectl config current-context` is pointing at the prod cluster.

---

## Step 2 — Fetch MP Response (pre-clustering snapshot)

```bash
curl -sf -H "api-version: 1" \
  "http://localhost:8081/journey/<journey_id>/unitOfTracking/<unit_of_tracking>" \
  > /tmp/mp_response.json && echo "MP response saved ($(wc -c < /tmp/mp_response.json) bytes)"
```

This is MP's output — the events it sends to MCE before any clustering happens. Each event has the normalised `eventTrigger`, `eventTiming`, location, and timestamp that MP enrichment produced.

**Error handling:**
- `204 No Content` → MP has not processed this journey yet; check Kafka consumer lag on `milestone-topic`
- `404` → journey + container not found in MP; confirm identifiers and that at least one event has been processed

---

## Step 3 — Fetch MCE Response + Full Normalization Trace

```bash
curl -sf -H "api-version: 1" \
  "http://localhost:8087/journey/<journey_id>/unitOfTracking/<unit_of_tracking>" \
  | python3 .claude/scripts/parse_clusters.py --mp /tmp/mp_response.json
```

**Error handling (MCE):**
- `204 No Content` → MCE has not yet clustered this journey; check Kafka lag on `milestone-topic`, or check if MCE errored and republished to `sjg-topic` fallback
- `404` → journey + container not found in MCE's `journey_subscription` table
- `500` or connection refused → port-forward dropped; re-run Step 1

---

## Step 4 — Interpret the Output

### 4a — Cluster Header

Each cluster block starts with:

```
### Cluster N — <EVENT_TRIGGER> / <EVENT_TIMING>
  Cluster ID        : N
  Cluster Timestamp : <ISO UTC>     ← MCE authoritative timestamp (triangulated)
  Cluster Timing    : ACTUAL | ESTIMATED
  Cluster Location  : <UN LOCODE>  ← MCE normalised cluster location
  Alt Codes         : <GEOID=...> | <CARGOWISE1=...> | ...
  Event Count       : N
```

### 4b — Three-Level Normalization Trace

```
  Provider             Layer       Trigger                   Timing       Timestamp              Location Notes
  -------------------- ---------- ------------------------- ------------ ---------------------- -------- ------------------------------
  P44_NEW              MP         VESSEL_ARRIVAL            ACTUAL       2024-03-01T14:30:00Z   USLAX    (MP output — pre-MCE)
                       MCE event  VESSEL_ARRIVAL            ACTUAL       2024-03-01T14:30:00Z   USLAX    (matches MP)
                       MCE cluster VESSEL_ARRIVAL           ACTUAL       2024-03-01T14:30:00Z   USLONG   loc USLAX→USLONG; ts selected as authoritative

  VIZION               MP         VESSEL_ARRIVAL            ACTUAL       2024-03-01T16:45:00Z   USLGB    (MP output — pre-MCE)
                       MCE event  VESSEL_ARRIVAL            ACTUAL       2024-03-01T16:45:00Z   USLGB    (matches MP)
                       MCE cluster VESSEL_ARRIVAL           ACTUAL       2024-03-01T14:30:00Z   USLONG   loc USLGB→USLONG; ts Δ+2.3h (not selected)
```

**Reading the trace:**

| Observation | Meaning |
|---|---|
| **MP → MCE event**: trigger/timing/location changed | MCE itself modified the event (rare — usually a MCE bug or re-enrichment) |
| **MP → MCE event**: all `(matches MP)` | MCE stored exactly what MP sent; any normalisation is purely in the cluster step |
| **MCE event → MCE cluster**: `loc X→Y` | MCE rolled up the provider's sub-location / terminal UNLOC to the port UNLOC via reference data |
| **MCE event → MCE cluster**: `ts selected as authoritative` | This provider's timestamp was chosen by triangulation (highest precedence or closest to consensus) |
| **MCE event → MCE cluster**: `ts Δ+2.3h (not selected)` | This provider reported the event 2.3 h later; its timestamp was not selected |
| MP row absent for an event | MP sent the event after the script snapped the MP response, or event identifier changed — match by trigger+timing+location instead |

### 4c — Per-Event Table

```
| # | Event Trigger | Timing | Data Provider | Event Timestamp | Δ vs Cluster | Event Location | Event Alt Codes | Loc Changed |
```

- **Δ vs Cluster** — signed offset between the event's timestamp and the cluster authoritative timestamp. `±0s` = this provider was the timestamp source.
- **Loc Changed** — `YES` if MCE changed this event's location during clustering.
- **Event Alt Codes** — alternative codes on the *event's* location object (per provider) — useful for cross-checking reference data mappings.

---

## Step 5 — Optional: Confirm MP Normalisation via DB

If you want to see what MP wrote to its own `milestone` table (and compare with what MP returned from the API):

```sql
-- db: milestone-processor
SELECT event_trigger, event_timing, data_provider, timestamp, location_code, created_at
FROM milestone
WHERE journey_id = '<journey_id>'
  AND unit_of_tracking = '<unit_of_tracking>'
ORDER BY created_at DESC
LIMIT 100;
```

Load `mcp__isce-di-db__query` via ToolSearch before running.

Divergence between this table and the MP API response means an event was re-processed or arrived out of order after the API call.

---

## Step 6 — Optional: MCE Triangulation Audit

To confirm which event MCE selected as authoritative per cluster and which rules it applied:

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

- `applied_rules` containing `Global Provider Precedence` → winner chosen by precedence (MaerskTnT > P44_NEW > VIZION > GNOSIS > Portcast > others).
- Cross-check `data_provider` here against the `ts selected as authoritative` entry in the normalization trace.

---

## Step 7 — Cleanup

```bash
kill $PF_MP $PF_MCE 2>/dev/null || pkill -f "port-forward.*(milestone-processor|mce)"
rm -f /tmp/mp_response.json
```

---

## Quick Reference — Normalization Signal Patterns

| Signal | Likely cause |
|---|---|
| MP location ≠ MCE event location | MCE re-enriched the event (unexpected — flag as potential MCE bug) |
| MCE event location ≠ cluster location | MCE clustering rolled up sub-location/terminal to port UNLOC via reference data — check `themis-visibility-reference-data-locations` |
| All events `Loc Changed = NO` | All providers agreed on the same UNLOC; no location normalisation needed |
| One provider `Δ ±0s`, rest have large deltas | That provider was the timestamp authority; others reported late/early |
| Single-provider cluster beside a multi-provider cluster for same trigger | Solo event's timestamp was outside the merge window; it could not be grouped |
| MP trigger/timing ≠ MCE event trigger/timing | MP enrichment changed the event type — check DAS transformer mapping for that provider |
| MP row absent for a MCE event | Event arrived at MP after /tmp/mp_response.json was saved; re-fetch MP or match by trigger+timing+location |
| `UNCLUSTERED` events in MCE output | Events arrived after the last MCE run — check Kafka consumer lag on `milestone-topic` |
| Event in MP but absent from MCE clusters | MCE de-duplicated it, or it arrived after the last clustering run |