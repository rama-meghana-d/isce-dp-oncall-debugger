# DP Child: SUBSCRIPTIONLESS (all variants)

Fetches raw blob for any SUBSCRIPTIONLESS variant and runs the transformation script.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
dp_name:   SUBSCRIPTIONLESS | SUBSCRIPTIONLESS_BOL | SUBSCRIPTIONLESS_BOLCONTAINER | SUBSCRIPTIONLESS_CBCN
```

$ARGUMENTS

---

## Run

Select DP ID from `dp_name`:

| dp_name | data_provider_id |
|---------|----------------|
| SUBSCRIPTIONLESS | 4677b314-13bf-47ad-a859-b65165839618 |
| SUBSCRIPTIONLESS_BOL | 4677b314-13bf-47ad-a859-b65165839621 |
| SUBSCRIPTIONLESS_BOLCONTAINER | 4677b314-13bf-47ad-a859-b65165839622 |
| SUBSCRIPTIONLESS_CBCN | 4677b314-13bf-47ad-a859-b65165839624 |

```bash
DP_ID="<select from table above>"
curl -sf "http://localhost:8085/data-provider/${DP_ID}/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_subscriptionless.py --job-id <job_id> --container <container> --dp-name <dp_name>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Identifies granularity key using priority: `CONTAINER_NUMBER > BILL_OF_LADING > AIR_WAY_BILL_NUMBER > CARRIER_BOOKING_NUMBER`
- Events are passthrough (pre-transformed by carrier) — no trigger/timing remapping
- Notes which events have vessel references **stripped** (only vessel-related triggers may carry vessel refs)
- Shows transport plan legs if present
