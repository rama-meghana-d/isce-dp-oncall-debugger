# DP Child: INTTRA

Fetches raw Inttra blob and runs the transformation script. **VESSEL_ARRIVAL and VESSEL_DEPARTURE are always dropped** (flag default=true).

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `e4c1e004-c98d-41cf-841b-ada912d26a1d`

```bash
curl -sf "http://localhost:8085/data-provider/e4c1e004-c98d-41cf-841b-ada912d26a1d/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_inttra.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Remaps `ESTIMATED_DELIVERY` → `DELIVERY`; all other triggers pass through
- **Drops VESSEL_ARRIVAL and VESSEL_DEPARTURE** (inttra-exclude-vessel-arrival-departure=true, default ON)
- Applies deduplication: unique by (trigger, timing, locationCode)
- Extracts vessel references from `eventConnections`
- Shows counts and root cause guide
