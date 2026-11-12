"""
Daily update of the market data shown on the site, for Austria (AT) and Germany-Luxembourg (DE_LU).

From the ENTSO-E Transparency Platform API it fetches:
  - SDAC day-ahead prices                 -> TB1/TB2/TB4 spreads, daily base/peak prices,
                                             and the latest days at full resolution
  - FCR and aFRR capacity prices          -> daily average EUR/MW/h (aFRR up and down)
  - aFRR activated energy prices          -> daily average EUR/MWh (up and down)

and merges the results into the JSON files in docs/data/, which the web page reads.

Usage:
    ENTSOE_API_KEY=... python scripts/update_tb.py                    # refresh recent days
    ENTSOE_API_KEY=... python scripts/update_tb.py --since 2024-01-01 # rebuild from a date

Only the Python standard library is used.
"""
import argparse
import datetime as dt
import json
import os
import statistics
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
MARKET_TZ = ZoneInfo("Europe/Vienna")  # same clock as Berlin; delivery days follow CET/CEST
DATA_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"
TB_FILE, DA_FILE, RECENT_FILE, BAL_FILE = (DATA_DIR / n for n in
    ("tb_daily.json", "da_daily.json", "da_recent.json", "balancing_daily.json"))
REFRESH_DAYS = 5      # re-fetch the last few stored days in case ENTSO-E corrected them
RECENT_DAYS = 8       # days kept at full resolution for the "latest days" chart
BALANCING_START = dt.date(2024, 1, 1)
RESOLUTION_MIN = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT4H": 240, "P1D": 1440}

# Bidding zones for day-ahead prices
BIDDING_ZONE = {"AT": "10YAT-APG------L", "DE_LU": "10Y1001A1001A82H"}
# Control areas for balancing data. Germany publishes per TSO; FCR and aFRR capacity
# take the highest of the four (they normally carry the same price), aFRR energy comes
# from TenneT (the German TSOs publish one common PICASSO price; Amprion publishes none).
CONTROL_AREAS = {
    "AT": ["10YAT-APG------L"],
    "DE_LU": ["10YDE-EON------1", "10YDE-VE-------2", "10YDE-RWENET---I", "10YDE-ENBW-----N"],
}
AFRR_ENERGY_SOURCE = {
    # zone: (control area, processType). Austria publishes its aFRR price under
    # "local selection" (A68), available from July 2026; Germany under "realised" (A16).
    "AT": ("10YAT-APG------L", "A68"),
    "DE_LU": ("10YDE-EON------1", "A16"),
}
BAL_KEYS = ["fcr", "afrr_cap_up", "afrr_cap_down", "afrr_en_up", "afrr_en_down"]
# ENTSO-E publishes capacity prices per MW per settlement period, with the period differing
# between operators. Values are rescaled to EUR/MW/h by comparing them with stored days.
FACTOR_CANDIDATES = [1, 4, 0.25, 16, 0.0625, 2, 0.5, 96, 1 / 96, 24, 1 / 24]


# ---------------------------------------------------------------- helpers

def iso_to_utc(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)


def day_bounds_utc(first_day, last_day):
    start = dt.datetime.combine(first_day, dt.time(0), MARKET_TZ).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(last_day + dt.timedelta(days=1), dt.time(0), MARKET_TZ).astimezone(dt.timezone.utc)
    return start, end


def chunks(first_day, last_day, days):
    s = first_day
    while s <= last_day:
        e = min(last_day, s + dt.timedelta(days=days - 1))
        yield s, e
        s = e + dt.timedelta(days=1)


def fetch_xml(token, params, retries=3):
    url = API_URL + "?" + urllib.parse.urlencode({"securityToken": token, **params})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 400 and b"No matching data" in body:
                return body  # acknowledgement document: no data in window
            if e.code == 401:
                sys.exit("ENTSO-E rejected the API key (HTTP 401). Check the ENTSOE_API_KEY secret.")
            if attempt == retries - 1:
                print(f"  warning: ENTSO-E request failed ({e.code}) for {params.get('documentType')}: {body[:200]!r}")
                return None
        except urllib.error.URLError as e:
            if attempt == retries - 1:
                print(f"  warning: ENTSO-E request failed ({e}) for {params.get('documentType')}")
                return None
        time.sleep(5 * (attempt + 1))


