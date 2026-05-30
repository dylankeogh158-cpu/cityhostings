# CityHostings — KPI & Owner Reporting

Automated daily sync from Cloudbeds, materialised KPI views in Postgres, monthly PDF owner reports with AI commentary, and a Streamlit owner dashboard. Designed to run free on GitHub Actions + Supabase.

## What's in this repo

```
.
├── migrations/
│   ├── 001_schema.sql       # 7 tables + indexes
│   └── 002_views.sql        # 3 materialised views + refresh function
├── src/
│   ├── cityhostings/
│   │   ├── cloudbeds.py     # API wrapper with retry/pagination
│   │   ├── db.py            # Postgres helpers
│   │   ├── kpis.py          # KPI + P&L queries
│   │   ├── ai.py            # Claude (Haiku) commentary — token-light, prompt-cached
│   │   └── email.py         # Resend wrapper
│   ├── sync.py              # Daily entry point
│   └── generate_reports.py  # Monthly entry point
├── reports/
│   └── templates/
│       └── monthly.html     # Jinja2 → WeasyPrint PDF
├── dashboard.py             # Streamlit owner dashboard
├── .github/workflows/
│   ├── daily-sync.yml       # cron: 03:00 UTC daily
│   └── monthly-report.yml   # cron: 06:00 UTC on the 1st
├── requirements.txt
├── .env.example
└── .gitignore
```

## Setup (TL;DR)

Follow `CityHostings_Getting_Started.md` in the parent folder for full instructions. The 60-second version:

1. Run `migrations/001_schema.sql` then `migrations/002_views.sql` in Supabase SQL Editor.
2. Add the secrets from `.env.example` to GitHub Actions Secrets.
3. Add at least one row to the `properties` table.
4. Trigger **Daily Cloudbeds Sync** manually from the Actions tab.
5. Trigger **Monthly Owner Report** manually with `test_mode=true` to email yourself a sample PDF.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your keys
cd src && python sync.py          # daily sync
cd src && python generate_reports.py   # monthly job (last month)
streamlit run dashboard.py         # owner dashboard
```

## Token-light Claude usage

`src/cityhostings/ai.py` is designed for low API cost:

- **Model**: `claude-haiku-4-5` (cheapest Claude with strong quality)
- **Prompt caching**: the ~200-token system prompt is marked `cache_control: ephemeral` so it's reused across all 25 properties in the same monthly batch
- **Compact payload**: structured key-value text instead of JSON to save tokens
- **`max_tokens=500`** caps output cost
- **One call per property per month**

Expected cost at 25 properties × 12 months = 300 calls/year:
- Input (cached after the first call):  ~75k tokens/yr → **~$0.08**
- Output:                                ~110k tokens/yr → **~$0.55**
- **Total: ~$0.63/year**

## Deploying

- **GitHub Actions** runs both crons automatically once you push to `main` and add the secrets.
- **Streamlit Community Cloud** deploys `dashboard.py` from this repo — just point it at `dashboard.py`, paste the Supabase URL + anon key, and you're live at `cityhostings-owner.streamlit.app` (or your custom subdomain).
- **Supabase Storage**: create a public bucket named `reports` (or keep it private and rely on signed URLs from `monthly_reports.pdf_url`).

## Things to tune later

- **OTA commission overrides** — Cloudbeds doesn't always include commission on every reservation. If you find Airbnb bookings show £0 OTA fees, add a `commission_overrides` table keyed by source.
- **Owner stays / blocked nights** — populate `unit_availability` from Cloudbeds blocks if you want strict occupancy (the default just uses `units × days_in_month`).
- **Domain verification** for Resend — until you verify a sending domain, emails come from `onboarding@resend.dev`.
- **Time zones** — all queries are UTC. If you have UK properties and notice month-boundary weirdness, add `at time zone 'Europe/London'` to the views.

## When things break

1. Check the GitHub Actions log — it has full Python tracebacks.
2. Check `sync_runs` and `monthly_reports` tables for the most recent run.
3. Cloudbeds errors are almost always missing API scopes — re-check Phase 1 of the getting started guide.
