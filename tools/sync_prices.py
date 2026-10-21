#!/usr/bin/env python3
"""
Mandi Prices Daily Sync & Mirror Tool
--------------------------------------
Fetches daily commodity prices across all Indian mandis from the Agmarknet API,
flattens them into the standard Mandi Bhav schema, and saves a consolidated
mirror snapshot to `docs/prices_latest.json`.

Runs for FREE via GitHub Actions cron at 12:00 PM, 4:30 PM, and 8:30 PM IST
(capturing the day's final closing rates before nightly government server maintenance).
"""

import base64
import gzip
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

ssl._create_default_https_context = ssl._create_unverified_context

# State Name -> Agmarknet State ID Mapping
STATE_IDS = {
    "Maharashtra": "20",
    "Uttar Pradesh": "34",
    "Madhya Pradesh": "19",
    "Karnataka": "16",
    "Gujarat": "11",
    "Rajasthan": "29",
    "Punjab": "28",
    "Andhra Pradesh": "2",
    "Telangana": "32",
    "Tamil Nadu": "31",
    "West Bengal": "36",
    "Bihar": "5",
    "Haryana": "12",
    "Odisha": "26",
    "Kerala": "17",
    "Himachal Pradesh": "13",
    "Uttarakhand": "35",
    "Chhattisgarh": "7",
    "Jharkhand": "15",
    "Assam": "4",
    "Goa": "10",
    "Delhi": "9",
    "Jammu and Kashmir": "14"
}

# Top traded agricultural commodities across India (used by weighted report fallback)
PRIMARY_COMMODITY_IDS = [
    1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
    18, 19, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 34, 35,
    36, 37, 38, 39, 42, 43, 45, 46, 52, 59, 60, 61, 64, 65, 66, 67,
    68, 69, 70, 71, 73, 74, 76, 78, 84, 85, 87, 92, 106, 111, 113, 114,
    115, 116, 118, 122, 124, 125, 126, 127, 129, 131, 132, 133, 136, 140, 151, 156,
    160, 161, 214, 217, 260, 261, 292, 309, 342, 343
]

# Optional Agmarknet access token — if not set or expired, falls back to automatic Captcha OCR.
AGMARKNET_TOKEN = os.environ.get("AGMARKNET_TOKEN", "").strip()

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://agmarknet.gov.in",
    "Referer": "https://agmarknet.gov.in/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
}

if AGMARKNET_TOKEN:
    HEADERS["Authorization"] = f"Bearer {AGMARKNET_TOKEN}"


class MaintenanceException(Exception):
    pass


class RateLimitException(Exception):
    pass


