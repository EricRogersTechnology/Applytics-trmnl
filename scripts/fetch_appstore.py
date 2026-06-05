#!/usr/bin/env python3
"""
Fetch App Store Connect download + revenue stats and push them to a TRMNL
private-plugin webhook.

Pipeline
--------
1. Sign a short-lived ES256 JWT with your App Store Connect API key (.p8).
2. Download the last N daily Sales reports (gzipped TSV) from
   GET /v1/salesReports and parse them.
3. Aggregate downloads + developer proceeds for: latest day, trailing 7 days,
   trailing 30 days, and per-app over 30 days.
4. Fetch the current star rating + rating count per app from the public
   iTunes Lookup API (no auth needed).
5. POST a compact `merge_variables` payload to the TRMNL webhook.

Everything is configured through environment variables (see .env.example).
Run with DRY_RUN=1 to print the payload instead of posting it.

Apple report nuance: daily reports lag ~1-2 days and a day with zero sales
returns HTTP 404 (no report). We treat 404 as "no data for that day" and use
the most recent day that *did* return a report as the headline ("today").
"""

from __future__ import annotations

import gzip
import io
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT
import requests

ASC_BASE = "https://api.appstoreconnect.apple.com"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"ERROR: missing required environment variable {name}")
    return val


KEY_ID = env("ASC_KEY_ID", required=True)
ISSUER_ID = env("ASC_ISSUER_ID", required=True)
VENDOR_NUMBER = env("ASC_VENDOR_NUMBER", required=True)
WEBHOOK_URL = env("TRMNL_WEBHOOK_URL", required=True)

# Storefront used for star ratings via the iTunes lookup API.
STOREFRONT = env("ASC_STOREFRONT", "us")
# How many days back to pull. 35 safely covers a 30-day window plus the
# 1-2 day report-availability lag at the recent end.
DAYS_BACK = int(env("DAYS_BACK", "35"))
# Max apps to include in the per-app list (keeps payload under TRMNL's 2 KB).
TOP_APPS = int(env("TOP_APPS", "6"))
# Optional pinned report version, e.g. "1_1". Leave unset to let Apple default.
REPORT_VERSION = env("ASC_REPORT_VERSION")
DRY_RUN = bool(env("DRY_RUN"))


def load_private_key() -> str:
    """Return the PEM private key from ASC_PRIVATE_KEY or ASC_PRIVATE_KEY_PATH."""
    inline = os.environ.get("ASC_PRIVATE_KEY")
    if inline:
        # GitHub Secrets keep real newlines; locally a single-line var may use \n.
        return inline.replace("\\n", "\n")
    path = os.environ.get("ASC_PRIVATE_KEY_PATH")
    if path and os.path.exists(path):
        with open(path, "r") as fh:
            return fh.read()
    sys.exit(
        "ERROR: provide the App Store Connect key via ASC_PRIVATE_KEY "
        "(PEM contents) or ASC_PRIVATE_KEY_PATH (path to the .p8 file)."
    )


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def generate_token(private_key: str) -> str:
    """Create a short-lived (≤20 min) ES256 JWT for the App Store Connect API."""
    now = int(time.time())
    payload = {
        "iss": ISSUER_ID,
        "iat": now,
        "exp": now + 19 * 60,  # 19 min, comfortably under Apple's 20 min cap
        "aud": "appstoreconnect-v1",
    }
    headers = {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


# --------------------------------------------------------------------------- #
# Sales reports
# --------------------------------------------------------------------------- #
def is_app_download(product_type: str) -> bool:
    """
    Heuristic for what counts as a "download".

    Sales-report rows carry a Product Type Identifier. We exclude:
      - in-app purchases / subscriptions  -> codes starting with "IA"
      - app updates                       -> codes starting with "7"
    Everything else (free + paid first downloads, redownloads, universal,
    bundles) is counted. Tune this to match how you think about downloads.
    """
    pt = (product_type or "").strip().upper()
    if pt.startswith("IA"):
        return False
    if pt.startswith("7"):
        return False
    return True


def parse_report(raw_gzip: bytes) -> list[dict]:
    """Gunzip a Sales report and return its rows as dicts keyed by column name."""
    text = gzip.decompress(raw_gzip).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return [row for row in reader]


def fetch_daily_report(token: str, report_date: str) -> list[dict] | None:
    """
    GET one daily SALES/SUMMARY report. Returns parsed rows, or None when the
    report does not exist (404 = no sales that day, or not generated yet).
    Raises on auth / parameter errors so they surface loudly.
    """
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": VENDOR_NUMBER,
        "filter[reportDate]": report_date,
    }
    if REPORT_VERSION:
        params["filter[version]"] = REPORT_VERSION

    resp = requests.get(
        f"{ASC_BASE}/v1/salesReports",
        params=params,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/a-gzip"},
        timeout=60,
    )

    if resp.status_code == 200:
        return parse_report(resp.content)
    if resp.status_code == 404:
        return None  # no report for this day
    if resp.status_code in (401, 403):
        sys.exit(
            f"ERROR: App Store Connect returned {resp.status_code}. Check that the "
            "API key is valid and has SALES (or Admin/Finance) access, and that the "
            f"Issuer ID / Key ID / Vendor Number are correct.\n{resp.text[:400]}"
        )
    sys.exit(f"ERROR: salesReports {report_date} -> {resp.status_code}\n{resp.text[:400]}")


