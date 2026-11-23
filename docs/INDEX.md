# AITOS Documentation Index

This is the curated entry point for maintained engineering and operator
documentation. The README remains the project overview; INSTALLATION.md remains
the canonical installation/deployment procedure. Historical reports are evidence
snapshots, not current operational instructions unless explicitly adopted below.

## Start here

- [Project overview](../README.md) — product scope, architecture summary and quick start.
- [Installation & deployment](../INSTALLATION.md) — desktop setup, local runtime and deployment procedures.
- [Operator user manual](USER_MANUAL.md) — workspace pages, connection modes and read-only boundaries.
- [Local development](LOCAL_DEVELOPMENT.md) — low-resource local setup.
- [Operator gateway](OPERATOR_GATEWAY.md) — authenticated read-only gateway boundary.
- [Credential security & remote signing RFC](CREDENTIAL_SECURITY_REMOTE_SIGNING_RFC.md) — signer custody, threat model and production gaps.

## Architecture and financial integrity

- [Architecture Evolution Program](../engineering/ARCHITECTURE_EVOLUTION_PROGRAM.md) — architecture decisions and evolution gates.
- [Financial State Authority & Reconciliation](../engineering/FINANCIAL_STATE_AUTHORITY_AND_RECONCILIATION.md) — current store ownership and reconciliation contract.
- [Execution recovery](EXECUTION_RECOVERY.md) — durable intent, protective orders and restart behavior.
- [Root-cause register](../engineering/ROOT_CAUSE_REGISTER.md) — findings, evidence and open/closed status.
- [System audit 2026-11-20](../engineering/SYSTEM_AUDIT_2026-11-20.md) — baseline audit and acceptance gates.
- [Production-readiness remediation 2026-11-23](../engineering/PRODUCTION_READINESS_REMEDIATION_2026-11-23.md) — current remediation branch status and remaining gates.

## Market and research domains

- [Binance Futures testnet configuration](../engineering/BINANCE_FUTURES_TESTNET_CONFIGURATION.md) — endpoint and credential separation.
- [Spot integration](SPOT_MARKET_INTEGRATION.md) — isolated Spot scope and rollout restrictions.
- [Adaptive multi-timeframe trading intelligence](ADAPTIVE_MTF_TRADING_INTELLIGENCE.md) — market structure, order flow and portfolio research.
- [Continuous intelligence](CONTINUOUS_INTELLIGENCE.md) — outcome attribution and governed model evolution.

## Maintenance policy

- Keep one current operational procedure per task in INSTALLATION.md or the relevant
  domain guide; link historical incident/audit reports rather than duplicating
  instructions.
- When a guide is superseded, mark it **ARCHIVED / SUPERSEDED**, link its replacement,
  and move it only after all inbound links and workflow references are checked.
- Do not delete workflow files or historical evidence as a documentation cleanup
  shortcut. Classify workflows by trigger, write permissions, environment, and
  destructive/financial effect before proposing consolidation.
- Every dated audit must state its exact baseline SHA and distinguish source evidence,
  CI evidence, runtime evidence and NOT TESTED gates.
