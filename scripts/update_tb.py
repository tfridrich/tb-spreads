"""
Daily update of Austrian and German-Luxembourg day-ahead TB spreads.

Fetches SDAC day-ahead prices from the ENTSO-E Transparency Platform API,
averages them to hourly prices (since Oct 2025 the auction clears in 15-min
products), calculates TB1/TB2/TB4 per delivery day and merges the result into
docs/data/tb_daily.json, which the web page reads.

TBx = mean of the x highest hourly prices minus mean of the x lowest (EUR/MWh).

Usage:
    ENTSOE_API_KEY=... python scripts/update_tb.py               # refresh recent days
    ENTSOE_API_KEY=... python scripts/update_tb.py --since 2019-01-01   # full rebuild

Only the Python standard library is used, so nothing needs installing.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

API_URL = "https://web-api.tp.entsoe.eu/api"
MARKETS = {  # bidding zone -> EIC code
    "AT": "10YAT-APG------L",
    "DE_LU": "10Y1001A1001A82H",
}
MARKET_TZ = ZoneInfo("Europe/Vienna")  # same clock as Berlin; SDAC delivery days follow CET/CEST
DATA_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "tb_daily.json"
REFRESH_DAYS = 5  # re-fetch the last few stored days in case ENTSO-E corrected them
RESOLUTION_MIN = {"PT15M": 15, "PT30M": 30, "PT60M": 60}


def iso_to_utc(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)


def fetch_xml(token, eic, start_utc, end_utc, retries=3):
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": start_utc.strftime("%Y%m%d%H%M"),
        "periodEnd": end_utc.strftime("%Y%m%d%H%M"),
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 400 and b"No matching data" in body:
                return body  # acknowledgement document: no data in window
            if e.code == 401:
                sys.exit("ENTSO-E rejected the API key (HTTP 401). Check the ENTSOE_API_KEY secret.")
            if attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def parse_prices(xml_bytes):
    """Return {utc_datetime: price} for the SDAC auction (classification sequence 1).

    Handles 15/30/60-minute resolution and curve type A03, where a missing
    position means the price is unchanged from the previous position.
    """
    root = ET.fromstring(xml_bytes)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        return {}
    prices = {}
    for ts in root.iter(f"{ns}TimeSeries"):
        seq = ts.find(f"{ns}classificationSequence_AttributeInstanceComponent.position")
        if seq is not None and seq.text and seq.text.strip() != "1":
            continue  # skip other sequences (e.g. EXAA auction published as sequence 2)
        for period in ts.iter(f"{ns}Period"):
            start = iso_to_utc(period.find(f"{ns}timeInterval/{ns}start").text)
            end = iso_to_utc(period.find(f"{ns}timeInterval/{ns}end").text)
            res = period.find(f"{ns}resolution").text.strip()
            step = RESOLUTION_MIN.get(res)
            if step is None:
                raise ValueError(f"Unexpected resolution {res}")
            n = int((end - start).total_seconds() // (step * 60))
            values = [None] * n
            for pt in period.iter(f"{ns}Point"):
                pos = int(pt.find(f"{ns}position").text)
                if 1 <= pos <= n:
                    values[pos - 1] = float(pt.find(f"{ns}price.amount").text)
            last = None
            for i in range(n):
                if values[i] is None:
                    values[i] = last  # A03: carry previous value forward
                last = values[i]
                if values[i] is not None:
                    prices[start + dt.timedelta(minutes=step * i)] = values[i]
    return prices


def hourly_by_day(prices):
    """Average sub-hourly prices to hours, then group the hours by Vienna delivery day."""
    hours = defaultdict(list)
    for t, p in prices.items():
        hours[t.replace(minute=0, second=0, microsecond=0)].append(p)
    days = defaultdict(list)
    for h, ps in hours.items():
        local_day = h.astimezone(MARKET_TZ).date().isoformat()
        days[local_day].append(sum(ps) / len(ps))
    return days


def tb_row(day, hourly):
    s = sorted(hourly)
    mean = lambda a: sum(a) / len(a)
    tb = lambda x: mean(s[-x:]) - mean(s[:x])
    return [day, round(tb(1), 2), round(tb(2), 2), round(tb(4), 2), round(mean(s), 2)]


def fetch_market(token, eic, first_day, last_day):
    """Fetch [first_day, last_day] in chunks of up to ~1 year and return daily TB rows."""
    rows = {}
    chunk_start = first_day
    while chunk_start <= last_day:
        chunk_end = min(last_day, chunk_start + dt.timedelta(days=360))
        start_utc = dt.datetime.combine(chunk_start, dt.time(0), MARKET_TZ).astimezone(dt.timezone.utc)
        end_utc = dt.datetime.combine(chunk_end + dt.timedelta(days=1), dt.time(0), MARKET_TZ).astimezone(dt.timezone.utc)
        prices = parse_prices(fetch_xml(token, eic, start_utc, end_utc))
        for day, hourly in hourly_by_day(prices).items():
            d = dt.date.fromisoformat(day)
            if first_day <= d <= last_day and len(hourly) >= 23:  # complete delivery days only (23/24/25 h)
                rows[day] = tb_row(day, hourly)
        chunk_start = chunk_end + dt.timedelta(days=1)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="rebuild from this date (YYYY-MM-DD) instead of refreshing recent days")
    args = ap.parse_args()
    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        sys.exit("Set the ENTSOE_API_KEY environment variable (GitHub: repository secret ENTSOE_API_KEY).")

    data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {"markets": {}}
    today = dt.datetime.now(MARKET_TZ).date()
    last_day = today + dt.timedelta(days=1)  # tomorrow's prices are published around 13:00 CET
    changed = 0

    for zone, eic in MARKETS.items():
        existing = {r[0]: r for r in data["markets"].get(zone, [])}
        if args.since:
            first_day = dt.date.fromisoformat(args.since)
        elif existing:
            first_day = dt.date.fromisoformat(max(existing)) - dt.timedelta(days=REFRESH_DAYS)
        else:
            first_day = dt.date(2019, 1, 1)
        new_rows = fetch_market(token, eic, first_day, last_day)
        for day, row in new_rows.items():
            if existing.get(day) != row:
                existing[day] = row
                changed += 1
        data["markets"][zone] = [existing[d] for d in sorted(existing)]
        print(f"{zone}: {len(new_rows)} days fetched ({first_day} to {last_day}), latest stored {max(existing)}")

    if changed or "updated" not in data:
        data["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(data, separators=(",", ":")))
    print(f"{changed} day(s) added or changed.")


if __name__ == "__main__":
    main()
