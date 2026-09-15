# DigitalOcean secret provisioning (not activation)

This is a **separate** operator path from merging Market Intelligence or enabling
the weekday refresh timer. It places provider credentials on the writer host
without changing production behaviour.

Do **not** run provider ingest, enable `fmp-mi-refresh.timer`, restart Streamlit,
or treat credential presence as READY / AVAILABLE.

## What is already there

`deploy.yml` still deploys only on push to `main`. It does not install Market
Intelligence timers and does not write provider secrets. The host already has
`/root/FMP_SCREENER`, the dashboard service, and (after a merge) additive `mi_*`
migrations. The env file, if present, is `/etc/fmp/market_intelligence.env`
(mode 0600).

GitHub Actions repository secrets may hold `EIA_API_KEY`, `OPENFIGI_API_KEY`,
and `SEC_USER_AGENT`. Their existence in GitHub does **not** prove they reach the
ingestion runtime. Transfer them with the workflow below (or the host script).

## GitHub Actions path (preferred when secrets live only in GitHub)

```text
Actions → Provision MI provider keys (not activation) → Run workflow
```

Workflow file: `.github/workflows/provision_mi_provider_keys.yml`

- Uses pinned SSH trust (`DO_SSH_KEY` + `DO_SSH_KNOWN_HOSTS`)
- Writes `/root/FMP_SCREENER/.secrets/{eia_api_key,openfigi_api_key,sec_user_agent}`
- Upserts into `/etc/fmp/market_intelligence.env` via
  `scripts/provision_digitalocean_mi_secrets.sh`
- Preserves existing non-placeholder values unless `rotate=true`
- Refuses empty GitHub values (does not clobber a valid host secret with blank)
- Never prints secret values; never restarts services; never enables ingest flags

## Host script (when key files are already on the droplet)

```bash
scripts/provision_digitalocean_mi_secrets.sh \
  --eia-key-file /root/FMP_SCREENER/.secrets/eia_api_key \
  --openfigi-key-file /root/FMP_SCREENER/.secrets/openfigi_api_key \
  --sec-ua-file /root/FMP_SCREENER/.secrets/sec_user_agent

scripts/provision_digitalocean_mi_secrets.sh --apply \
  --eia-key-file /root/FMP_SCREENER/.secrets/eia_api_key \
  --openfigi-key-file /root/FMP_SCREENER/.secrets/openfigi_api_key \
  --sec-ua-file /root/FMP_SCREENER/.secrets/sec_user_agent
```

`SEC_USER_AGENT` may contain spaces; the provisioner quotes it safely.
API keys must remain single tokens without whitespace.

## Activation remains a later human step

Credential presence is configuration only. Bounded validation examples:

- EIA: `python -m jobs.market_intelligence_refresh --eia --json` on the writer host
- OpenFIGI: set `MI_OPENFIGI_ENABLED=1` for one bounded run, then unset
- SEC: set `MI_EDGAR_ENABLED=1` for one Apple CIK run (`--edgar`), then unset

MSRB/EMMA purchase is deferred; the adapter remains registered but is omitted from
Data Health. Manual EMMA website review + Fixed Income calculator stay available.