def find_tesseract_bin():
    for candidate in (shutil.which("tesseract"), "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "tesseract"


TESSERACT_BIN = find_tesseract_bin()


def generate_and_solve_captcha():
    """Fetches a fresh single-use captcha from Agmarknet and solves it via Tesseract OCR."""
    url = "https://api.agmarknet.gov.in/v1/captcha/generator"
    headers = {k: v for k, v in HEADERS.items() if k != "Authorization"}
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status == 200:
                payload = json.loads(response.read().decode("utf-8"))
                captcha_key = payload.get("captcha_key", "")
                img_b64 = payload.get("captcha_image", "")
                if not captcha_key or not img_b64:
                    return None, None
                img_bytes = base64.b64decode(img_b64)
                proc = subprocess.run(
                    [
                        TESSERACT_BIN,
                        "stdin",
                        "stdout",
                        "--psm",
                        "7",
                        "-c",
                        "tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    ],
                    input=img_bytes,
                    capture_output=True,
                    timeout=10,
                )
                code = "".join(proc.stdout.decode("utf-8", errors="ignore").split())
                if code:
                    return captcha_key, code
    except Exception as e:
        print(f"  [WARN] Captcha generation/OCR failed: {e}", file=sys.stderr)
    return None, None


def append_captcha_params(url, captcha_key, captcha_code):
    sep = "&" if "?" in url else "?"
    return (
        f"{url}{sep}captcha_key={urllib.parse.quote(captcha_key)}"
        f"&captcha={urllib.parse.quote(captcha_code)}"
    )


def fetch_json(url, timeout=30, retries=3, use_captcha=False, method="GET", body_dict=None, fast_fail_429=False):
    need_captcha = use_captcha and not AGMARKNET_TOKEN
    for attempt in range(retries + 1):
        target_url = url
        req_headers = {**HEADERS}
        payload_bytes = None
        current_body = dict(body_dict) if body_dict is not None else None

        if need_captcha:
            req_headers.pop("Authorization", None)
            ckey, ccode = generate_and_solve_captcha()
            if ckey and ccode:
                target_url = append_captcha_params(url, ckey, ccode)
                if current_body is not None:
                    current_body["captcha_key"] = ckey
                    current_body["captcha"] = ccode

        if current_body is not None:
            req_headers["Content-Type"] = "application/json"
            payload_bytes = json.dumps(current_body).encode("utf-8")

        req = urllib.request.Request(target_url, data=payload_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    data = response.read().decode("utf-8")
                    return json.loads(data)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            if e.code == 503:
                raise MaintenanceException("Agmarknet is in scheduled maintenance (HTTP 503)")
            if "Too many requests" in err_body or e.code == 429:
                if fast_fail_429:
                    raise RateLimitException("Endpoint rate-limited (HTTP 429)")
                if attempt < retries:
                    time.sleep(3.0)
                    continue
            if e.code in (400, 401) and (
                "TOKEN_OR_CAPTCHA_REQUIRED" in err_body
                or "CAPTCHA" in err_body.upper()
                or e.code == 401
            ):
                need_captcha = True
                if attempt < retries:
                    time.sleep(0.5)
                    continue
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"  [WARN] HTTP {e.code} for {url}: {err_body[:120]}", file=sys.stderr)
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"  [WARN] Request failed for {url}: {e}", file=sys.stderr)
    return None


def resolve_live_date_and_market_districts():
    """
    Resolves the current active report date and market->district lookup from
    /v1/daily-price-arrival/filters.
    Note: /v1/agmarknet-live-date returns the static portal launch date (11-07-2025)
    and must NOT be used as the current price query date.
    """
    ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    fallback_date = ist_now.strftime("%Y-%m-%d")
    market_to_district = {}

    url = "https://api.agmarknet.gov.in/v1/daily-price-arrival/filters"
    data = fetch_json(url, timeout=20, retries=2)
    if data and isinstance(data, dict):
        filter_data = data.get("data") or {}
        if isinstance(filter_data, dict):
            # Build district lookup
            district_by_id = {}
            for d in filter_data.get("district_data") or []:
                did = d.get("id")
                dname = (d.get("district_name") or "").strip()
                if did and dname and dname.lower() != "all districts":
                    district_by_id[did] = dname

            for m in filter_data.get("market_data") or []:
                mname = (m.get("mkt_name") or "").strip()
                did = m.get("district_id")
                if mname and did in district_by_id:
                    market_to_district[mname.lower()] = district_by_id[did]

            # Extract current to_date from range_data
            range_list = filter_data.get("range_data") or []
            if range_list and isinstance(range_list[0], dict):
                price_range = range_list[0].get("price") or range_list[0].get("both") or {}
                to_date = (price_range.get("to_date") or "").strip()
                if to_date:
                    try:
                        parsed = datetime.strptime(to_date, "%Y-%m-%d")
                        # Never accept stale dates older than 7 days
                        if abs((ist_now.date() - parsed.date()).days) <= 7:
                            return parsed.strftime("%Y-%m-%d"), market_to_district
                    except ValueError:
                        pass

    return fallback_date, market_to_district


def normalize_state_name(raw_state):
    cleaned = " ".join((raw_state or "").strip().split())
    low = cleaned.lower()
    mapping = {
        "keralam": "Kerala",
        "kerala": "Kerala",
        "chattisgarh": "Chhattisgarh",
        "chhattisgarh": "Chhattisgarh",
        "nct of delhi": "Delhi",
        "new delhi": "Delhi",
        "delhi": "Delhi",
        "jammu & kashmir": "Jammu and Kashmir",
        "jammu and kashmir": "Jammu and Kashmir",
        "andaman & nicobar": "Andaman and Nicobar",
        "andaman and nicobar islands": "Andaman and Nicobar",
        "andaman and nicobar": "Andaman and Nicobar",
        "dadra & nagar haveli": "Dadra and Nagar Haveli",
        "dadra and nagar haveli": "Dadra and Nagar Haveli",
        "daman & diu": "Daman and Diu",
        "daman and diu": "Daman and Diu",
        "pondicherry": "Puducherry",
        "puducherry": "Puducherry",
        "orissa": "Odisha",
        "odisha": "Odisha",
        "uttaranchal": "Uttarakhand",
        "uttarakhand": "Uttarakhand",
    }
    return mapping.get(low, cleaned)


def normalize_quintal_prices(min_raw, max_raw, modal_raw, commodity_name="", unit_of_price=""):
    if modal_raw is None:
        return None
    try:
        modal_int = int(round(float(modal_raw)))
    except (ValueError, TypeError):
        return None
    if modal_int <= 2:
        return None

    try:
        min_int = int(round(float(min_raw))) if min_raw is not None else modal_int
    except (ValueError, TypeError):
        min_int = modal_int
    try:
        max_int = int(round(float(max_raw))) if max_raw is not None else modal_int
    except (ValueError, TypeError):
        max_int = modal_int

    if min_int <= 0:
        min_int = modal_int
    if max_int <= 0:
        max_int = modal_int

    unit_lower = (unit_of_price or "").strip().lower()
    comm_lower = (commodity_name or "").strip().lower()
    is_count_based = (
        any(k in unit_lower for k in ("dozen", "nos", "number", "piece", "bundle", "bunch"))
        or "coconut" in comm_lower
        or "egg" in comm_lower
    )

    scale = 100 if (not is_count_based and 3 <= modal_int <= 55 and max_int <= 65) else 1
    scaled_modal = modal_int * scale
    scaled_min = min_int * scale
    scaled_max = max_int * scale
    return min(scaled_min, scaled_modal), max(scaled_max, scaled_modal), scaled_modal


def sanitize_variety(commodity, variety):
    comm_lower = commodity.lower()
    if "flower" in comm_lower or "rose" in comm_lower or "marigold" in comm_lower:
        return variety
    var_lower = variety.lower()
    if var_lower in ["other flowers", "flowers", "flowers-others", "gulabi"]:
        return "Other"
    return variety


def flatten_response(data, state_name, date_str, market_to_district=None):
    """Flattens Agmarknet 2.0 daily-report-state response into PriceRecord list."""
    records = []
    if not data or not isinstance(data, dict):
        return records
    market_to_district = market_to_district or {}
    norm_state = normalize_state_name(state_name)

    markets = data.get("markets", []) or []
    for market in markets:
        market_name = (market.get("marketName") or "").strip()
        if not market_name:
            continue
        district_name = market_to_district.get(market_name.lower(), "")
        commodity_groups = market.get("commodityGroups", []) or []
        for group in commodity_groups:
            commodities = group.get("commodities", []) or []
            for comm in commodities:
                comm_name = (comm.get("commodityName") or "").strip()
                if not comm_name:
                    continue
                var_data = comm.get("data", []) or []
                for vp in var_data:
                    normalized = normalize_quintal_prices(
                        vp.get("minimumPrice"),
                        vp.get("maximumPrice"),
                        vp.get("modalPrice"),
                        comm_name,
                        vp.get("unitOfPrice") or "",
                    )
                    if not normalized:
                        continue
                    min_int, max_int, modal_int = normalized
                    raw_var = (vp.get("variety") or "").strip()
                    variety = sanitize_variety(comm_name, raw_var) if raw_var else "Other"

                    records.append({
                        "state": norm_state,
                        "district": district_name,
                        "market": market_name,
                        "commodity": comm_name,
                        "variety": variety,
                        "arrivalDate": date_str,
                        "minPrice": min_int,
                        "maxPrice": max_int,
                        "modalPrice": modal_int
                    })
    return records


def flatten_weighted_response(data, date_str, market_to_district=None):
    """Flattens Agmarknet 2.0 commodity-market/daily-report-weighted response into PriceRecord list."""
    records = []
    if not data or not isinstance(data, dict):
        return records
    market_to_district = market_to_district or {}

    for cg in data.get("commodities", []) or []:
        for item in cg.get("items", []) or []:
            comm_name = (item.get("Commodity") or "").strip()
            if not comm_name:
                continue
            for st in item.get("states", []) or []:
                state_name = normalize_state_name(st.get("state") or "")
                if not state_name:
                    continue
                for mkt in st.get("markets", []) or []:
                    market_name = (mkt.get("market_name") or "").strip()
                    if not market_name:
                        continue
                    district_name = market_to_district.get(market_name.lower(), "")
                    for vp in mkt.get("data", []) or []:
                        normalized = normalize_quintal_prices(
                            vp.get("minPrice"),
                            vp.get("maxPrice"),
                            vp.get("modalPrice"),
                            comm_name,
                            vp.get("unitOfPrice") or "",
                        )
                        if not normalized:
                            continue
                        min_int, max_int, modal_int = normalized
                        raw_var = (vp.get("variety") or "").strip()
                        variety = sanitize_variety(comm_name, raw_var) if raw_var else "Other"

                        records.append({
                            "state": state_name,
                            "district": district_name,
                            "market": market_name,
                            "commodity": comm_name,
                            "variety": variety,
                            "arrivalDate": date_str,
                            "minPrice": min_int,
                            "maxPrice": max_int,
                            "modalPrice": modal_int
                        })
    return records


def sync_via_weighted_report(target_date, market_to_district=None, batch_size=9):
    """
    Fetches all states across India in commodity batches via
    POST /v1/prices-and-arrivals/commodity-market/daily-report-weighted.
    Used when GET daily-report-state is rate-limited (HTTP 429).
    """
    print(f"[*] Fetching all-India prices via weighted report for date: {target_date}")
    url = "https://api.agmarknet.gov.in/v1/prices-and-arrivals/commodity-market/daily-report-weighted"
    all_records = []
    batches = [
        PRIMARY_COMMODITY_IDS[i:i + batch_size]
        for i in range(0, len(PRIMARY_COMMODITY_IDS), batch_size)
    ]

    for idx, batch in enumerate(batches, 1):
        print(f"  -> Batch {idx}/{len(batches)} (commodities={batch})...", end="", flush=True)
        data = fetch_json(
            url,
            timeout=35,
            retries=4,
            use_captcha=True,
            method="POST",
            body_dict={"date": target_date, "commodityIds": batch},
        )
        if data:
            recs = flatten_weighted_response(data, target_date, market_to_district)
            all_records.extend(recs)
            print(f" OK ({len(recs)} records)")
        else:
            print(" FAILED")

    states_count = len(set(r["state"] for r in all_records))
    return all_records, states_count


def sync_all_states(target_date, market_to_district=None):
    """Iterates through all priority and remaining states to collect prices."""
    print(f"[*] Starting Mandi price sync for date: {target_date}")
    all_records = []
    successful_states = 0
    consecutive_429 = 0

    for state_name, state_id in STATE_IDS.items():
        url = f"https://api.agmarknet.gov.in/v1/prices-and-arrivals/commodity-wise/daily-report-state?date={target_date}&stateIds={state_id}"
        print(f"  -> Fetching {state_name} (ID: {state_id})...", end="", flush=True)
        try:
            data = fetch_json(url, use_captcha=True, retries=2, fast_fail_429=True)
            consecutive_429 = 0
        except MaintenanceException as me:
            print(" MAINTENANCE (Agmarknet 503)")
            raise me
        except RateLimitException:
            print(" RATE LIMITED (429)")
            print("[!] daily-report-state is rate-limited (HTTP 429). Switching to complete weighted report endpoint...")
            return sync_via_weighted_report(target_date, market_to_district)

        if data:
            records = flatten_response(data, state_name, target_date, market_to_district)
            if records:
                all_records.extend(records)
                successful_states += 1
                print(f" OK ({len(records)} records)")
            else:
                print(" 0 records")
        else:
            print(" FAILED")

    if not all_records or successful_states < 5:
        return sync_via_weighted_report(target_date, market_to_district)

    return all_records, successful_states


def merge_records(primary_records, secondary_records):
    """
    Merges secondary_records (e.g. yesterday's closing prices or rolling 7-day history)
    with primary_records, deduplicating by (state, market, commodity, variety, arrivalDate).
    Each record retains its genuine arrivalDate.
    """
    seen = set()
    merged = []
    for r in primary_records:
        key = (
            r["state"].lower(),
            r["market"].lower(),
            r["commodity"].lower(),
            r["variety"].lower(),
            r.get("arrivalDate", ""),
        )
        if key not in seen:
            seen.add(key)
            merged.append(r)

    for r in secondary_records:
        key = (
            r["state"].lower(),
            r["market"].lower(),
            r["commodity"].lower(),
            r["variety"].lower(),
            r.get("arrivalDate", ""),
        )
        if key not in seen:
            seen.add(key)
            merged.append(r)

    return merged


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_out_dir = os.environ.get("MANDI_OUTPUT_DIR", "").strip()
    if env_out_dir:
        out_dir = os.path.abspath(os.path.join(repo_root, env_out_dir)) if not os.path.isabs(env_out_dir) else env_out_dir
    else:
        out_dir = os.path.join(repo_root, "docs")
    os.makedirs(out_dir, exist_ok=True)
    out_json = os.path.join(out_dir, "prices_latest.json")
    out_gz = os.path.join(out_dir, "prices_latest.json.gz")

    existing_records = []
    existing_date = ""
    if os.path.exists(out_json):
        try:
            with open(out_json, "r", encoding="utf-8") as f:
                existing_payload = json.load(f)
                if isinstance(existing_payload, dict):
                    existing_records = existing_payload.get("records") or []
                    existing_date = (existing_payload.get("date") or "").strip()
        except Exception:
            existing_records = []

    try:
        date_str, market_to_district = resolve_live_date_and_market_districts()
        print(f"[*] Resolved current Agmarknet date: {date_str} (districts mapped: {len(market_to_district)})")
        records, states_ok = sync_all_states(date_str, market_to_district)

        # Also fetch yesterday's data if today's count is partial (< 5000 records) or empty,
        # so markets that haven't submitted today's closing report yet still have yesterday's genuine price.
        yesterday_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if len(records) < 5000:
            print(f"[*] Supplementing {date_str} ({len(records)} records) with {yesterday_str} closing prices...")
            y_records, _ = sync_all_states(yesterday_str, market_to_district)
            if not records and y_records:
                date_str = yesterday_str
                records = y_records
            elif y_records:
                records = merge_records(records, y_records)

        if not records:
            two_days_ago_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
            print(f"[!] Still no records found. Attempting {two_days_ago_str} fallback...")
            records, states_ok = sync_all_states(two_days_ago_str, market_to_district)
            if records:
                date_str = two_days_ago_str
    except MaintenanceException as me:
        print(f"\n[INFO] {me}. Agmarknet servers are in maintenance; refreshing snapshot with preserved records.")
        records = []
        date_str = existing_date or (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")

    if existing_records and records:
        cutoff_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        recent_existing = [
            r for r in existing_records
            if str(r.get("arrivalDate", "")) >= cutoff_str
            and str(r.get("arrivalDate", "")) <= date_str
        ]
        records = merge_records(records, recent_existing)
    elif existing_records and not records:
        print(f"[*] Using {len(existing_records)} preserved records from existing snapshot.")
        records = existing_records
        if existing_date:
            date_str = existing_date

    # Strictly filter out any historical/stale records older than 2026
    records = [
        r for r in records
        if not str(r.get("arrivalDate", "")).startswith("2025")
        and str(r.get("arrivalDate", "")) >= "2026-01-01"
    ]

    if not records:
        print("[WARN] Agmarknet returned 0 total records across recent dates.")
        if os.path.exists(out_json):
            print(f"       Preserving existing {out_json} snapshot.")
            sys.exit(0)

    now_ms = int(time.time() * 1000)
    payload = {
        "version": 1,
        "date": date_str,
        "updatedAtMs": now_ms,
        "recordsCount": len(records),
        "statesCount": len(set(r["state"] for r in records)),
        "commoditiesCount": len(set(r["commodity"] for r in records)),
        "records": records
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"[✓] Saved {len(records)} records ({payload['commoditiesCount']} commodities across {payload['statesCount']} states) to {out_json}")
    print(f"[✓] Compressed gzip saved to {out_gz} ({os.path.getsize(out_gz)} bytes)")


if __name__ == "__main__":
    main()
