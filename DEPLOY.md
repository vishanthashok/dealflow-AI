# DealFlow AI — Single File Deployment Guide

## Files Included
- `dealflow_ai.py` — The entire application (FastAPI backend + embedded frontend)
- `requirements.txt` — Python dependencies

---

## Run Locally (2 minutes)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run
python dealflow_ai.py

# 3. Open browser
# → http://localhost:8000       (Full UI)
# → http://localhost:8000/api/docs  (Swagger API docs)
```

Uses SQLite by default (no database setup needed). A `dealflow.db` file is
created automatically in the same folder on first run.

---

## 🏆 DEPLOYMENT RECOMMENDATION: Railway (Best Choice)

### Why Railway over Streamlit or Vercel?

| Platform    | Verdict | Reason |
|-------------|---------|--------|
| **Railway** | ✅ BEST  | Native Python support, free tier, one-click deploy, runs FastAPI perfectly |
| Render      | ✅ Good  | Similar to Railway, also free tier |
| **Vercel**  | ⚠️ Poor  | Built for Next.js/Node.js — FastAPI on Vercel requires serverless hacks, no persistent DB, cold starts |
| **Streamlit**| ❌ Wrong | Streamlit is for data-science dashboards only, NOT for full-stack web apps with auth/APIs |

---

## Deploy to Railway (Free, 5 minutes)

1. Push both files to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Railway auto-detects Python — add these environment variables:
   ```
   SECRET_KEY=your-random-secret-here
   PORT=8000
   ADMIN_SYNC_SECRET=long-random-string-for-cron-sync
   ```
   Optional — live deal ingestion:
   ```
   FEED_SYNC_INTERVAL_MINUTES=30
   NEWSAPI_KEY=...          # https://newsapi.org — extra headlines
   NEWSAPI_QUERY=...        # optional; default is M&A-focused
   DEAL_RSS_FEEDS=https://...,https://...   # optional; comma-separated RSS URLs
   ```
   After deploy: `GET /api/v1/system/feeds` shows sync status; `POST /api/v1/system/sync-feeds` with header `X-Admin-Sync-Secret` triggers a pull (use for Railway Cron).
5. Set the **start command**:
   ```
   python dealflow_ai.py
   ```
6. Done! Railway gives you a live URL like `https://dealflow-xxx.railway.app`

**Auth URLs (same deployment):** full-page **Sign in** at `/login` and **Sign up** at `/register` (data is stored in your database — use Postgres below so users are not wiped on every redeploy).

### Using PostgreSQL on Railway (optional upgrade from SQLite)
1. In Railway dashboard → Add Plugin → PostgreSQL
2. It auto-injects `DATABASE_URL` into your environment
3. The app detects it automatically — no code changes needed

---

## Deploy to Render (Alternative Free Option)

1. Push to GitHub
2. Go to https://render.com → New Web Service
3. Connect your repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `python dealflow_ai.py`
6. Add env var: `SECRET_KEY=your-random-secret`
7. Deploy!

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./dealflow.db` | Database (SQLite or PostgreSQL URL) |
| `SECRET_KEY` | random | JWT signing secret — SET THIS in production |
| `PORT` | `8000` | Server port |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | JWT access token lifetime |
| `FEED_SYNC_INTERVAL_MINUTES` | `30` | Background RSS/NewsAPI sync (`0` = off) |
| `NEWSAPI_KEY` | — | [NewsAPI](https://newsapi.org) key for `/v2/everything` |
| `NEWSAPI_QUERY` | M&A phrase | NewsAPI `q` parameter |
| `DEAL_RSS_FEEDS` | built-in list | Comma-separated RSS feed URLs |
| `ADMIN_SYNC_SECRET` | — | Protects `POST /api/v1/system/sync-feeds` |

### APIs & data sources (what you may want next)

| Tier | APIs / sources | Notes |
|------|------------------|--------|
| **Free / no key** | Public **RSS** (Google News, MarketWatch, Yahoo Finance, any outlet that publishes RSS) | Implemented in-app; respect each site’s Terms of Use and robots rules. |
| **Low-cost news** | [NewsAPI](https://newsapi.org), [GNews](https://gnews.io), [Bing News Search](https://www.microsoft.com/en-us/bing/apis/bing-news-search-api) | Broad headlines; not PE-grade deal terms. |
| **Filings (US)** | [SEC EDGAR](https://www.sec.gov/edgar) (8-K Item 2.01,/schedules) | Structured but noisy; usually need your own parser + rate limits. |
| **Premium deal data** | PitchBook, S&P Capital IQ, Refinitiv, Preqin, FactSet M&A | Contracts + licensing; these are the “real” live trackers enterprises use. |

For **several websites**, the practical path is: **RSS + optional NewsAPI**, then add **paid** or **broker feed** APIs when you need verified deal values and parties.

---

## Features

- ✅ Live deal ingestion from multiple **RSS** feeds (plus optional **NewsAPI**)
- ✅ Background sync on a timer; manual / cron trigger via `POST /api/v1/system/sync-feeds`
- ✅ Full M&A deal feed with AI classification
- ✅ Sector + buyer type + deal size NLP engine
- ✅ JWT authentication (register/login)
- ✅ Deal bookmarking
- ✅ Smart alert creation
- ✅ Trending sector momentum dashboard
- ✅ CSV export (authenticated users)
- ✅ Full REST API at `/api/v1/`
- ✅ Swagger docs at `/api/docs`
- ✅ 15 seed deals pre-loaded on first run
