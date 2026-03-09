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
   ```
5. Set the **start command**:
   ```
   python dealflow_ai.py
   ```
6. Done! Railway gives you a live URL like `https://dealflow-xxx.railway.app`

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

---

## Features

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
