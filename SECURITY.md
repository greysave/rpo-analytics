# Security Policy

## Scope

`rpo-analytics` is a local, offline analytics tool. It opens no network
sockets, makes no outbound requests and reads only the files and SQLite
database you point it at. It is intended for synthetic or exported
run-history data.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a vulnerability"
under the repository's Security tab). Do not open a public issue for
suspected vulnerabilities. Expect an acknowledgement within a week; this is a
volunteer-maintained project.

## Hardening notes

- All SQL uses bound parameters; the only interpolated value is the integer
  schema version in migrations.
- Ingested identifiers must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`, so
  values like `=cmd|...` cannot reach CSV exports as spreadsheet formulas.
- Report and export commands open the database read-only (`mode=ro`), so they cannot modify it
  or create a new file from a mistyped path; only `init` and `ingest` write.
- Inputs are validated per record; malformed records are rejected and reported
  without aborting the batch.
- Please never commit real backup inventories, hostnames, credentials or
  customer data. A gitleaks workflow runs on every push and pull request.