def parse_series(xml_bytes, value_tag, keep=lambda ts, find: True):
    """Parse an ENTSO-E market document into {key: {utc_slot: value}} on a 15-minute grid.

    key is the flow direction (A01 up, A02 down, A03 symmetric) or "price" when absent.
    Points cover their full resolution (a 4-hour block fills 16 quarter-hours); with curve
    type A03 a missing position repeats the previous value. The first value seen for a slot wins.
    """
    out = defaultdict(dict)
    if not xml_bytes:
        return out
    root = ET.fromstring(xml_bytes)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        return out
    for ts in root.iter(f"{ns}TimeSeries"):
        find = lambda tag: (lambda el: el.text.strip() if el is not None and el.text else None)(ts.find(f"{ns}{tag}"))
        if not keep(ts, find):
            continue
        key = find("flowDirection.direction") or "price"
        for period in ts.iter(f"{ns}Period"):
            start = iso_to_utc(period.find(f"{ns}timeInterval/{ns}start").text)
            end = iso_to_utc(period.find(f"{ns}timeInterval/{ns}end").text)
            step = RESOLUTION_MIN.get(period.find(f"{ns}resolution").text.strip())
            if step is None:
                continue
            n = int((end - start).total_seconds() // (step * 60))
            values = [None] * n
            for pt in period.iter(f"{ns}Point"):
                v = pt.find(f"{ns}{value_tag}")
                pos = int(pt.find(f"{ns}position").text)
                if v is not None and v.text and 1 <= pos <= n:
                    values[pos - 1] = float(v.text)
            last = None
            for i in range(n):
                if values[i] is None:
                    values[i] = last
                last = values[i]
                if values[i] is None:
                    continue
                t0 = start + dt.timedelta(minutes=step * i)
                for q in range(max(1, step // 15)):
                    out[key].setdefault(t0 + dt.timedelta(minutes=15 * q), values[i])
    return out


def daily_mean(slots, first_day, last_day, min_slots=80):
    """Average quarter-hour values per Vienna delivery day (only days with enough data)."""
    days = defaultdict(list)
    for t, v in slots.items():
        days[t.astimezone(MARKET_TZ).date()].append(v)
    return {d: sum(v) / len(v) for d, v in days.items()
            if first_day <= d <= last_day and len(v) >= min_slots}


# ---------------------------------------------------------------- day-ahead

def sdac_only(ts, find):
    seq = find("classificationSequence_AttributeInstanceComponent.position")
    return seq is None or seq == "1"  # skip the EXAA auction (sequence 2)


def fetch_day_ahead(token, zone, first_day, last_day):
    """Return {date: prices dict} with TB row, base/peak row and the native price slots."""
    eic = BIDDING_ZONE[zone]
    native = {}
    for s, e in chunks(first_day, last_day, 360):
        start, end = day_bounds_utc(s, e)
        xml = fetch_xml(token, {"documentType": "A44", "in_Domain": eic, "out_Domain": eic,
                                "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")})
        native.update(parse_native(xml))
    hours = defaultdict(list)
    for t, p in native.items():
        hours[t.replace(minute=0)].append(p)
    days = defaultdict(list)
    for h, ps in hours.items():
        loc = h.astimezone(MARKET_TZ)
        days[loc.date()].append((loc.hour, sum(ps) / len(ps)))
    tb, da = {}, {}
    for d, hp in days.items():
        if not (first_day <= d <= last_day) or len(hp) < 23:
            continue  # complete delivery days only (23/24/25 h)
        s = sorted(p for _, p in hp)
        mean = lambda a: sum(a) / len(a)
        tb[d.isoformat()] = [d.isoformat()] + [round(mean(s[-x:]) - mean(s[:x]), 2) for x in (1, 2, 4)] + [round(mean(s), 2)]
        peak = [p for h, p in hp if 8 <= h < 20] if d.weekday() < 5 else []
        da[d.isoformat()] = [d.isoformat(), round(mean(s), 2), round(mean(peak), 2) if peak else None]
    return tb, da, native


def parse_native(xml):
    """Day-ahead prices at their published resolution (not expanded to 15 minutes)."""
    out = {}
    if not xml:
        return out
    root = ET.fromstring(xml)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        return out
    for ts in root.iter(f"{ns}TimeSeries"):
        seq = ts.find(f"{ns}classificationSequence_AttributeInstanceComponent.position")
        if seq is not None and seq.text and seq.text.strip() != "1":
            continue
        for period in ts.iter(f"{ns}Period"):
            start = iso_to_utc(period.find(f"{ns}timeInterval/{ns}start").text)
            end = iso_to_utc(period.find(f"{ns}timeInterval/{ns}end").text)
            step = RESOLUTION_MIN[period.find(f"{ns}resolution").text.strip()]
            n = int((end - start).total_seconds() // (step * 60))
            values = [None] * n
            for pt in period.iter(f"{ns}Point"):
                pos = int(pt.find(f"{ns}position").text)
                if 1 <= pos <= n:
                    values[pos - 1] = float(pt.find(f"{ns}price.amount").text)
            last = None
            for i in range(n):
                if values[i] is None:
                    values[i] = last
                last = values[i]
                if values[i] is not None:
                    out.setdefault(start + dt.timedelta(minutes=step * i), values[i])
    return out


# ---------------------------------------------------------------- balancing

def fetch_capacity(token, zone, process_type, first_day, last_day):
    """FCR (A52) or aFRR (A51) contracted capacity prices -> {direction: {slot: price}}."""
    merged = defaultdict(dict)
    for area in CONTROL_AREAS[zone]:
        for s, e in chunks(first_day, last_day, 90):
            start, end = day_bounds_utc(s, e)
            xml = fetch_xml(token, {"documentType": "A81", "businessType": "B95", "processType": process_type,
                                    "type_MarketAgreement.Type": "A01", "controlArea_Domain": area,
                                    "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")})
            for direction, slots in parse_series(xml, "procurement_Price.amount").items():
                for t, v in slots.items():
                    if t not in merged[direction] or v > merged[direction][t]:
                        merged[direction][t] = v  # highest price across the German control areas
    return merged


def fetch_afrr_energy(token, zone, first_day, last_day):
    area, process_type = AFRR_ENERGY_SOURCE[zone]
    out = defaultdict(dict)
    for s, e in chunks(first_day, last_day, 30):
        start, end = day_bounds_utc(s, e)
        xml = fetch_xml(token, {"documentType": "A84", "processType": process_type, "controlArea_Domain": area,
                                "businessType": "A96",
                                "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")})
        aFRR = lambda ts, find: find("businessType") in (None, "A96")
        for direction, slots in parse_series(xml, "activation_Price.amount", keep=aFRR).items():
            for t, v in slots.items():
                out[direction].setdefault(t, v)
    return out


def calibrate(key, fresh, stored, factors):
    """Find the factor that turns ENTSO-E's raw values into the stored unit (EUR/MW/h or EUR/MWh).

    Compares fresh values with stored ones on overlapping days. Returns None if no factor
    can be confirmed, in which case the series is left untouched this run.
    """
    ratios = [stored[d] / v for d, v in fresh.items()
              if d in stored and stored[d] not in (None, 0) and v not in (None, 0)]
    if len(ratios) >= 2:
        r = statistics.median(ratios)
        best = min(FACTOR_CANDIDATES, key=lambda c: abs(r / c - 1))
        if abs(r / best - 1) <= 0.03:
            factors[key] = best
            return best
        print(f"  warning: {key}: fresh values differ from stored ones by a factor of {r:.3f}; not updated.")
        return None
    if key in factors:
        return factors[key]
    if "afrr_en" in key:
        return 1.0  # energy prices are published in EUR/MWh
    print(f"  warning: {key}: no stored days to check the unit against; not updated.")
    return None


# ---------------------------------------------------------------- main

def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="rebuild from this date (YYYY-MM-DD) instead of refreshing recent days")
    args = ap.parse_args()
    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        sys.exit("Set the ENTSOE_API_KEY environment variable (GitHub: repository secret ENTSOE_API_KEY).")

    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(MARKET_TZ).date()
    last_day = today + dt.timedelta(days=1)  # next day's auctions are published the day before
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    tb = load(TB_FILE, {"markets": {}})
    da = load(DA_FILE, {"markets": {}})
    recent = load(RECENT_FILE, {"markets": {}})
    bal = load(BAL_FILE, {"columns": ["date"] + BAL_KEYS, "markets": {}, "factors": {}})
    bal.setdefault("factors", {})
    changed = 0

    # ---- day-ahead: TB spreads, base/peak, latest days
    for zone in BIDDING_ZONE:
        tb_rows = {r[0]: r for r in tb["markets"].get(zone, [])}
        da_rows = {r[0]: r for r in da["markets"].get(zone, [])}
        if args.since:
            first = dt.date.fromisoformat(args.since)
        elif tb_rows:
            first = dt.date.fromisoformat(max(tb_rows)) - dt.timedelta(days=REFRESH_DAYS)
        else:
            first = dt.date(2019, 1, 1)
        first = min(first, today - dt.timedelta(days=RECENT_DAYS))
        new_tb, new_da, native = fetch_day_ahead(token, zone, first, last_day)
        for d, row in new_tb.items():
            if tb_rows.get(d) != row:
                tb_rows[d] = row; changed += 1
        for d, row in new_da.items():
            if da_rows.get(d) != row:
                da_rows[d] = row; changed += 1
        tb["markets"][zone] = [tb_rows[d] for d in sorted(tb_rows)]
        da["markets"][zone] = [da_rows[d] for d in sorted(da_rows)]
        if native:
            latest = max(native).astimezone(MARKET_TZ).date()
            cut = dt.datetime.combine(latest - dt.timedelta(days=RECENT_DAYS - 1), dt.time(0), MARKET_TZ)
            recent["markets"][zone] = [[t.strftime("%Y-%m-%dT%H:%MZ"), round(p, 2)]
                                       for t, p in sorted(native.items()) if t >= cut]
        print(f"day-ahead {zone}: {len(new_tb)} days from {first}; latest stored {max(tb_rows) if tb_rows else '-'}")

    # ---- balancing: FCR and aFRR capacity, aFRR energy
    for zone in CONTROL_AREAS:
        rows = {r[0]: dict(zip(BAL_KEYS, r[1:])) for r in bal["markets"].get(zone, [])}
        dates = sorted(rows)
        if args.since:
            first = max(BALANCING_START, dt.date.fromisoformat(args.since))
        elif dates:
            first = dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=REFRESH_DAYS)
        else:
            first = BALANCING_START
        fetched = {}
        fcr = fetch_capacity(token, zone, "A52", first, last_day)
        fetched["fcr"] = daily_mean(fcr.get("A03") or fcr.get("price") or {}, first, last_day)
        afrr = fetch_capacity(token, zone, "A51", first, last_day)
        fetched["afrr_cap_up"] = daily_mean(afrr.get("A01", {}), first, last_day)
        fetched["afrr_cap_down"] = daily_mean(afrr.get("A02", {}), first, last_day)
        # aFRR energy: backfill from the series start if nothing is stored yet (Austria from July 2026)
        has_energy = any(r.get("afrr_en_up") is not None for r in rows.values())
        en_first = first if has_energy or args.since else BALANCING_START
        en = fetch_afrr_energy(token, zone, en_first, today)
        fetched["afrr_en_up"] = daily_mean(en.get("A01", {}), en_first, today, min_slots=1)
        fetched["afrr_en_down"] = daily_mean(en.get("A02", {}), en_first, today, min_slots=1)
        for k in BAL_KEYS:
            fresh = {d.isoformat(): v for d, v in fetched[k].items()}
            if not fresh:
                continue
            stored = {d: r.get(k) for d, r in rows.items() if r.get(k) is not None}
            factor = calibrate(f"{zone}.{k}", fresh, stored, bal["factors"])
            if factor is None:
                continue
            for d, v in fresh.items():
                val = round(v * factor, 3)
                if rows.setdefault(d, {}).get(k) != val:
                    rows[d][k] = val; changed += 1
        bal["markets"][zone] = [[d] + [rows[d].get(k) for k in BAL_KEYS] for d in sorted(rows)]
        print(f"balancing {zone}: " + ", ".join(f"{k} {len(fetched[k])} days" for k in BAL_KEYS))

    for obj, path in ((tb, TB_FILE), (da, DA_FILE), (recent, RECENT_FILE), (bal, BAL_FILE)):
        obj["updated"] = stamp
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, separators=(",", ":")))
    print(f"{changed} value(s) added or changed. Unit factors: {bal['factors']}")


if __name__ == "__main__":
    main()