def to_float(value: str) -> float:
    try:
        return float((value or "0").strip())
    except ValueError:
        return 0.0


def to_int(value: str) -> int:
    try:
        return int(float((value or "0").strip()))
    except ValueError:
        return 0


def collect(token: str):
    """
    Pull DAYS_BACK daily reports and build:
      by_date[date] = {"downloads": int, "revenue": float}
      apps[apple_id] = {"name", "downloads", "revenue"}  (30-day totals)
      currency: dominant proceeds currency
    """
    by_date: dict[str, dict] = {}
    app_totals: dict[str, dict] = defaultdict(
        lambda: {"name": "", "downloads": 0, "revenue": 0.0}
    )
    currency_totals: dict[str, float] = defaultdict(float)

    today = datetime.now(timezone.utc).date()
    # day 1 = yesterday (today's report never exists yet)
    for offset in range(1, DAYS_BACK + 1):
        day = today - timedelta(days=offset)
        date_str = day.isoformat()
        rows = fetch_daily_report(token, date_str)
        if rows is None:
            continue

        day_downloads = 0
        day_revenue = 0.0
        within_30 = offset <= 30

        for row in rows:
            units = to_int(row.get("Units", "0"))
            # "Developer Proceeds" is per-unit; row total = units * proceeds.
            proceeds = to_float(row.get("Developer Proceeds", "0")) * units
            product_type = row.get("Product Type Identifier", "")
            cur = (row.get("Currency of Proceeds") or "USD").strip() or "USD"
            apple_id = (row.get("Apple Identifier") or "").strip()
            title = (row.get("Title") or "").strip()

            if is_app_download(product_type):
                day_downloads += units

            day_revenue += proceeds
            currency_totals[cur] += proceeds

            # Per-app 30-day rollup (downloads + revenue).
            if within_30 and apple_id:
                a = app_totals[apple_id]
                if title:
                    a["name"] = title
                if is_app_download(product_type):
                    a["downloads"] += units
                a["revenue"] += proceeds

        by_date[date_str] = {"downloads": day_downloads, "revenue": day_revenue}

    currency = max(currency_totals, key=currency_totals.get) if currency_totals else "USD"
    return by_date, app_totals, currency


def window_totals(by_date: dict[str, dict], latest: str, days: int):
    """Sum downloads + revenue over `days` ending at (and including) `latest`."""
    end = datetime.fromisoformat(latest).date()
    downloads, revenue = 0, 0.0
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        if d in by_date:
            downloads += by_date[d]["downloads"]
            revenue += by_date[d]["revenue"]
    return downloads, revenue


