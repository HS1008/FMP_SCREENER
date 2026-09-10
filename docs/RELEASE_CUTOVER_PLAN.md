# Production release plan (authorization required)

This plan is reviewable evidence. It does **not** merge PRs, install systemd,
or mutate production from Cursor.

## WARNING

**MERGING FMP #29 TO MAIN IS A PRODUCTION DEPLOY EVENT.**

`/.github/workflows/deploy.yml` triggers on `push -> main`, runs on
`ubuntu-latest`, then SSHes to DigitalOcean with `StrictHostKeyChecking=yes`
and a pinned `DO_SSH_KNOWN_HOSTS` secret. It is **not** a human workflow
dispatch and **not self-hosted**.

Do not merge #29 until first-run deployment prerequisites below are satisfied.

A separate recommendation that manual dispatch would be safer may be considered
later. Auto-deploy policy is unchanged without human approval.

## 1. Pull requests and order

1. **quant-strategies MAIN safety/tooling PR** → `main`
   (launch authorization on default-branch QC workflows only).
2. **HS1008/quant-strategies #28** → `research-integration`
   (`cursor/research-architecture-674b`). CSFML label-contract producer.
3. **HS1008/fmp_screener #29** → `main` **LAST**
   (`cursor/research-architecture-674b`). Auto-deploys.

Do not merge platform-MI into this line. Do not launch QuantConnect.

## 2. What executes automatically on merge to main

1. GitHub Actions checks out the merged SHA.
2. Configures pinned SSH trust (`DO_SSH_KEY`, `DO_SSH_KNOWN_HOSTS`).
3. SSHes to the droplet and runs `scripts/deploy_host.sh --sha <merged_sha>`.
4. Host script: fetch/pull `/root/FMP_SCREENER` to that SHA, stage
   `/opt/fmp/releases/<sha>`, install the **staged** venv, apply DB migrations
   **once** from the staged tree using the trusted checksum baseline, provision
   / verify the dashboard read-only identity, query-back official research
   rows, record sanitized deploy identity, restart the **existing**
   `fmp-dashboard` unit, post-restart verify.

systemd cutover to `/opt/fmp/current` is **not** performed automatically.
`cutover --apply` still refuses to install the live unit.

## 3. Host files that must already exist

- `/etc/fmp/fmp-dashboard.env` (required; missing file fails deploy)
- `/etc/fmp/fmp-writer.env` (preferred writer identity for migrations)
- `/etc/fmp/secrets/dashboard_readonly.pw` **or** the legacy
  `/root/FMP_SCREENER/.secrets/dashboard_readonly.pw` (copied, not deleted,
  on first migrate)
- Existing `/root/FMP_SCREENER` checkout, virtualenv, systemd unit, cron/timers
- Official research rows already in PostgreSQL
- GitHub secrets: `DO_SSH_KEY`, `DO_SSH_KNOWN_HOSTS`, `DO_HOST`, `DO_USER`

Human must confirm live droplet SQL files for migrations 001–019 match
`db/migration_checksum_baseline.json` (bound to `origin/main`
`2ed4da99df28d06e7730e56601de440e870dc823`). Cursor could not see the
production deployed SHA.

## 4. Database changes on first #29 deploy

- Add `schema_migrations.sha256` if missing.
- Backfill NULL hashes **only** when filename + current file hash match the
  trusted baseline. Mismatch or unknown historical file → hard fail.
- Apply new additive migrations 020–025 with hashes written at apply time.
- Repair `dashboard_readonly` to least privilege, or fail closed (including
  PUBLIC-inherited CREATE).

There is no `MIGRATIONS_BACKFILL_SHA256` bless-current-files flag.

## 5. Fail-closed deploy conditions

- Checkout SHA mismatch
- Missing `/etc/fmp/fmp-dashboard.env`
- Trusted baseline mismatch / unknown historical migration / drifted applied SQL
- Dashboard identity verify failed (privilege proof, not constraint false-positive)
- Host audit `readonly_unproven` / writer keys / provider fetch
- Official CSFML / Stage 1 / TLT identity refused (exit 2/4)
- Staged release missing verify script or bootable venv
- Missing pinned SSH known_hosts

## 6. What is and is not rolled back

Rolled back / left unchanged on failure before restart:

- The existing running Streamlit unit is not switched if identity verify fails
  before `systemctl restart`.
- `/opt/fmp/previous` remains the prior staged release when a new stage exists.

Not automatically rolled back:

- Additive DB migrations that already committed. Old application code must
  coexist with the new schema. Rollback of the app is `scripts/deploy_release.sh --rollback`
  (symlink only) plus a human decision; it does not un-apply SQL.
- Half-applied checksum backfill cannot happen: NULL-sha adoption is one
  transaction with apply.

A staged `/opt/fmp/current` symlink is **not** a completed cutover.
Configured identity ≠ running-service identity. Only an inspected PID / cwd /
executable / env-file path proves what Streamlit is running.

## 7. Expected verification outputs

Configured identity (`jobs.audit_host_dashboard` / verify script):

- `dashboard_readonly_verify=ok`
- `readonly_proven=true` from live identity/privilege proof, not merely a
  password file
- `writer_fallback=false`, no writer keys in dashboard env
- `csfml_v1_label_integrity=CANNOT_RULE_OUT`
- `csfml_v1_rerun_authorized=false`

Observed running service (`jobs.observe_running_dashboard`):

- `observed_running_identity=recorded` with a live PID
- `observed_uses_opt_fmp_current=true` only after the unit actually points at
  `/opt/fmp/current`
- env **paths** listed; no URLs or passwords
- `running_service_identity_proven=true` only when the live PID was inspected

`cutover --apply` expected: `apply_status=refused_no_systemd_mutate`,
`systemd_mutated=false`. That is **not** a completed cutover.

## 8. Post-restart checks

After the auto-deploy restart of the existing unit:

- `systemctl is-active fmp-dashboard`
- post-restart `scripts/verify_dashboard_identity.sh`
- Strategy Monitor shows separate CSFML V1 statuses (provenance VERIFIED,
  historical integrity CANNOT_RULE_OUT, engineering IMPLEMENTED/TESTED,
  rerun NOT AUTHORIZED, economic_gate NOT_DEFINED, promotion
  HUMAN_REVIEW_REQUIRED, holdout LOCKED)
