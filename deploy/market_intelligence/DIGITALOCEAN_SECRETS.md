# DigitalOcean secret provisioning (not activation)

This is a **separate** operator path from merging Market Intelligence or enabling
the weekday refresh timer. It exists so `FRED_API_KEY` can be placed on the
existing host without changing production behaviour.

Do **not** run a FRED ingest, enable `fmp-mi-refresh.timer`, restart Streamlit,
or treat this as go-live.

## What is already there

`deploy.yml` still deploys only on push to `main`. It does not install Market
Intelligence timers and does not write provider secrets. The host already has
`/root/FMP_SCREENER`, the dashboard service, and (after a merge) additive `mi_*`
migrations. The env file, if present, is `/etc/fmp/market_intelligence.env`
(mode 0600).

## Prepare the key on the host

Write the FRED key to a protected file. Never put it on a command line, in
`git`, or in a chat transcript.

```bash
install -d -m 0700 /root/FMP_SCREENER/.secrets
install -m 0600 /dev/null /root/FMP_SCREENER/.secrets/fred_api_key
# paste the key into that file with an editor; one line, no quotes
```

## Dry run (default)

From the checkout:

```bash
scripts/provision_digitalocean_mi_secrets.sh \
  --fred-key-file /root/FMP_SCREENER/.secrets/fred_api_key
```

Prints the planned edit. Does not write the env file, does not restart anything.

## Apply (still not activation)

```bash
scripts/provision_digitalocean_mi_secrets.sh --apply \
  --fred-key-file /root/FMP_SCREENER/.secrets/fred_api_key
```

- Creates `/etc/fmp/market_intelligence.env` from the example if it is missing
  (still placeholders for writer DB / API token).
- Sets `FRED_API_KEY` from the protected file.
- Leaves every other existing assignment untouched.
- Does not enable timers, does not run `market_intelligence_refresh`, does not
  open the API to the network.

Fill writer identity and `AI_CONTEXT_API_TOKEN` by editing the env file later.
Provision `mi_readonly` only when you are ready for pages/API reads
(`docs/MARKET_INTELLIGENCE.md`).

## Activation remains a later human step

When you decide to go live, follow the operator steps in
`docs/MARKET_INTELLIGENCE.md` (probe-config, dry-run, `--fred --mode full`,
analytics backfill, morning snapshot, then timers). Those commands are
intentionally not invoked here.
