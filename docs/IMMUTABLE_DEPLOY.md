# Immutable release deployment

Production today still uses `.github/workflows/deploy.yml` (`git pull --ff-only` in `/root/FMP_SCREENER`). That path stays until `scripts/deploy_release.sh` is validated on the host.

## Target layout

```
/opt/fmp/releases/<git_sha>/   # immutable checkout
/opt/fmp/current               # atomic symlink
/opt/fmp/previous              # previous successful release
/etc/fmp/...                   # host config (not in Git)
/var/lib/fmp/...               # host state
/var/log/fmp/...               # logs
```

Normal upgrades do not `git reset --hard` the live checkout.

## Rollback

```
scripts/deploy_release.sh --rollback
```

points `/opt/fmp/current` at the previous successful release and restarts Streamlit.

## Migrations

Migrations are additive. `jobs/apply_migrations.py` records `filename` + `sha256`. Changing an already-applied file fails closed. There is no automatic SQL down-migration; rollback of application code is supported, schema rollback is not.

## Secrets

Secrets stay in `/etc/fmp` and `/root/FMP_SCREENER/.secrets`. Release trees must not contain host-modified tracked env files.
