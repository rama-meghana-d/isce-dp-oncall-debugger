# DP Child: PORTCAST

Fetches raw PortCast blob and runs the transformation script.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `7e2b1c8a-4f3d-4c2a-9e6a-2b1f8e7d9c3a`

```bash
curl -sf "http://localhost:8085/data-provider/7e2b1c8a-4f3d-4c2a-9e6a-2b1f8e7d9c3a/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_portcast.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Drops master shipments upfront
- Drops shipments with null `billOfLading` or `containerMetadata`
- Maps `eventType` → `eventTrigger` (DCSA-style)
- Extracts import hold attributes (`CUSTOMS`, `LINE`, `OTHER`) — these are output as tracking attributes, not milestone events
- Shows transport plan legs if present
