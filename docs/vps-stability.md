# VPS Stability Policy

The paper-trading VPS uses a 1 GiB container memory limit for Redis. Redis persistent data lives on `/mnt/aitos-data/redis` and is never removed by Docker cleanup workflows.

Disposable Docker containers, unused images, networks, and BuildKit cache may be pruned. Persistent database data under `/mnt/aitos-data` is protected by an explicit mount check before cleanup.

The hourly VPS Stability Check verifies Redis is responsive and has completed dataset loading, verifies `aitos-paper` is healthy, and reports Docker and filesystem usage.
