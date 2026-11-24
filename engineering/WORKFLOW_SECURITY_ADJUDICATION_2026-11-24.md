# GitHub Actions security adjudication — 2026-11-24

## Scope and inventory

The current PR head contains 49 workflow YAML files in `.github/workflows/`.
The earlier request referenced 48; the checked-out branch has 49, so the
additional file must not be silently omitted. This report records a first-pass
source review of the high-impact workflows. It does not pretend to be an
independent security sign-off or a full review of all 49 files.

## High-impact workflow decisions

| Workflow | Observed capability | Adjudication / required guard |
|---|---|---|
| `cd.yml` | `packages:write`, production SSH, deployment credentials, Redis/ClickHouse/Neo4j/Binance/database secrets, manual dispatch; action refs use version tags rather than full commit SHAs | Retain as the production deploy path. Keep manual/opt-in and production environment approval. Do not merge with staging or routine CI. Pin every external action to reviewed full SHAs before production promotion. Verify all secret consumers and host/data-disk identity. |
| `staging-cd.yml` | `packages:write`, staging SSH and database/venue secrets, manual dispatch; version-tag action refs | Retain separately from production. Require staging environment protection and least-privilege staging credentials. Pin actions. No automatic promotion based only on a green build. |
| `vps-hard-reset.yml` | SSH and disk identity secrets; manual dispatch; destructive host/data reset operations | Retain as a separately protected break-glass workflow. Never call from routine CI or deployment. Require explicit operator approval, exact host and disk UUID confirmation, and a verified restorable backup. |
| `reset-paper-data.yml` | SSH mutation against paper data; manual dispatch | Retain separately. Require explicit environment/target confirmation and backup/retention review. Never consolidate into generic maintenance. |
| `database-recovery.yml` | `actions:write`, SSH secrets, manual recovery action | Retain as a recovery-only workflow. Restrict dispatch permissions and validate selected run/artifact/target before any write. Review why `actions:write` is required; remove it if no rerun/cancel API call remains. |
| `maintenance.yml` | Manual SSH maintenance with deployment-host credentials | Retain separately until each command is reviewed. Use read-only diagnostics by default; destructive subcommands require separate explicit confirmation. |
| `deployment-lock-recovery.yml` | Manual SSH lock cleanup/recovery | Retain separately. Require evidence that the lock owner is dead or lease expired; never delete a live lock solely by age or operator guess. |
| `windows-console-sign.yml` | Desktop signing/build workflow; action refs are mutable version tags | Retain separate from generic CI because signing has a different trust boundary. Pin actions and verify signing identity, artifact provenance and release permissions. |
| `three-store-recovery-integration.yml` | `contents:write`, `actions:write`, Binance Futures testnet credentials, manual dispatch and write-capable recovery | Retain as an isolated, operator-triggered venue acceptance workflow. Never make it a PR job or routine CI dependency. Review every testnet write and ensure credentials are demo-only. |
| `three-store-recovery-ci.yml` | PR-triggered, `contents:read`, disposable ClickHouse + SQLite and local signed venue fixture; no Binance credentials | Retain as the default PR recovery gate. It is the safe, non-venue counterpart and must not inherit write scopes. |

## Consolidation decision

No workflow was deleted, disabled, renamed, or merged in this pass. This is
intentional: production deployment, staging deployment, break-glass reset,
paper-data reset, database recovery, desktop signing and authenticated venue
acceptance are distinct trust domains. Combining them would broaden the blast
radius and make approval/required-check dependencies harder to reason about.

The 49-file registry and static audit remain the inventory baseline. Full
adjudication still requires per-file confirmation of:
- workflow and job permissions, including inherited repository defaults;
- all triggers, reusable-workflow callers, dispatch callers and fork trust;
- secret-to-environment mapping and required reviewers;
- full-SHA pinning and action provenance;
- shell interpolation, downloaded artifacts and untrusted inputs;
- destructive commands, target identity, backups and rollback;
- required branch-protection checks and external consumers.

## Release gate

Until all 49 rows have named owners and the above evidence is recorded, the
workflow-governance phase remains OPEN. Do not claim the 48/49-workflow
security adjudication or consolidation is complete. Preserve all historical
workflows and their run evidence.