# --------------------------------------------------------------------------- #
# Ratings (public iTunes Lookup API — no auth)
# --------------------------------------------------------------------------- #
def fetch_rating(apple_id: str) -> tuple[float | None, int]:
    try:
        resp = requests.get(
            ITUNES_LOOKUP,
            params={"id": apple_id, "country": STOREFRONT},
            timeout=20,
        )
        data = resp.json()
        if data.get("resultCount"):
            res = data["results"][0]
            avg = res.get("averageUserRating")
            count = res.get("userRatingCount", 0) or 0
            return (round(float(avg), 1) if avg is not None else None, int(count))
    except (requests.RequestException, ValueError, KeyError):
        pass
    return (None, 0)


# --------------------------------------------------------------------------- #
# Payload + push
# --------------------------------------------------------------------------- #
def money(value: float) -> str:
    return f"{value:,.2f}"


def build_payload(by_date, app_totals, currency) -> dict:
    if not by_date:
        # Brand-new app or no sales yet: push zeros so the screen still renders.
        return {
            "merge_variables": {
                "updated_at": "No data yet",
                "downloads_day": 0,
                "downloads_7d": 0,
                "downloads_30d": 0,
                "revenue_day": "0.00",
                "revenue_7d": "0.00",
                "revenue_30d": "0.00",
                "currency": currency,
                "apps": [],
            }
        }

    latest = max(by_date.keys())
    dl_day, rev_day = window_totals(by_date, latest, 1)
    dl_7, rev_7 = window_totals(by_date, latest, 7)
    dl_30, rev_30 = window_totals(by_date, latest, 30)

    # Top apps by 30-day downloads (fall back to revenue for paid apps).
    ranked = sorted(
        app_totals.items(),
        key=lambda kv: (kv[1]["downloads"], kv[1]["revenue"]),
        reverse=True,
    )[:TOP_APPS]

    apps = []
    for apple_id, a in ranked:
        rating, rating_count = fetch_rating(apple_id)
        apps.append(
            {
                "name": a["name"] or apple_id,
                "downloads_30d": a["downloads"],
                "revenue_30d": money(a["revenue"]),
                "rating": f"{rating:.1f}" if rating is not None else "—",
                "ratings_count": rating_count,
            }
        )

    pretty_date = datetime.fromisoformat(latest).strftime("%b %-d, %Y")
    return {
        "merge_variables": {
            "updated_at": pretty_date,
            "downloads_day": dl_day,
            "downloads_7d": dl_7,
            "downloads_30d": dl_30,
            "revenue_day": money(rev_day),
            "revenue_7d": money(rev_7),
            "revenue_30d": money(rev_30),
            "currency": currency,
            "apps": apps,
        }
    }


def trim_to_size(payload: dict, limit: int = 2000) -> dict:
    """Drop trailing apps until the JSON fits TRMNL's free-tier 2 KB limit."""
    apps = payload["merge_variables"]["apps"]
    while apps and len(json.dumps(payload)) > limit:
        apps.pop()
    return payload


def push(payload: dict) -> None:
    size = len(json.dumps(payload))
    print(f"Payload ({size} bytes):")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if DRY_RUN:
        print("\nDRY_RUN set — not posting to TRMNL.")
        return

    resp = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    if resp.status_code == 429:
        sys.exit("ERROR: TRMNL rate limit (429). Free tier allows 12 pushes/hour.")
    if not resp.ok:
        sys.exit(f"ERROR: TRMNL webhook -> {resp.status_code}\n{resp.text[:400]}")
    print(f"\nPushed to TRMNL OK ({resp.status_code}).")


def main() -> None:
    private_key = load_private_key()
    token = generate_token(private_key)
    by_date, app_totals, currency = collect(token)
    payload = trim_to_size(build_payload(by_date, app_totals, currency))
    push(payload)


if __name__ == "__main__":
    main()
