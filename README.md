# Alejandro García – eToro Pro Investor landing

Public landing page for [Alejandro García](https://www.etoro.com/people/alejandrogarciao) (eToro username `AlejandroGarciaO`).

Live site: https://alejogarcia9213-tech.github.io/alejandrogarcia-pro/

Past performance is not an indication of future results. This page does not guarantee returns or that the strategy will beat any market.

## Daily stats

`scripts/update_live.py` regenerates `live.json` (headline stats, holdings, and the compare-chart series) from the eToro Public API, plus Yahoo Finance for SPX500 / Nasdaq 100. A GitHub Action runs it daily at 05:30 UTC, and can be run on demand from the Actions tab.

### Required Actions secrets

Add both secrets here (names must match exactly):

https://github.com/alejogarcia9213-tech/alejandrogarcia-pro/settings/secrets/actions

- `ETORO_API_KEY` — Public API key (sent as `x-api-key`)
- `ETORO_USER_KEY` — User key (sent as `x-user-key`)

Mint **Real, Read-only** keys in eToro: Settings → Trading → API Key Management. Do not use write or demo keys.

Until both secrets are present, the workflow prints a notice and exits successfully without changing `live.json`.

### Run locally

```bash
export ETORO_API_KEY=   # your read-only key
export ETORO_USER_KEY=  # your read-only user key
python3 scripts/update_live.py          # writes live.json.new
python3 scripts/update_live.py --write  # replaces live.json after validation
```

The updater never writes exact Assets under Copy dollar amounts — only the public AUC tier band.
