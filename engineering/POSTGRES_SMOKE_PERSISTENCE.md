# Hosted PostgreSQL persistence smoke probe

The Render and Railway smoke service is a storage-adapter probe, not a full AITOS
trading runtime. Its `/health` endpoint is intentionally read-only.

## What the health request proves

Every request opens fresh AITOS PostgreSQL adapter pools and reads pre-existing,
stable sentinel records:

- The known `AITOSSMOKEUSDT` 1-minute market-data record and its expected close.
- The stable operational journal entry for `render-smoke:trade`.
- The durable OPEN trade snapshot.
- The DECISION, TRADE_LINK and OUTCOME records for
  `render-smoke:decision`.
- The applied PostgreSQL schema version.

The endpoint does not insert, update, repair, or re-seed records. A missing or
changed sentinel fails closed with HTTP 503. The response explicitly reports
`write_performed: false`.

A successful request after a service process restart therefore checks that the
same database still contains records written before that restart. It does not
prove disk-level backup recovery, multi-region durability, or ClickHouse/SQLite
coordination.

## One-time initialization

Only when provisioning a genuinely new, empty smoke database, run the following
command once from a trusted operator environment with that database's DSN:

```sh
DATABASE_URL='postgresql://…' python scripts/render_postgres_smoke.py --seed
```

The seed uses fixed identities and adapter idempotency keys. It is an explicit
write operation and must never be added to a health check, startup hook, or
automatic deploy command. Do not run it against a production trading database.

After seeding, start the service normally:

```sh
python scripts/render_postgres_smoke.py
```

Then request `GET /health`. A process restart followed by another successful
health request is the required hosted persistence smoke check.

## Operational interpretation

- HTTP 200: all pre-existing sentinels were read and matched.
- HTTP 503: database unavailable, schema mismatch, or one or more sentinels
  missing/changed. Investigate; do not auto-seed to make health green.
- HTTP 404: path is not the health endpoint.
- This probe does not submit exchange requests or require Binance credentials.
