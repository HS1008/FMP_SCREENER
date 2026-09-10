# Production release plan (authorization required)

This plan is reviewable evidence. It does **not** merge PRs, dispatch Deploy,
install systemd, or mutate production.

## 1. Pull requests and order

1. **HS1008/quant-strategies #28** → `research-integration`
   (`cursor/research-architecture-674b`). Merge first. CSFML label-contract
   producer.
2. **HS1008/fmp_screener #29** → `main`
   (`cursor/research-architecture-674b`). Merge second. Dashboard ingest,
   Monitor, host verification. Consumer pin is unchanged unless
   `required_by_kind` / integrity digest actually change (they do not in this
   milestone).

Do not merge platform-MI into this line. Do not launch QuantConnect.

## 2. Preconditions (names only)

- GitHub write access to merge the two PRs.
- Self-hosted Deploy runner on the production droplet.
- PostgreSQL roles: `dashboard_readonly` (Streamlit), writer role used only
  by ingest/MI jobs (not Streamlit).
- Host files: `/etc/fmp/fmp-dashboard.env`, `/etc/fmp/fmp-writer.env` (create
  if absent), `/root/FMP_SCREENER/.secrets/dashboard_readonly.pw`.
- Credentials by name only: `DASHBOARD_READONLY_URL`, writer URL in
  `fmp-writer.env`, no Streamlit writer fallback.

## 3. Host preparation and deploy

Everyday path after FMP #29 is on `main` (human dispatches Deploy):

```bash
# on the droplet, Deploy workflow already does the equivalent
bash scripts/provision_dashboard_readonly.sh --require
FMP_IDENTITY_ENV_ONLY=1 FMP_DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env \
  bash scripts/verify_dashboard_identity.sh
python -m jobs.audit_host_dashboard --require-readonly \
  --env-file /etc/fmp/fmp-dashboard.env \
  --out /var/lib/fmp/deploy/host_audit.json
python -m jobs.observe_running_dashboard \
  --out /var/lib/fmp/deploy/running_observation.json
python -m jobs.cutover_dashboard_systemd \
  --env-file /etc/fmp/fmp-dashboard.env \
  --out /var/lib/fmp/deploy/cutover_readiness.json
# cutover --apply still does not install the live unit
```

Immutable layout (optional, separate from everyday git-pull):

```bash
scripts/deploy_release.sh --sha <merged_main_sha>
```

## 4. Expected verification outputs

Configured identity (`jobs.audit_host_dashboard` / verify script):

- `dashboard_readonly_verify=ok`
- `readonly_proven=true`
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

## 5. Post-restart checks

After a human `systemctl restart fmp-dashboard`:

- Re-run identity verify against `/etc/fmp/fmp-dashboard.env`.
- Re-run `jobs.observe_running_dashboard` and confirm PID/cwd/exe/code SHA.
- Live CSFML / TLT / Stage 1 query-back (existing verify jobs).
- Confirm official research identities already in PostgreSQL were not
  overwritten.

Do not require `FMP_STREAMLIT_READONLY` to appear in `/proc/PID/environ`.

## 6. Failure handling and rollback

- If `readonly_proven` is false: do not restart; do not install the proposed
  unit.
- If writer keys appear in the dashboard env: refuse (`exit 4`).
- Everyday rollback: remain on `/root/FMP_SCREENER` git-pull unit.
- Immutable rollback: `scripts/deploy_release.sh --rollback`.

## 7. Operations that need production authorization

- Merge #28 / #29.
- Dispatch `.github/workflows/deploy.yml` on `main`.
- Create `/etc/fmp/fmp-writer.env` or change host env files.
- Install the proposed systemd unit (cutover tool will not do this).
- `systemctl restart` / enable.
- Production write-denial probes (exercise on disposable PostgreSQL first).
- Any QuantConnect job, including smoke.
- The separate pre-2025 V1 impact investigation
  (`quant-strategies/research/stage2/PROPOSED_V1_IMPACT_INVESTIGATION.md`).
