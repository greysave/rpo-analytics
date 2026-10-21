# Case study: RPO compliance, drift and capacity analytics

> Fictional scenario generated from seed 7. All workloads, policies and measurements are synthetic.

## Scenario

**Example Industries** needs to turn 45 days of fictional protection-run history into defensible RPO compliance, early drift warnings and a transparent capacity forecast.

## Approach

- Ingest an explicit versioned JSON contract into SQLite.
- Reject conflicting run IDs while keeping identical ingest idempotent.
- Measure non-compliant time between recovery points.
- Compare baseline and recent windows for drift.
- Fit a documented ordinary least-squares capacity line and report its R².
- Open all reporting commands in read-only SQLite mode.

## Reproduce

```console
rpo-analytics generate --seed 7 --days 45 --output sample.json
rpo-analytics --db rpo.db init
rpo-analytics --db rpo.db ingest sample.json
rpo-analytics --db rpo.db compliance
rpo-analytics --db rpo.db drift
rpo-analytics --db rpo.db forecast --capacity-limit-bytes 2000000000000
```

## Verified synthetic result

The run inserted **5,521** records with zero conflicts or rejections. Selected compliance rows:

```text
vm-001  gold    RPO 60m    violated   compliance 0.9796   27 violations
vm-002  silver  RPO 240m   violated   compliance 0.8700   62 violations
vm-003  bronze  RPO 1440m  violated   compliance 0.7087    2 violations
vm-006  bronze  RPO 1440m  compliant  compliance 1.0000    0 violations
```

Detected drift included:

```text
vm-002 rpo_drift critical: mean daily max gap 226.2 -> 384.4 min (+69.9%, RPO 240 min)
vm-003 no_recent_data critical: no runs in the last 7 days
vm-005 rpo_drift critical: mean daily max gap 201.1 -> 304.5 min (+51.4%, RPO 240 min)
```

Capacity model: 31 points, **83.59 GB/day** slope and **R² 0.9981**. The synthetic dataset already exceeded the example 2 TB limit, so `days_until_full` was 0.

## What this demonstrates

Applied data engineering and analytics: schema design, idempotent ingestion, time-window reasoning, compliance math, drift detection, regression, explainable forecasting and reproducible CLI reports. It is intentionally described as transparent analytics—not machine learning.
