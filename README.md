# Applytics — App Store Connect → TRMNL

> An **unofficial, open-source** [TRMNL](https://usetrmnl.com) plugin that puts your App Store **downloads**, **developer proceeds**, and **★ ratings** on an e-ink display — refreshed automatically every day. Account-wide: every app under your developer account, no per-app setup.

```
┌─────────────────────────────────────────┐
│   42            310           1280        │   ← downloads: today / 7d / 30d
│   Downloads     Last 7 days   Last 30 d   │
│                                           │
│   USD 12.50     USD 88.00     USD 350.00  │   ← developer proceeds
│   today         7-day         30-day      │
│                                           │
│   Your App      900 dl · ★ 4.8 (25)       │   ← per-app breakdown
│   ─────────────────────────────────────   │
│   📱 Applytics · Updated Jun 2            │
└─────────────────────────────────────────┘
```

## How it works — and why your key stays safe

TRMNL plugins render JSON through a Liquid template; they can't run code. But App Store Connect requires a **JWT signed with your private key** on every request (tokens live ≤20 minutes), which TRMNL can't produce.

So a small Python script — run by **GitHub Actions on a daily schedule, in *your own* fork** — signs the token, pulls Apple's Sales reports, and **pushes** the result to your TRMNL webhook:

```
GitHub Actions (your fork · cron 2×/day)
  └─ sign JWT with your .p8 → GET /v1/salesReports (gzipped TSV)
       → aggregate downloads + proceeds → POST { merge_variables } → TRMNL
```

**Your `.p8` private key never leaves your control.** It lives only in *your* GitHub Secrets (encrypted) and on your own machine. Data flows only to Apple's API and your own TRMNL webhook. **This repo ships no credentials.**

## What you'll need

1. An **App Store Connect API key** with **Sales** access — a `.p8` file, a **Key ID**, and an **Issuer ID**
2. Your **Vendor Number**
3. A **TRMNL** account + a **Private Plugin** (Webhook strategy)
4. A **GitHub** account (free Actions minutes run the daily job)

## Setup

### 1 · App Store Connect API key
*Users and Access → Integrations → App Store Connect API* → **＋** → name it → **Access: Sales** (least privilege) → **Generate**. Download `AuthKey_XXXXXXXXXX.p8` (**one-time!**), and note the **Key ID** (key row) and **Issuer ID** (top of page).

### 2 · Vendor Number
*Business → Payments and Financial Reports* → the 8-ish-digit number (often starts with `8`).

### 3 · TRMNL plugin
*TRMNL → Plugins → Private Plugin.* Either:
- **Import** [`dist/applytics-trmnl.zip`](dist/applytics-trmnl.zip) — creates the webhook plugin + all four layouts in one step, **or**
- create it manually (**Strategy: Webhook**) and paste the four `src/*.liquid` files into **Edit Markup**.

Copy the plugin's **Webhook URL**.

### 4 · Fork this repo + add Secrets
**Fork** this repo, then in your fork: *Settings → Secrets and variables → Actions* → add each (names are case-sensitive):

| Secret | Value |
|---|---|
| `ASC_KEY_ID` | your Key ID |
| `ASC_ISSUER_ID` | your Issuer ID |
| `ASC_PRIVATE_KEY` | the **contents** of your `.p8` (the whole `-----BEGIN…END-----` block) |
| `ASC_VENDOR_NUMBER` | your Vendor Number |
| `TRMNL_WEBHOOK_URL` | your plugin's webhook URL |

### 5 · Run it
*Actions → Applytics → Run workflow* (or wait for the daily cron). The log prints the payload it pushed; your TRMNL updates on its next refresh.

## Run locally (optional)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in your values
set -a; source .env; set +a
DRY_RUN=1 python scripts/fetch_appstore.py   # prints the payload, doesn't post
```

## Preview the layouts
```bash
gem install trmnl_preview && bin/trmnlp      # → http://localhost:4567
```
`.trmnlp.yml` ships with mock data so you can design without the API.

## Customizing
| Want to… | Where |
|---|---|
| Change refresh times | `cron:` in `.github/workflows/trmnl.yml` (UTC) |
| Redefine a "download" | `is_app_download()` in `scripts/fetch_appstore.py` |
| Show more/fewer apps | `TOP_APPS` env var (default 6) |
| Track one app (or a few) | `ASC_APP_ID` = the app's numeric App Store ID (from its App Store URL); unset = whole account |
| Non-US ratings storefront | `ASC_STOREFRONT` env var (default `us`) |
| Restyle the screen | `src/*.liquid` + the [TRMNL framework](https://trmnl.com/framework) |

## Notes & limits
- **TRMNL free tier:** ≤2 KB payload, ≤12 pushes/hour. The script auto-trims the per-app list to fit; a daily cron is well within limits.
- **Revenue is an estimate** — `Units × Developer Proceeds` from the Sales report, in your dominant proceeds currency. Apple's monthly *Financial* reports are the settled source.
- **Proceeds need the Paid Apps Agreement** active (bank + tax info). Downloads don't.
- **Downloads** use the Sales/Summary report (excludes in-app purchases and updates by default — tune `is_app_download()`).
- **Data lags ~1–2 days**, and a day with no sales returns `404`; the script uses the latest available day as "today" and shows **"No data yet"** when there's nothing.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| `401` / `403` from Apple | wrong Key ID / Issuer ID, key lacks Sales access, or bad `.p8` |
| "No data yet" | app isn't generating App Store sales yet (TestFlight doesn't count), or reports not ready |
| `400` from Apple | wrong Vendor Number; try pinning `ASC_REPORT_VERSION=1_1` |
| `429` from TRMNL | pushing too often (free tier = 12/hr) |
| `Could not deserialize key…` in CI | `ASC_PRIVATE_KEY` holds the *filename* instead of the file *contents* |

## Security
- Your `.p8` lives only in **your** GitHub Secrets (encrypted at rest) and on your machine.
- `.p8` and `.env` are git-ignored; this repo contains **no** credentials.
- Prefer a **Sales**-scoped key (least privilege): if a secret ever leaked, it could only *read reports*, never manage your account.

## Disclaimer
Unofficial. Not affiliated with or endorsed by **Apple** or **TRMNL**. "App Store Connect" is a trademark of Apple Inc. Provided as-is, no warranty.

## License
[MIT](LICENSE) © 2026 Eric Rogers
