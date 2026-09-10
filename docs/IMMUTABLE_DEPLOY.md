# Immutable release deployment

Production today still uses `.github/workflows/deploy.yml` (`git pull --ff-only` in `/root/FMP_SCREENER`). That path stays until `scripts/deploy_release.sh` is validated on the host. Do not delete the git-pull flow.

## Target layout

```
/opt/fmp/releases/<git_sha>/   # immutable checkout of that SHA
/opt/fmp/current               # atomic symlink to the active release
/opt/fmp/previous              # previous successful release
/etc/fmp/...                   # host config (not in Git)
/var/lib/fmp/...               # host state (not in Git)
/var/log/fmp/...               # logs
```

Normal upgrades do not `git reset --hard` the live checkout.

## Host state (`/var/lib/fmp`)

Release trees are replaceable. Host state is not.

| Path | Owner | Contents |
|---|---|---|
| `/var/lib/fmp/streamlit` | Streamlit unit | Streamlit cache / local data files |
| `/var/lib/fmp/outputs` | Streamlit / jobs | optional host-local outputs if not using the checkout `outputs/` |
| `/var/lib/fmp/deploy` | deploy script | last successful SHA, rollback pointer notes |
| `/etc/fmp/fmp-dashboard.env` | root 0600 | `DASHBOARD_READONLY_URL` only; no writer URL |
| `/etc/fmp/market_intelligence.env` | root 0600 | MI writer / FRED / FINRA (separate from Streamlit) |
| `/root/FMP_SCREENER/.secrets/dashboard_readonly.pw` | root 0600 | used by `scripts/provision_dashboard_readonly.sh` (everyday deploy and MI activate) |
| `/var/log/fmp` | services | dashboard / deploy logs |

The Streamlit unit must mount `ReadWritePaths=/var/lib/fmp /var/log/fmp`. See `deploy/fmp-dashboard.service.example`.

## Streamlit identity

`DASHBOARD_READONLY_URL` is required for Strategy Monitor. It is **not** `mi_readonly` (that role cannot SELECT `strategies` / `backtests`). If the URL is unset, the page fails closed unless `DASHBOARD_ALLOW_WRITER_FALLBACK=1` is set as a temporary local escape. Deploy treats that escape as failure (exit 4).

Everyday deploy and `deploy_release.sh` run `scripts/provision_dashboard_readonly.sh --require` after migrations, then `scripts/verify_dashboard_identity.sh`. The password file at `/root/FMP_SCREENER/.secrets/dashboard_readonly.pw` is required. The provision script applies `db/roles/dashboard_readonly.sql` via admin URL or local postgres peer (never the dashboard writer) and writes `DASHBOARD_READONLY_URL` into `/etc/fmp/fmp-dashboard.env` and `/root/FMP_SCREENER/.env` without printing values. After writing, it removes writer keys (`DATABASE_URL`, `DB_*`, `MARKET_INTELLIGENCE_DATABASE_URL`, `DASHBOARD_ALLOW_WRITER_FALLBACK`) from the systemd Streamlit env only; the checkout `.env` keeps writer keys for ingest/CLI. A missing password file fails deploy (exit 3). `DASHBOARD_ALLOW_WRITER_FALLBACK=1` fails deploy (exit 4). `STREAMLIT_ALLOW_PROVIDER_FETCH=1` fails deploy (exit 5). Neither escape is a production path.

Verify sources `/etc/fmp/fmp-dashboard.env` only (`FMP_IDENTITY_ENV_ONLY=1`). It does not source `/root/FMP_SCREENER/.env`. Inherited writer keys (`DATABASE_URL`, `DB_*`, `MARKET_INTELLIGENCE_DATABASE_URL`) from a parent shell are unset before that load so `activate_market_intelligence_host.sh` can verify identity after `load_writer_env`. Writer keys that reappear from the dashboard env file fail closed (exit 4). Exit 2, 3, 4, or 5 fails deploy. Everyday deploy also runs `python -m qc_research.contracts.digests` before migrations.

`scripts/deploy_release.sh` always fetches and checks out the requested SHA, then fail-closes if `git rev-parse HEAD` does not match. A leftover tree under `/opt/fmp/releases/<sha>/` is not trusted as that SHA.

If `/opt/fmp/releases` exists, everyday deploy also populates an immutable release tree for that SHA (`--skip-restart --skip-preflight` unless `FMP_IMMUTABLE_RELEASE_STRICT=1`, which also runs pip / migrations / pytest on the release checkout). `--skip-preflight` does **not** skip contract-digest, `provision_dashboard_readonly.sh --require`, or identity verify on the release tree. After a successful populate, deploy re-runs identity verify from `/opt/fmp/current` and fails closed if that script is missing (exit 6) or verify fails. A populate failure now fails deploy (the git-pull tree is not a substitute once the immutable layout exists). Hosts that have not created `/opt/fmp/releases` are unchanged. Systemd still runs from `/root/FMP_SCREENER` until cutover is validated on the host. Live platform ingest and production verify prefer `/opt/fmp/current` for Python `CODE_ROOT` when that tree contains the ingest/verify modules, and fall back to `/root/FMP_SCREENER` only if the immutable link is absent. Cutover readiness stays dry-run and is not ready until the proposed unit's `/opt/fmp/current/venv/bin/streamlit` exists (everyday `--skip-preflight` populate does not create that venv). Deploy writes `/var/lib/fmp/deploy/current.json` (override with `FMP_DEPLOY_STATE`) with SHA, checkout path, immutable populate status, and `readonly_proven` from identity verify. A writer subshell then persists that sanitized record to `mi_deploy_host_identity` so Data Health can SELECT `dashboard_readonly_proven` / systemd path / immutable SHA through `mi_v_ops_status` without reading host JSON. After that it writes `/var/lib/fmp/deploy/host_audit.json` via `jobs.audit_host_dashboard --require-readonly` (sourced from `/etc/fmp/fmp-dashboard.env` only, not checkout `.env`) and fails closed before restarting Streamlit if the audit is unproven. The files and the database row must not contain URLs or passwords.

## Rollback

```
scripts/deploy_release.sh --rollback
```

points `/opt/fmp/current` at the previous successful release and restarts Streamlit (unless `--skip-restart`).

## Migrations

Migrations are additive. `jobs/apply_migrations.py` records `filename` + `sha256`. Changing an already-applied file fails closed. There is no automatic SQL down-migration; rollback of application code is supported, schema rollback is not.

## Secrets

Secrets stay in `/etc/fmp` and `/root/FMP_SCREENER/.secrets`. Release trees must not contain host-modified tracked env files.

## Local / pre-cutover checks

```
scripts/deploy_release.sh --sha <git_sha> --skip-restart --skip-preflight --skip-identity
python -m jobs.cutover_dashboard_systemd --verify-rc 0 --out /var/lib/fmp/deploy/cutover_readiness.json
```

`--skip-preflight` skips pip, migrations, and pytest. `--skip-identity` also skips contract digests, read-only provision, and identity verify (layout-only). Host cutover still requires the full preflight.

`jobs.cutover_dashboard_systemd` is dry-run by default. Everyday deploy records `/var/lib/fmp/deploy/cutover_readiness.json` and does **not** pass `--apply` or `--require-ready`. Missing `/opt/fmp/current` is recorded, not a deploy failure. Writer keys in `/etc/fmp/fmp-dashboard.env` fail closed (exit 4) before Streamlit restart. `--apply` still does not call `systemctl`; it refuses unless `FMP_ALLOW_SYSTEMD_CUTOVER=1`, and even then it will not mutate the live unit. Install the proposed unit (`--write-unit`) only after host identity is proven.
