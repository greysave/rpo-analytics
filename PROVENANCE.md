# Provenance

This repository was written from scratch as a clean-room portfolio project.

## What it is built from

- A data contract (`rpo-analytics/1`, documented in the README) invented for
  this project. Field names are generic (`run_id`, `workload_id`, `policy_key`,
  `status`, byte counts) and do not mirror any vendor API or schema.
- A seeded pseudo-random generator (`rpo_analytics/fixtures.py`) that produces
  all sample data. Object names such as `vm-001` and policy names such as
  `gold` are invented placeholders.
- The Python standard library (`sqlite3`, `argparse`, `zoneinfo`, ...). Timezone
  rules come from the operating system, or from the optional `tzdata` package
  (required on Windows; `UTC` works without either). Development tools (pytest,
  pytest-cov, ruff, mypy) are pinned in `pyproject.toml`.
- The unmodified Apache License 2.0 text, as published by the Apache Software
  Foundation.

## What it is not built from

- No source code, tests, fixtures, schemas, API clients, endpoints, hostnames,
  credentials or documentation from any employer, customer or vendor.
- No vendor SDK or vendor API response samples.
- No git history from any other repository. This repository starts from a
  single fresh commit.
- No real backup, customer or infrastructure data of any kind.

## Algorithms

Compliance gaps, rolling means, drift thresholds and the least-squares
capacity line are textbook calculations; each is documented in the module
docstrings and the README so it can be checked by hand.

## Author

Public author identity: `Greysave contributors <noreply@users.noreply.github.com>`.
