#!/usr/bin/env python3
"""Regenerate live.json + compare series for Alejandro García's Pro landing page.

Fetches public-profile stats from the eToro Public API (plain HTTPS) and index
history from Yahoo Finance. Designed to run locally or in GitHub Actions.

Authentication (required for eToro; never printed):
  ETORO_API_KEY   -> header x-api-key   (Public API Key)
  ETORO_USER_KEY  -> header x-user-key  (User Key, Real, Read is enough)

Optional aliases: X_API_KEY / X_USER_KEY.
Base URL override: ETORO_API_BASE (default https://public-api.etoro.com)

Usage:
  python3 scripts/update_live.py                  # writes live.json.new
  python3 scripts/update_live.py --out /tmp/x.json
  python3 scripts/update_live.py --write          # replaces live.json if valid
  python3 scripts/update_live.py --dry-run        # fetch + validate, write nothing

This script never prints secret values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "live.json.new"
LIVE_PATH = ROOT / "live.json"
USERNAME = "alejandrogarciao"
DISPLAY_USERNAME = "AlejandroGarciaO"
CID = 28063388
PROFILE_URL = f"https://www.etoro.com/people/{USERNAME}"
ETORO_BASE_DEFAULT = "https://public-api.etoro.com"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5y&events=history"
INCEPTION = date(2022, 4, 16)
UA = "AlejandroGarciaO-ProLanding/1.0 (live.json updater)"

# Fallback industry labels when the instruments endpoint has no sector.
INDUSTRY_FALLBACK = {
    "NVDA": "Electronic Technology",
    "TSLA": "Electronic Technology",
    "GOOGL": "Technology Services",
    "MSFT": "Technology Services",
    "META": "Technology Services",
    "AMZN": "Retail Trade",
    "ISRG": "Health Technology",
    "FSRNQ": "Consumer Durables",
}


class ConfigError(SystemExit):
    pass


class FetchError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_keys() -> tuple[str, str]:
    api = os.environ.get("ETORO_API_KEY") or os.environ.get("X_API_KEY") or ""
    user = os.environ.get("ETORO_USER_KEY") or os.environ.get("X_USER_KEY") or ""
    api, user = api.strip(), user.strip()
    if not api or not user:
        raise ConfigError(
            "Missing ETORO_API_KEY / ETORO_USER_KEY (or X_API_KEY / X_USER_KEY). "
            "Set them in the environment or as GitHub Actions secrets. Values are never printed."
        )
    return api, user


def etoro_get(path: str, query: dict[str, str] | None = None, timeout: int = 45) -> Any:
    base = os.environ.get("ETORO_API_BASE", ETORO_BASE_DEFAULT).rstrip("/")
    url = base + path
    if query:
        url += ("&" if "?" in path else "?") + urlencode(query)
    api, user = load_keys()
    headers = {
        "x-api-key": api,
        "x-user-key": user,
        "x-request-id": str(uuid.uuid4()),
        "Accept": "application/json",
        "User-Agent": UA,
    }
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise FetchError(f"eToro GET {path} -> HTTP {e.code} {body}") from e
    except URLError as e:
        raise FetchError(f"eToro GET {path} -> {e.reason}") from e


def yahoo_closes(symbol: str) -> dict[str, float]:
    url = YAHOO_CHART.format(symbol=symbol)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise FetchError(f"Yahoo chart empty for {symbol}")
    r0 = result[0]
    ts = r0.get("timestamp") or []
    closes = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out: dict[str, float] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(int(t), tz=timezone.utc).date().isoformat()
        out[d] = float(c)
    if not out:
        raise FetchError(f"Yahoo chart had no closes for {symbol}")
    return out


def fmt_pct(v: float, signed: bool = True) -> str:
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def cls(v: float) -> str:
    if v > 0:
        return "positive"
    if v < 0:
        return "negative"
    return ""


def metric(v: float, signed: bool = True, extra_class: str | None = None) -> dict[str, Any]:
    return {"display": fmt_pct(v, signed=signed), "class": extra_class if extra_class is not None else cls(v), "raw": round(v, 2)}


def parse_d(s: str) -> date:
    return date.fromisoformat(s[:10])


def last_on_or_before(series: dict[str, float], day: date) -> tuple[str | None, float | None]:
    d = day
    for _ in range(14):
        key = d.isoformat()
        if key in series:
            return key, series[key]
        d -= timedelta(days=1)
    return None, None


def downsample(pts: list[dict[str, Any]], max_n: int = 200) -> list[dict[str, Any]]:
    if len(pts) <= max_n:
        return pts
    step = (len(pts) - 1) / (max_n - 1)
    out, used = [], set()
    for i in range(max_n):
        idx = int(round(i * step))
        if idx in used:
            continue
        used.add(idx)
        out.append(pts[idx])
    if pts[-1] not in out:
        out.append(pts[-1])
    return out


def portfolio_cum_from_gains(gains: list[tuple[date, float]], start: date, end: date) -> tuple[list[dict[str, Any]], float]:
    """gains are (date, pct) applied on that date, AFTER start baseline (v=0 at start)."""
    pts = [{"t": start.isoformat(), "v": 0.0}]
    nav = 1.0
    last_v = 0.0
    for d, g in gains:
        if d <= start or d > end:
            continue
        nav *= (1 + g / 100.0)
        last_v = round((nav - 1) * 100, 2)
        pts.append({"t": d.isoformat(), "v": last_v})
    if pts[-1]["t"] != end.isoformat():
        pts.append({"t": end.isoformat(), "v": last_v})
    return pts, last_v


def index_cum(px: dict[str, float], start: date, end: date) -> tuple[list[dict[str, Any]], float | None]:
    _, sval = last_on_or_before(px, start)
    if sval is None:
        return [], None
    pts = [{"t": start.isoformat(), "v": 0.0}]
    last_v = 0.0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.isoformat() in px:
            last_v = round((px[d.isoformat()] / sval - 1) * 100, 2)
            pts.append({"t": d.isoformat(), "v": last_v})
    if pts[-1]["t"] != end.isoformat():
        _, ev = last_on_or_before(px, end)
        last_v = round((ev / sval - 1) * 100, 2) if ev else last_v
        pts.append({"t": end.isoformat(), "v": last_v})
    return downsample(pts, 200), last_v


def monthly_to_plot_gains(monthly: list[tuple[date, float]], end: date) -> list[tuple[date, float]]:
    """Turn eToro monthly rows (period-start date + month gain) into (plot_date, gain)."""
    rows: list[tuple[date, float]] = []
    for i, (pstart, g) in enumerate(monthly):
        if i + 1 < len(monthly):
            plot = monthly[i + 1][0] - timedelta(days=1)
        else:
            plot = end
        if plot > end:
            plot = end
        rows.append((plot, g, pstart))  # type: ignore
    # re-pack as (plot, gain) but we need pstart for filtering in caller
    return rows  # type: ignore


def portfolio_cum_monthly(monthly: list[tuple[date, float]], start: date, end: date) -> tuple[list[dict[str, Any]], float]:
    pts = [{"t": start.isoformat(), "v": 0.0}]
    nav = 1.0
    last_v = 0.0
    n = len(monthly)
    for i, (pstart, g) in enumerate(monthly):
        if pstart < start:
            continue
        plot = (monthly[i + 1][0] - timedelta(days=1)) if i + 1 < n else end
        if plot > end:
            plot = end
        if plot < start:
            continue
        nav *= (1 + g / 100.0)
        last_v = round((nav - 1) * 100, 2)
        pts.append({"t": plot.isoformat(), "v": last_v})
    if pts[-1]["t"] != end.isoformat():
        pts.append({"t": end.isoformat(), "v": last_v})
    return pts, last_v


def fetch_rankings(period: str) -> dict[str, Any]:
    data = etoro_get(f"/api/v2/portfolios/{USERNAME}/rankings", {"period": period})
    row = (data or {}).get("data") or data or {}
    if not row:
        raise FetchError(f"empty rankings for period={period}")
    return row


def fetch_monthly_gains() -> list[tuple[date, float]]:
    data = etoro_get(f"/api/v1/user-info/people/{USERNAME}/gain")
    monthly = (data or {}).get("monthly") or []
    out = []
    for row in monthly:
        ts = row.get("timestamp") or row.get("date")
        g = row.get("gain")
        if ts is None or g is None:
            continue
        out.append((parse_d(ts), float(g)))
    out.sort(key=lambda x: x[0])
    return out


def fetch_daily_gains(min_d: date, max_d: date) -> list[tuple[date, float]]:
    """Fetch daily gains, chunked by calendar year to keep payloads modest."""
    out: list[tuple[date, float]] = []
    year = min_d.year
    while year <= max_d.year:
        a = max(min_d, date(year, 1, 1))
        b = min(max_d, date(year, 12, 31))
        data = etoro_get(
            f"/api/v1/user-info/people/{USERNAME}/daily-gain",
            {"minDate": a.isoformat(), "maxDate": b.isoformat(), "type": "Daily"},
        )
        rows = data if isinstance(data, list) else (data or {}).get("items") or []
        for row in rows:
            ts = row.get("timestamp") or row.get("date")
            g = row.get("gain")
            if ts is None or g is None:
                continue
            out.append((parse_d(ts), float(g)))
        year += 1
    out.sort(key=lambda x: x[0])
    # de-dupe by date, last wins
    dedup: dict[date, float] = {}
    for d, g in out:
        dedup[d] = g
    return sorted(dedup.items())


def fetch_live_portfolio() -> dict[str, Any]:
    return etoro_get(f"/api/v1/user-info/people/{USERNAME}/portfolio/live") or {}


def fetch_instruments(ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    # instrumentsIds is a comma-separated list (form, explode=false)
    data = etoro_get("/api/v2/market-data/instruments", {"instrumentsIds": ",".join(str(i) for i in ids)})
    results = (data or {}).get("results") or []
    return {int(r["instrumentId"]): r for r in results if r.get("instrumentId") is not None}


def gain_as_pct(row: dict[str, Any], field: str) -> float | None:
    """Rankings gains are decimal fractions (0.0438 = 4.38%)."""
    if field not in row or row[field] is None:
        return None
    v = float(row[field])
    return v * 100.0


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def build() -> dict[str, Any]:
    log("Fetching eToro rankings…")
    curr_year = fetch_rankings("CurrYear")
    curr_month = fetch_rankings("CurrMonth")
    two_year = fetch_rankings("AbsTwoYears")

    total = gain_as_pct(curr_year, "fiveYearGain")
    if total is None:
        total = gain_as_pct(curr_year, "gain")
    ytd = gain_as_pct(curr_year, "gain")
    this_month = gain_as_pct(curr_month, "gain")
    two_y = gain_as_pct(two_year, "gain")
    five_y = gain_as_pct(curr_year, "fiveYearGain")
    ann = gain_as_pct(curr_year, "annualizedReturn")
    # risk / DD / copiers: use CurrYear row (risk is current; DD all-time-in-window)
    risk = int(curr_year.get("riskScore") or 0)
    peak = curr_year.get("peakToValley")
    if peak is None:
        peak = two_year.get("peakToValley")
    max_dd = float(peak) if peak is not None else 0.0
    copiers = int(curr_year.get("copiers") or 0)
    auc_tier = curr_year.get("aumTierDesc") or two_year.get("aumTierDesc") or "$100K-$300K"
    # never keep exact AUC dollars
    if not isinstance(auc_tier, str) or not re.search(r"K", auc_tier, re.I):
        auc_tier = "$100K-$300K"

    if any(v is None for v in (total, ytd, this_month, two_y, five_y, ann)):
        missing = [n for n, v in [
            ("total", total), ("ytd", ytd), ("this_month", this_month),
            ("two_year", two_y), ("five_year", five_y), ("annualized", ann),
        ] if v is None]
        raise FetchError(f"missing ranking fields: {missing}")

    log("Fetching monthly + daily gains…")
    monthly = fetch_monthly_gains()
    end = today_utc()
    # Prefer last Yahoo/eToro common close; daily gains often exist through yesterday.
    daily = fetch_daily_gains(INCEPTION, end)

    log("Fetching live portfolio + instruments…")
    portfolio = fetch_live_portfolio()
    cash_raw = portfolio.get("realizedCreditPct")
    positions = portfolio.get("positions") or []
    ids = [int(p["instrumentId"]) for p in positions if p.get("instrumentId") is not None]
    inst_map = fetch_instruments(ids)

    holdings = []
    sector_acc: dict[str, float] = {}
    invested = 0.0
    for p in positions:
        iid = int(p.get("instrumentId") or 0)
        weight = float(p.get("investmentPct") or 0.0)
        invested += weight
        info = inst_map.get(iid) or {}
        symbol = info.get("symbol") or str(iid)
        name = info.get("displayName") or symbol
        industry = INDUSTRY_FALLBACK.get(symbol, "Other")
        holdings.append({
            "symbol": symbol,
            "name": name,
            "weight_pct": round(weight, 2),
            "industry": industry,
        })
        sector_acc[industry] = sector_acc.get(industry, 0.0) + weight
    holdings.sort(key=lambda h: h["weight_pct"], reverse=True)

    if cash_raw is not None:
        cash = float(cash_raw)
    else:
        cash = max(0.0, 100.0 - invested)

    sectors = [{
        "name": "Cash",
        "pct": round(cash, 2),
        "style": "background:linear-gradient(90deg, var(--amber), var(--gold));",
    }]
    for name, pct in sorted(sector_acc.items(), key=lambda kv: kv[1], reverse=True):
        sectors.append({"name": name, "pct": round(pct, 2), "style": ""})

    log("Fetching Yahoo index history (^GSPC, ^NDX)…")
    spx = yahoo_closes("%5EGSPC")
    ndx = yahoo_closes("%5ENDX")

    # Align as_of to the latest date all three sources can support.
    daily_last = daily[-1][0] if daily else end
    _, spx_last_d = (None, None)
    spx_dates = [parse_d(k) for k in spx]
    ndx_dates = [parse_d(k) for k in ndx]
    spx_max = max(spx_dates) if spx_dates else end
    ndx_max = max(ndx_dates) if ndx_dates else end
    as_of = min(end, daily_last, spx_max, ndx_max)

    ranges_spec = {
        "1W": ("daily", as_of - timedelta(days=7)),
        "1M": ("daily", as_of - timedelta(days=30)),
        "6M": ("monthly", as_of - timedelta(days=183)),
        "1Y": ("monthly", as_of - timedelta(days=365)),
        "5Y": ("monthly", INCEPTION),
    }

    compare_ranges: dict[str, Any] = {}
    for name, (kind, start) in ranges_spec.items():
        if start < INCEPTION and name != "5Y":
            start = INCEPTION
        if start > as_of:
            start = as_of
        if kind == "daily" and daily:
            port, port_end = portfolio_cum_from_gains(daily, start, as_of)
        else:
            port, port_end = portfolio_cum_monthly(monthly, start, as_of)
        sp_pts, sp_end = index_cum(spx, start, as_of)
        nq_pts, nq_end = index_cum(ndx, start, as_of)
        compare_ranges[name] = {
            "series": {
                "portfolio": port,
                "spx500": sp_pts,
                "nsdq100": nq_pts,
            },
            "end_pct": {
                "portfolio": round(port_end, 2),
                "spx500": round(sp_end, 2) if sp_end is not None else None,
                "nsdq100": round(nq_end, 2) if nq_end is not None else None,
            },
        }

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    annual = {}
    # yearly from monthly payload if present — re-fetch is already in gain()
    # We'll compute calendar years from monthly for raw.annual_from_monthly
    by_year: dict[int, list[float]] = {}
    for d, g in monthly:
        by_year.setdefault(d.year, []).append(g)
    for y, gs in by_year.items():
        nav = 1.0
        for g in gs:
            nav *= (1 + g / 100.0)
        annual[str(y)] = round((nav - 1) * 100, 2)

    live = {
        "username": DISPLAY_USERNAME,
        "cid": CID,
        "updated_at": now,
        "source": "etoro-public-api",
        "profile_url": PROFILE_URL,
        "metrics": {
            "total_return": metric(total),  # type: ignore
            "ytd": metric(ytd),  # type: ignore
            "ytd_label_year": str(as_of.year),
            "risk_score": {"display": f"{risk}/10", "class": "", "raw": risk},
            "max_drawdown": metric(max_dd),
            "copiers": {"display": str(copiers), "class": "", "raw": copiers},
            "auc": {
                "display": auc_tier,
                "class": "",
                "tier": auc_tier,
            },
            "this_month": metric(this_month),  # type: ignore
            "two_year": metric(two_y),  # type: ignore
            "five_year": metric(five_y),  # type: ignore
            "annualized": metric(ann),  # type: ignore
            "cash": {"display": f"{cash:.2f}%", "class": "warning", "raw": round(cash, 2)},
        },
        "community": {
            "copiers": copiers,
            "auc_display": auc_tier,
        },
        "sectors": sectors,
        "holdings": holdings,
        "raw": {
            "copiers_history_latest": copiers,
            "rankings_curr_year": {
                "Gain": round(ytd, 2),  # type: ignore
                "Copiers": copiers,
                "RiskScore": risk,
                "PeakToValley": round(max_dd, 2),
                "AnnualizedReturn": round(ann, 2),  # type: ignore
                "FiveYearGain": round(five_y, 2),  # type: ignore
                "AUMTierDesc": auc_tier,
            },
            "annual_from_monthly": annual,
        },
        "compare": {
            "updated_at": now,
            "as_of": as_of.isoformat(),
            "default_range": "1Y",
            "sample": False,
            "notes": {
                "portfolio": (
                    "Real eToro public monthly gains (5Y/1Y/6M) and daily gains (1W/1M) for "
                    f"{USERNAME} via GET /api/v1/user-info/people/{{username}}/gain and daily-gain. "
                    f"5Y starts at account inception {INCEPTION.isoformat()} (account younger than 5 years)."
                ),
                "indices": (
                    "Real daily closes for ^GSPC (SPX500) and ^NDX (Nasdaq 100) from Yahoo Finance. "
                    "Not sample data."
                ),
            },
            "ranges": compare_ranges,
        },
    }
    return live


AUC_DOLLAR_RE = re.compile(r"\$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?")  # $116,302 style, not $100K


def validate(live: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for k in ("username", "metrics", "holdings", "sectors", "compare", "updated_at"):
        if k not in live:
            errs.append(f"missing top-level key {k}")
    m = live.get("metrics") or {}
    for k in ("total_return", "ytd", "risk_score", "max_drawdown", "copiers", "auc",
              "this_month", "two_year", "five_year", "annualized", "cash"):
        if k not in m:
            errs.append(f"missing metrics.{k}")
    auc = m.get("auc") or {}
    tier = str(auc.get("tier") or auc.get("display") or "")
    if "K" not in tier.upper() or not tier.startswith("$"):
        errs.append(f"AUC is not a tier band: {tier!r}")
    if "raw" in auc and isinstance(auc["raw"], (int, float)) and auc["raw"] > 1000:
        errs.append("AUC raw exact dollars must not be stored")
    blob = json.dumps(live)
    if AUC_DOLLAR_RE.search(blob):
        errs.append("document appears to contain an exact dollar amount like $116,302")
    if "116302" in blob:
        errs.append("document contains a forbidden exact AUC figure")
    cmp_ = live.get("compare") or {}
    ranges = cmp_.get("ranges") or {}
    for name in ("1W", "1M", "6M", "1Y", "5Y"):
        if name not in ranges:
            errs.append(f"missing compare range {name}")
            continue
        r = ranges[name]
        series = (r.get("series") or {})
        for s in ("portfolio", "spx500", "nsdq100"):
            pts = series.get(s) or []
            if not pts:
                errs.append(f"{name}.{s} series empty")
                continue
            if pts[0].get("v") != 0:
                errs.append(f"{name}.{s} does not start at 0 (v={pts[0].get('v')})")
        ep = r.get("end_pct") or {}
        for s in ("portfolio", "spx500", "nsdq100"):
            if not isinstance(ep.get(s), (int, float)):
                errs.append(f"{name}.end_pct.{s} not numeric")
    if cmp_.get("sample") is not False:
        errs.append("compare.sample should be false for a live update")
    cop = m.get("copiers") or {}
    if not isinstance(cop.get("raw"), int) or cop["raw"] < 0:
        errs.append("copiers.raw invalid")
    tr = (m.get("total_return") or {}).get("raw")
    if not isinstance(tr, (int, float)):
        errs.append("total_return.raw invalid")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output path (default: live.json.new)")
    ap.add_argument("--write", action="store_true", help="replace live.json after validation (backup to live.json.bak)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and validate only; write no files")
    args = ap.parse_args()

    try:
        live = build()
    except ConfigError as e:
        log(str(e))
        return 2
    except FetchError as e:
        log(f"FETCH FAILED: {e}")
        return 1

    errs = validate(live)
    if errs:
        log("VALIDATION FAILED:")
        for e in errs:
            log(f"  - {e}")
        return 3

    # brief non-secret summary
    m = live["metrics"]
    log(
        "OK  total={total} ytd={ytd} month={month} risk={risk} copiers={cop} cash={cash} "
        "1Y port={p} spx={s} ndx={n} as_of={as_of}".format(
            total=m["total_return"]["display"],
            ytd=m["ytd"]["display"],
            month=m["this_month"]["display"],
            risk=m["risk_score"]["display"],
            cop=m["copiers"]["display"],
            cash=m["cash"]["display"],
            p=live["compare"]["ranges"]["1Y"]["end_pct"]["portfolio"],
            s=live["compare"]["ranges"]["1Y"]["end_pct"]["spx500"],
            n=live["compare"]["ranges"]["1Y"]["end_pct"]["nsdq100"],
            as_of=live["compare"]["as_of"],
        )
    )

    if args.dry_run:
        log("dry-run: no files written")
        return 0

    if args.write:
        dest = LIVE_PATH
        if dest.exists():
            bak = dest.with_suffix(".json.bak")
            bak.write_text(dest.read_text())
            log(f"backed up {dest} -> {bak}")
    else:
        dest = args.out

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
    log(f"wrote {dest} ({dest.stat().st_size} bytes)")
    if not args.write:
        log("live.json was not overwritten (pass --write to replace it after a good run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
