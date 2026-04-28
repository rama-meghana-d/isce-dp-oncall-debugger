# DP Child: PCA

Fetches raw PCA blob and runs the transformation script. PCA always produces one event: `VESSEL_ARRIVAL / PREDICTED`.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `e48d7d48-4e5e-4ac2-b5ec-b3cecbc1d27a`

```bash
curl -sf "http://localhost:8085/data-provider/e48d7d48-4e5e-4ac2-b5ec-b3cecbc1d27a/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_pca.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Extracts `predictedArrivalTime` and `lastDischargePort`
- Shows the fixed transformation: always `VESSEL_ARRIVAL / PREDICTED`
- No filtering logic — one event is always produced if data is present
- Notes that PCA covers only predicted arrival; all other event types come from other providers
