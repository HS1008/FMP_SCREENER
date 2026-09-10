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
| `/root/FMP_SCREENER/.secrets/dashboard_readonly.pw` | root 0600 | used by `scripts/activate_market_intelligence_host.sh` to provision the role |
| `/var/log/fmp` | services | dashboard / deploy logs |

The Streamlit unit must mount `ReadWritePaths=/var/lib/fmp /var/log/fmp`. See `deploy/fmp-dashboard.service.example`.

## Streamlit identity

`DASHBOARD_READONLY_URL` is required for Strategy Monitor. It is **not** `mi_readonly` (that role cannot SELECT `strategies` / `backtests`). If the URL is unset, the page fails closed unless `DASHBOARD_ALLOW_WRITER_FALLBACK=1` is set as a temporary host escape. Deploy runs `python -m jobs.verify_dashboard_readonly` after migrations (exit 2 fails deploy; exit 3 means unset / skip).

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
scripts/deploy_release.sh --sha <git_sha> --skip-restart --skip-preflight
```

`--skip-preflight` skips pip, migrations, pytest, and the read-only verify. Use only to exercise symlink layout. Host cutover still requires the full preflight.
