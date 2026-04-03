"""
╔══════════════════════════════════════════════════════════════╗
║           DealFlow AI — Complete Single-File App             ║
║     FastAPI Backend + Embedded Frontend (SQLite Edition)     ║
╚══════════════════════════════════════════════════════════════╝

Run:  python dealflow_ai.py
Then: http://localhost:8000

Deploy to Railway: push repo + requirements.txt; set SECRET_KEY, optional
NEWSAPI_KEY, ADMIN_SYNC_SECRET, DEAL_RSS_FEEDS (see DEPLOY.md).
"""

# ── stdlib ────────────────────────────────────────────────────
import os, re, math, csv, json, hashlib, secrets, logging, asyncio
import urllib.request
import urllib.parse
from time import mktime
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Optional, List, Tuple
from contextlib import asynccontextmanager

# ── third-party (see requirements_single.txt) ────────────────
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text,
    ForeignKey, create_engine, func, or_, desc, asc
)
from sqlalchemy.orm import relationship, Session, declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel, EmailStr, field_validator
from passlib.context import CryptContext
from jose import JWTError, jwt
import feedparser
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DATABASE_URL   = os.getenv("DATABASE_URL", "sqlite:///./dealflow.db")
SECRET_KEY     = os.getenv("SECRET_KEY", secrets.token_hex(32))
ALGORITHM      = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES  = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))
REFRESH_TOKEN_EXPIRE_DAYS    = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", 30))
APP_PORT       = int(os.getenv("PORT", 8000))
FEED_SYNC_INTERVAL_MINUTES = int(os.getenv("FEED_SYNC_INTERVAL_MINUTES", "30"))
ADMIN_SYNC_SECRET = os.getenv("ADMIN_SYNC_SECRET", "").strip()
NEWSAPI_KEY       = os.getenv("NEWSAPI_KEY", "").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dealflow")

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────
class UserModel(Base):
    __tablename__ = "users"
    id           = Column(Integer, primary_key=True, index=True)
    email        = Column(String, unique=True, index=True, nullable=False)
    name         = Column(String, nullable=False)
    hashed_pw    = Column(String, nullable=False)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    alerts       = relationship("AlertModel", back_populates="user", cascade="all, delete-orphan")
    bookmarks    = relationship("BookmarkModel", back_populates="user", cascade="all, delete-orphan")


class DealModel(Base):
    __tablename__ = "deals"
    id           = Column(Integer, primary_key=True, index=True)
    title        = Column(String, nullable=False)
    summary      = Column(Text)
    source_url   = Column(String)
    source_name  = Column(String)
    published_at = Column(DateTime, default=datetime.utcnow)
    sector       = Column(String, index=True)
    buyer_type   = Column(String, index=True)
    deal_size_m  = Column(Float, nullable=True)
    cross_border = Column(Boolean, default=False)
    acquirer     = Column(String, nullable=True)
    target       = Column(String, nullable=True)
    deal_score   = Column(Float, default=0.0)
    created_at   = Column(DateTime, default=datetime.utcnow)
    bookmarks    = relationship("BookmarkModel", back_populates="deal", cascade="all, delete-orphan")


class AlertModel(Base):
    __tablename__ = "alerts"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    name         = Column(String, nullable=False)
    sectors      = Column(String)          # JSON list stored as string
    buyer_types  = Column(String)
    min_size_m   = Column(Float, nullable=True)
    keywords     = Column(String)          # comma-separated
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    user         = relationship("UserModel", back_populates="alerts")


class BookmarkModel(Base):
    __tablename__ = "bookmarks"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    deal_id      = Column(Integer, ForeignKey("deals.id"), nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
    user         = relationship("UserModel", back_populates="bookmarks")
    deal         = relationship("DealModel", back_populates="bookmarks")


# ─────────────────────────────────────────────────────────────
# NLP ENGINE
# ─────────────────────────────────────────────────────────────
SECTOR_KEYWORDS = {
    "Technology":      ["software", "saas", "tech", "ai", "cloud", "data", "cyber",
                        "digital", "fintech", "semiconductor", "chip", "platform"],
    "Healthcare":      ["pharma", "biotech", "medtech", "hospital", "health", "drug",
                        "medical", "clinical", "therapeutics", "diagnostics"],
    "Energy":          ["oil", "gas", "renewable", "solar", "wind", "energy", "power",
                        "utility", "nuclear", "battery", "ev"],
    "Financial":       ["bank", "insurance", "financial", "wealth", "asset management",
                        "payments", "credit", "lending", "fintech"],
    "Consumer":        ["retail", "consumer", "brand", "food", "beverage", "ecommerce",
                        "fashion", "apparel", "restaurant", "hospitality"],
    "Industrial":      ["manufacturing", "industrial", "aerospace", "defense", "chemical",
                        "materials", "logistics", "supply chain", "automation"],
    "Infrastructure":  ["infrastructure", "real estate", "reit", "telecom", "transport",
                        "airport", "port", "rail", "broadband", "fiber"],
}

BUYER_KEYWORDS = {
    "Private Equity":   ["private equity", "pe firm", "buyout", "lbo", "kkr", "blackstone",
                         "carlyle", "apollo", "tpg", "warburg", "advent", "bain capital"],
    "Strategic":        ["acquires", "acquisition", "merger", "buys", "takeover",
                         "strategic buyer", "corp", "inc", "ltd", "plc"],
    "Cross-border":     ["cross-border", "international", "global", "foreign", "overseas",
                         "transatlantic", "asia pacific", "european"],
    "Venture":          ["venture", "vc", "seed", "series a", "series b", "startup",
                         "growth equity", "early stage"],
    "Public-to-Private":["public-to-private", "go-private", "take-private", "delisting",
                         "p2p", "take private"],
}

SIZE_PATTERNS = [
    (r"\$\s*([\d,.]+)\s*billion",  1000.0),
    (r"\$\s*([\d,.]+)\s*bn",       1000.0),
    (r"\$\s*([\d,.]+)\s*million",     1.0),
    (r"\$\s*([\d,.]+)\s*m\b",         1.0),
    (r"([\d,.]+)\s*billion\s*dollar", 1000.0),
    (r"([\d,.]+)bn",               1000.0),
    (r"([\d,.]+)m\s+deal",            1.0),
    (r"€\s*([\d,.]+)\s*billion",     1000.0),
    (r"€\s*([\d,.]+)\s*bn",          1000.0),
    (r"€\s*([\d,.]+)\s*million",       1.0),
    (r"£\s*([\d,.]+)\s*billion",     1000.0),
    (r"£\s*([\d,.]+)\s*million",       1.0),
]

def classify_sector(text: str) -> str:
    tl = text.lower()
    scores = {s: sum(1 for kw in kws if kw in tl) for s, kws in SECTOR_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General"

def classify_buyer(text: str) -> str:
    tl = text.lower()
    scores = {b: sum(1 for kw in kws if kw in tl) for b, kws in BUYER_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Strategic"

def extract_deal_size(text: str) -> Optional[float]:
    for pattern, multiplier in SIZE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                return val * multiplier
            except ValueError:
                continue
    return None

def is_cross_border(text: str) -> bool:
    cb_terms = ["cross-border", "international acquisition", "foreign buyer",
                "overseas deal", "transatlantic", "cross border"]
    tl = text.lower()
    return any(t in tl for t in cb_terms)

SECTOR_HEAT = {
    "Technology": 1.0, "Healthcare": 0.85, "Financial": 0.80,
    "Energy": 0.75, "Infrastructure": 0.70,
    "Consumer": 0.60, "Industrial": 0.55, "General": 0.30,
}

def compute_deal_score(size_m: Optional[float], cross_b: bool,
                       buyer: str, sector: str) -> float:
    # size percentile (40%)
    if size_m is None:   sp = 0.0
    elif size_m >= 5000: sp = 1.0
    elif size_m >= 1000: sp = 0.8
    elif size_m >= 500:  sp = 0.6
    elif size_m >= 100:  sp = 0.4
    else:                sp = 0.2

    cb_score     = 1.0 if cross_b else 0.0
    pe_score     = 1.0 if "Private Equity" in buyer else 0.0
    sector_score = SECTOR_HEAT.get(sector, 0.3)

    raw = sp * 40 + cb_score * 20 + pe_score * 20 + sector_score * 20
    return round(min(raw, 100.0), 1)

def classify_article(title: str, body: str = "") -> dict:
    text   = f"{title} {body}"
    sector = classify_sector(text)
    buyer  = classify_buyer(text)
    size   = extract_deal_size(text)
    cross  = is_cross_border(text)
    score  = compute_deal_score(size, cross, buyer, sector)
    return dict(sector=sector, buyer_type=buyer, deal_size_m=size,
                cross_border=cross, deal_score=score)

# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer(auto_error=False)

def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(data: dict, expires_delta: timedelta) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
) -> UserModel:
    exc = HTTPException(status_code=401, detail="Invalid or missing token")
    if creds is None:
        raise exc
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise exc
    except JWTError:
        raise exc
    user = db.query(UserModel).filter(UserModel.id == int(user_id)).first()
    if not user or not user.is_active:
        raise exc
    return user

def optional_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
) -> Optional[UserModel]:
    try:
        return get_current_user(creds, db)
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    email: EmailStr
    name: str
    password: str

    @field_validator("name")
    @classmethod
    def name_ok(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 2:
            raise ValueError("Name must be at least 2 characters")
        return v

    @field_validator("password")
    @classmethod
    def password_ok(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class UserOut(BaseModel):
    id: int
    email: str
    name: str
    created_at: datetime
    class Config: from_attributes = True

class DealOut(BaseModel):
    id: int
    title: str
    summary: Optional[str]
    source_url: Optional[str]
    source_name: Optional[str]
    published_at: datetime
    sector: Optional[str]
    buyer_type: Optional[str]
    deal_size_m: Optional[float]
    cross_border: bool
    acquirer: Optional[str]
    target: Optional[str]
    deal_score: float
    class Config: from_attributes = True

class DealListOut(BaseModel):
    items: List[DealOut]
    total: int
    page: int
    pages: int

class AlertIn(BaseModel):
    name: str
    sectors: Optional[List[str]] = None
    buyer_types: Optional[List[str]] = None
    min_size_m: Optional[float] = None
    keywords: Optional[str] = None

class AlertOut(BaseModel):
    id: int
    name: str
    sectors: Optional[str]
    buyer_types: Optional[str]
    min_size_m: Optional[float]
    keywords: Optional[str]
    is_active: bool
    created_at: datetime
    class Config: from_attributes = True

class TrendingOut(BaseModel):
    sector: str
    deal_count: int
    avg_score: float
    total_value_m: float

class FeedStatusOut(BaseModel):
    last_run: Optional[str] = None
    last_added: int = 0
    last_error: Optional[str] = None
    sync_interval_minutes: int
    rss_feed_urls: List[str]
    newsapi_configured: bool

class UserStatsOut(BaseModel):
    bookmark_count: int
    alert_count: int

# ─────────────────────────────────────────────────────────────
# SEED DATA
# ─────────────────────────────────────────────────────────────
SEED_DEALS = [
    {
        "title": "KKR acquires Hitachi's semiconductor division for $3.2 billion",
        "summary": "Private equity giant KKR has agreed to acquire Hitachi's semiconductor business in a $3.2 billion deal, marking one of the largest cross-border technology buyouts of the year.",
        "source_name": "Reuters",
        "acquirer": "KKR", "target": "Hitachi Semiconductor",
        "published_at": datetime.utcnow() - timedelta(days=1),
    },
    {
        "title": "Microsoft acquires cybersecurity startup for $680 million",
        "summary": "Microsoft has announced plans to acquire a leading cybersecurity software company valued at $680 million, strengthening its cloud security portfolio.",
        "source_name": "Bloomberg",
        "acquirer": "Microsoft", "target": "CyberShield AI",
        "published_at": datetime.utcnow() - timedelta(days=2),
    },
    {
        "title": "Blackstone closes $5.1 billion renewable energy fund buyout",
        "summary": "Blackstone's infrastructure arm has completed its acquisition of a major European renewable energy platform in a $5.1 billion LBO, going private from the Amsterdam exchange.",
        "source_name": "FT",
        "acquirer": "Blackstone", "target": "GreenPower Europe",
        "published_at": datetime.utcnow() - timedelta(days=3),
    },
    {
        "title": "Apollo Global acquires US hospital network in $2.8bn deal",
        "summary": "Apollo Global Management has agreed to take private a US regional hospital chain in a $2.8 billion public-to-private transaction, backed by healthcare sector growth.",
        "source_name": "WSJ",
        "acquirer": "Apollo Global", "target": "MidWest Health Network",
        "published_at": datetime.utcnow() - timedelta(days=4),
    },
    {
        "title": "Advent International leads $450m Series B in AI fintech startup",
        "summary": "Advent International has led a $450 million Series B round in a fast-growing AI-powered financial services platform serving SME lenders globally.",
        "source_name": "TechCrunch",
        "acquirer": "Advent International", "target": "LendGenius",
        "published_at": datetime.utcnow() - timedelta(days=5),
    },
    {
        "title": "Carlyle Group acquires German industrial automation firm for €1.1bn",
        "summary": "Carlyle Group has signed an agreement to acquire a leading German industrial automation and robotics manufacturer in a cross-border deal valued at approximately €1.1 billion.",
        "source_name": "Handelsblatt",
        "acquirer": "Carlyle Group", "target": "AutoTech GmbH",
        "published_at": datetime.utcnow() - timedelta(days=6),
    },
    {
        "title": "Salesforce merges with data analytics platform in $900m deal",
        "summary": "Salesforce has announced a strategic merger with a cloud data analytics platform in a $900 million all-stock transaction to bolster its AI and CRM capabilities.",
        "source_name": "CNBC",
        "acquirer": "Salesforce", "target": "DataSphere Inc",
        "published_at": datetime.utcnow() - timedelta(days=7),
    },
    {
        "title": "Warburg Pincus backs $320m take-private of UK pharma company",
        "summary": "Warburg Pincus has led a consortium to take private a UK-listed pharmaceutical company in a £250 million deal, with plans to accelerate its drug pipeline.",
        "source_name": "City A.M.",
        "acquirer": "Warburg Pincus", "target": "PharmaTech UK",
        "published_at": datetime.utcnow() - timedelta(days=8),
    },
    {
        "title": "Amazon Web Services acquires cloud security firm for $1.5 billion",
        "summary": "AWS has completed its acquisition of a cloud-native security company for $1.5 billion, adding zero-trust architecture capabilities to its enterprise portfolio.",
        "source_name": "Bloomberg",
        "acquirer": "Amazon Web Services", "target": "CloudSentry",
        "published_at": datetime.utcnow() - timedelta(days=9),
    },
    {
        "title": "EQT Infrastructure closes €2.3bn European fiber broadband buyout",
        "summary": "EQT Infrastructure has completed the acquisition of a European fiber broadband infrastructure operator in a €2.3 billion LBO, aiming to expand coverage across Scandinavia.",
        "source_name": "Infrastructure Investor",
        "acquirer": "EQT Infrastructure", "target": "NordFiber",
        "published_at": datetime.utcnow() - timedelta(days=10),
    },
    {
        "title": "TPG Growth invests $280m in Southeast Asian consumer brand",
        "summary": "TPG Growth has announced a $280 million growth equity investment in a Southeast Asian consumer goods company, its largest investment in the APAC region this year.",
        "source_name": "Deal Street Asia",
        "acquirer": "TPG Growth", "target": "AsiaBrands Co.",
        "published_at": datetime.utcnow() - timedelta(days=11),
    },
    {
        "title": "Bain Capital buys Australian healthcare logistics firm for $420m",
        "summary": "Bain Capital's private equity arm has agreed to acquire an Australian healthcare logistics and cold-chain distribution company for $420 million from its founding family.",
        "source_name": "AFR",
        "acquirer": "Bain Capital", "target": "MedLogistics AU",
        "published_at": datetime.utcnow() - timedelta(days=12),
    },
    {
        "title": "Google completes $750m acquisition of AI chip designer",
        "summary": "Alphabet's Google division has closed its acquisition of a semiconductor startup specialising in AI inference chips, bolstering its custom silicon strategy for data centres.",
        "source_name": "The Verge",
        "acquirer": "Google", "target": "InferChip",
        "published_at": datetime.utcnow() - timedelta(days=13),
    },
    {
        "title": "Ares Management buys US solar energy portfolio for $1.85bn",
        "summary": "Ares Management's infrastructure fund has completed the acquisition of a large US utility-scale solar energy portfolio in a $1.85 billion deal, expanding its clean energy assets.",
        "source_name": "Reuters",
        "acquirer": "Ares Management", "target": "SunGrid Portfolio",
        "published_at": datetime.utcnow() - timedelta(days=14),
    },
    {
        "title": "CVC Capital Partners acquires European insurance broker for €600m",
        "summary": "CVC Capital Partners has agreed to acquire a pan-European insurance brokerage group in a €600 million leveraged buyout, betting on consolidation in the specialty insurance market.",
        "source_name": "FT",
        "acquirer": "CVC Capital Partners", "target": "EuroInsure Group",
        "published_at": datetime.utcnow() - timedelta(days=15),
    },
]


def seed_database(db: Session):
    if db.query(DealModel).count() > 0:
        return
    log.info("Seeding database with sample deals...")
    for d in SEED_DEALS:
        text    = d["title"] + " " + d.get("summary", "")
        clf     = classify_article(d["title"], d.get("summary", ""))
        deal    = DealModel(
            title        = d["title"],
            summary      = d.get("summary"),
            source_url   = f"https://example.com/deal-{hashlib.md5(d['title'].encode()).hexdigest()[:8]}",
            source_name  = d.get("source_name", "Reuters"),
            published_at = d.get("published_at", datetime.utcnow()),
            acquirer     = d.get("acquirer"),
            target       = d.get("target"),
            **clf,
        )
        db.add(deal)
    db.commit()
    log.info(f"Seeded {len(SEED_DEALS)} deals.")


# ─────────────────────────────────────────────────────────────
# LIVE FEEDS (RSS + optional NewsAPI)
# ─────────────────────────────────────────────────────────────
MA_HEADLINE_HINT = re.compile(
    r"acqui(r|s|ring|red|res)|merger|m\s*&\s*a|buyout|\blbo\b|take[-\s]?private|takeover|"
    r"\bbuys\b|\bpurchases\b|to\s+buy\s+|purchase\s+of|invests?\s+[$\d]|valued\s+at|"
    r"go(ing)?[-\s]private|strategic\s+(deal|buyer)|minority\s+stake|majority\s+stake|"
    r"leans\s+in|backs\s+[$\d]|close[sd]?\s+(the\s+)?(deal|acquisition)",
    re.I,
)

DEFAULT_RSS_FEEDS = [
    # Curated public RSS — swap for paid wires via DEAL_RSS_FEEDS
    "https://news.google.com/rss/search?q=mergers+acquisitions+buyout&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/news/rssindex",
]

FEED_REQUEST_HEADERS = {
    "User-Agent": "DealFlowAI/1.0 (+https://github.com/dealflow-ai; research bot)",
    "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
}


def _configured_feed_urls() -> List[str]:
    raw = os.getenv("DEAL_RSS_FEEDS", "").strip()
    if not raw:
        return list(DEFAULT_RSS_FEEDS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def _feed_label_from_url(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host.replace("www.", "") or "RSS"
    except Exception:
        return "RSS"


def _entry_datetime(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, ValueError, TypeError):
                continue
    return datetime.utcnow()


def _is_ma_headline(title: str, summary: str) -> bool:
    return bool(MA_HEADLINE_HINT.search(f"{title} {summary or ''}"))

def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", " ", s).strip()


def _fetch_rss(url: str):
    req = urllib.request.Request(url, headers=FEED_REQUEST_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return feedparser.parse(resp.read())


def ingest_rss_feeds(db: Session, feed_urls: Optional[List[str]] = None) -> int:
    added = 0
    for url in (feed_urls or _configured_feed_urls()):
        label = _feed_label_from_url(url)
        try:
            parsed = _fetch_rss(url)
            for entry in parsed.entries[:80]:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                link = (entry.get("link") or "").strip() or None
                summary = _strip_html(
                    entry.get("summary") or entry.get("description") or ""
                )[:4000]
                if not _is_ma_headline(title, summary):
                    continue
                if not link:
                    link = f"urn:feed:{hashlib.sha256(title.encode()).hexdigest()[:24]}"
                if db.query(DealModel.id).filter(DealModel.source_url == link).first():
                    continue
                clf = classify_article(title, summary)
                deal = DealModel(
                    title=title,
                    summary=summary or None,
                    source_url=link,
                    source_name=parsed.feed.get("title") or label,
                    published_at=_entry_datetime(entry),
                    acquirer=None,
                    target=None,
                    **clf,
                )
                db.add(deal)
                added += 1
        except Exception as e:
            log.warning("RSS ingest failed for %s: %s", url, e)
    return added


def ingest_newsapi(db: Session) -> int:
    if not NEWSAPI_KEY:
        return 0
    added = 0
    q = os.getenv(
        "NEWSAPI_QUERY",
        '("merger" OR "acquisition" OR buyout OR "take private")',
    )
    params = urllib.parse.urlencode(
        {
            "q": q,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 40,
            "apiKey": NEWSAPI_KEY,
        }
    )
    url = f"https://newsapi.org/v2/everything?{params}"
    req = urllib.request.Request(url, headers=FEED_REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("NewsAPI request failed: %s", e)
        return 0
    if payload.get("status") != "ok":
        log.warning("NewsAPI error: %s", payload.get("message") or payload)
        return 0
    for art in payload.get("articles") or []:
        title = (art.get("title") or "").strip()
        if not title:
            continue
        summary = (art.get("description") or "")[:4000]
        if not _is_ma_headline(title, summary):
            continue
        link = (art.get("url") or "").strip()
        if not link:
            continue
        if db.query(DealModel.id).filter(DealModel.source_url == link).first():
            continue
        pub = art.get("publishedAt")
        try:
            published_at = (
                datetime.fromisoformat(pub.replace("Z", "+00:00")).replace(tzinfo=None)
                if pub
                else datetime.utcnow()
            )
        except ValueError:
            published_at = datetime.utcnow()
        src = (art.get("source") or {}).get("name") or "NewsAPI"
        clf = classify_article(title, summary)
        db.add(
            DealModel(
                title=title,
                summary=summary or None,
                source_url=link,
                source_name=src,
                published_at=published_at,
                acquirer=None,
                target=None,
                **clf,
            )
        )
        added += 1
    return added


FEED_SYNC_STATE = {
    "last_run": None,
    "last_added": 0,
    "last_error": None,
}


def run_live_feed_sync() -> Tuple[int, Optional[str]]:
    """Pull RSS + optional NewsAPI into DB. Returns (new_row_count, error)."""
    db = SessionLocal()
    err = None
    total = 0
    try:
        total += ingest_rss_feeds(db)
        total += ingest_newsapi(db)
        db.commit()
    except Exception as e:
        db.rollback()
        err = str(e)
        log.exception("Live feed sync failed")
    finally:
        db.close()
    FEED_SYNC_STATE["last_run"] = datetime.utcnow().isoformat() + "Z"
    FEED_SYNC_STATE["last_added"] = total if err is None else 0
    FEED_SYNC_STATE["last_error"] = err
    return (total, err)


# ─────────────────────────────────────────────────────────────
# APP LIFECYCLE
# ─────────────────────────────────────────────────────────────
_feed_sync_task: Optional[asyncio.Task] = None


async def _feed_sync_loop():
    await asyncio.sleep(8)
    while True:
        try:
            added, err = await asyncio.to_thread(run_live_feed_sync)
            if err:
                log.error("Scheduled feed sync error: %s", err)
            else:
                log.info("Scheduled feed sync: %s new deals", added)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduled feed sync crashed")
        await asyncio.sleep(max(1, FEED_SYNC_INTERVAL_MINUTES) * 60)


# ─────────────────────────────────────────────────────────────
# APP LIFESPAN
# ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _feed_sync_task
    Base.metadata.create_all(bind=engine)
    if os.getenv("RAILWAY_ENVIRONMENT") and "sqlite" in (DATABASE_URL or "").lower():
        log.warning(
            "DATABASE_URL is SQLite on Railway — user signups may be lost on redeploy. "
            "Add the PostgreSQL plugin so Railway injects a persistent DATABASE_URL."
        )
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    if FEED_SYNC_INTERVAL_MINUTES > 0:
        _feed_sync_task = asyncio.create_task(_feed_sync_loop())
    log.info(f"DealFlow AI ready → http://localhost:{APP_PORT}")
    yield
    if _feed_sync_task:
        _feed_sync_task.cancel()
        try:
            await _feed_sync_task
        except asyncio.CancelledError:
            pass

# ─────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="DealFlow AI",
    version="1.0.0",
    description="M&A Intelligence Platform",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────────────────────
@app.post("/api/v1/auth/register", response_model=TokenOut, tags=["Auth"])
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if db.query(UserModel).filter(UserModel.email == str(body.email)).first():
        raise HTTPException(400, "Email already registered")
    user = UserModel(
        email     = str(body.email),
        name      = body.name,
        hashed_pw = hash_password(body.password),
    )
    db.add(user); db.commit(); db.refresh(user)
    return _issue_tokens(user)

@app.post("/api/v1/auth/login", response_model=TokenOut, tags=["Auth"])
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(UserModel).filter(UserModel.email == str(body.email)).first()
    if not user or not verify_password(body.password, user.hashed_pw):
        raise HTTPException(401, "Invalid credentials")
    return _issue_tokens(user)

@app.post("/api/v1/auth/refresh", response_model=TokenOut, tags=["Auth"])
def refresh_token(creds: HTTPAuthorizationCredentials = Depends(bearer),
                  db: Session = Depends(get_db)):
    exc = HTTPException(401, "Invalid refresh token")
    if creds is None: raise exc
    try:
        payload  = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id  = payload.get("sub")
        tok_type = payload.get("type")
        if tok_type != "refresh": raise exc
    except JWTError:
        raise exc
    user = db.query(UserModel).filter(UserModel.id == int(user_id)).first()
    if not user: raise exc
    return _issue_tokens(user)

@app.get("/api/v1/auth/me", response_model=UserOut, tags=["Auth"])
def me(user: UserModel = Depends(get_current_user)):
    return user

@app.get("/api/v1/auth/stats", response_model=UserStatsOut, tags=["Auth"])
def auth_stats(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)):
    bc = db.query(func.count(BookmarkModel.id)).filter(BookmarkModel.user_id == user.id).scalar()
    ac = db.query(func.count(AlertModel.id)).filter(AlertModel.user_id == user.id).scalar()
    return UserStatsOut(bookmark_count=int(bc or 0), alert_count=int(ac or 0))

def _issue_tokens(user: UserModel) -> dict:
    access  = create_token({"sub": str(user.id), "type": "access"},
                           timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    refresh = create_token({"sub": str(user.id), "type": "refresh"},
                           timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}

# ─────────────────────────────────────────────────────────────
# DEALS ROUTES
# ─────────────────────────────────────────────────────────────
@app.get("/api/v1/deals", response_model=DealListOut, tags=["Deals"])
def list_deals(
    sector:       Optional[str]   = None,
    buyer_type:   Optional[str]   = None,
    min_size:     Optional[float] = None,
    max_size:     Optional[float] = None,
    cross_border: Optional[bool]  = None,
    date_from:    Optional[str]   = None,
    date_to:      Optional[str]   = None,
    search:       Optional[str]   = None,
    sort_by:      str             = "published_at",
    page:         int             = 1,
    page_size:    int             = 12,
    db: Session = Depends(get_db),
):
    q = db.query(DealModel)
    if sector:       q = q.filter(DealModel.sector == sector)
    if buyer_type:   q = q.filter(DealModel.buyer_type == buyer_type)
    if min_size:     q = q.filter(DealModel.deal_size_m >= min_size)
    if max_size:     q = q.filter(DealModel.deal_size_m <= max_size)
    if cross_border is not None:
                     q = q.filter(DealModel.cross_border == cross_border)
    if date_from:
        q = q.filter(DealModel.published_at >= datetime.fromisoformat(date_from))
    if date_to:
        q = q.filter(DealModel.published_at <= datetime.fromisoformat(date_to))
    if search:
        s = f"%{search}%"
        q = q.filter(or_(DealModel.title.ilike(s), DealModel.summary.ilike(s),
                         DealModel.acquirer.ilike(s), DealModel.target.ilike(s)))

    sort_col = {
        "published_at": desc(DealModel.published_at),
        "deal_score":   desc(DealModel.deal_score),
        "deal_size_m":  desc(DealModel.deal_size_m),
    }.get(sort_by, desc(DealModel.published_at))
    q = q.order_by(sort_col)

    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return DealListOut(items=items, total=total, page=page,
                       pages=math.ceil(total / page_size))

@app.get("/api/v1/deals/trending", tags=["Deals"])
def trending_deals(db: Session = Depends(get_db)):
    results = (
        db.query(
            DealModel.sector,
            func.count(DealModel.id).label("deal_count"),
            func.avg(DealModel.deal_score).label("avg_score"),
            func.coalesce(func.sum(DealModel.deal_size_m), 0).label("total_value_m"),
        )
        .filter(DealModel.published_at >= datetime.utcnow() - timedelta(days=30))
        .group_by(DealModel.sector)
        .order_by(desc("deal_count"))
        .all()
    )
    return [
        TrendingOut(
            sector=r.sector or "General",
            deal_count=r.deal_count,
            avg_score=round(r.avg_score or 0, 1),
            total_value_m=round(r.total_value_m or 0, 1),
        )
        for r in results
    ]

@app.get("/api/v1/deals/export", tags=["Deals"])
def export_deals(db: Session = Depends(get_db),
                 user: UserModel = Depends(get_current_user)):
    deals = db.query(DealModel).order_by(desc(DealModel.published_at)).limit(500).all()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["ID","Title","Acquirer","Target","Sector","Buyer Type",
                "Deal Size ($M)","Cross Border","Score","Published","Source"])
    for d in deals:
        w.writerow([d.id, d.title, d.acquirer, d.target, d.sector, d.buyer_type,
                    d.deal_size_m, d.cross_border, d.deal_score,
                    d.published_at.date(), d.source_name])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=dealflow_export.csv"},
    )

@app.get("/api/v1/deals/{deal_id}", response_model=DealOut, tags=["Deals"])
def get_deal(deal_id: int, db: Session = Depends(get_db)):
    d = db.query(DealModel).filter(DealModel.id == deal_id).first()
    if not d: raise HTTPException(404, "Deal not found")
    return d

@app.post("/api/v1/deals/{deal_id}/bookmark", tags=["Deals"])
def bookmark_deal(deal_id: int, db: Session = Depends(get_db),
                  user: UserModel = Depends(get_current_user)):
    deal = db.query(DealModel).filter(DealModel.id == deal_id).first()
    if not deal: raise HTTPException(404, "Deal not found")
    existing = db.query(BookmarkModel).filter_by(user_id=user.id, deal_id=deal_id).first()
    if existing:
        db.delete(existing); db.commit()
        return {"bookmarked": False}
    db.add(BookmarkModel(user_id=user.id, deal_id=deal_id))
    db.commit()
    return {"bookmarked": True}

@app.get("/api/v1/deals/bookmarks", response_model=List[DealOut], tags=["Deals"])
def list_bookmarked_deals(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(DealModel)
        .join(BookmarkModel, BookmarkModel.deal_id == DealModel.id)
        .filter(BookmarkModel.user_id == user.id)
        .order_by(desc(BookmarkModel.created_at))
        .all()
    )

# ─────────────────────────────────────────────────────────────
# ALERTS ROUTES
# ─────────────────────────────────────────────────────────────
@app.get("/api/v1/alerts", response_model=List[AlertOut], tags=["Alerts"])
def list_alerts(user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(AlertModel).filter(AlertModel.user_id == user.id).all()

@app.post("/api/v1/alerts", response_model=AlertOut, tags=["Alerts"])
def create_alert(body: AlertIn, user: UserModel = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    a = AlertModel(
        user_id    = user.id,
        name       = body.name,
        sectors    = json.dumps(body.sectors) if body.sectors else None,
        buyer_types= json.dumps(body.buyer_types) if body.buyer_types else None,
        min_size_m = body.min_size_m,
        keywords   = body.keywords,
    )
    db.add(a); db.commit(); db.refresh(a)
    return a

@app.patch("/api/v1/alerts/{alert_id}", response_model=AlertOut, tags=["Alerts"])
def update_alert(alert_id: int, body: AlertIn,
                 user: UserModel = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    a = db.query(AlertModel).filter(AlertModel.id == alert_id,
                                    AlertModel.user_id == user.id).first()
    if not a: raise HTTPException(404, "Alert not found")
    a.name       = body.name
    a.sectors    = json.dumps(body.sectors) if body.sectors else None
    a.buyer_types = json.dumps(body.buyer_types) if body.buyer_types else None
    a.min_size_m = body.min_size_m
    a.keywords   = body.keywords
    db.commit(); db.refresh(a)
    return a

@app.delete("/api/v1/alerts/{alert_id}", tags=["Alerts"])
def delete_alert(alert_id: int, user: UserModel = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    a = db.query(AlertModel).filter(AlertModel.id == alert_id,
                                    AlertModel.user_id == user.id).first()
    if not a: raise HTTPException(404)
    db.delete(a); db.commit()
    return {"deleted": True}

# ─────────────────────────────────────────────────────────────
# COMPANIES ROUTE
# ─────────────────────────────────────────────────────────────
@app.get("/api/v1/companies/{name}", response_model=List[DealOut], tags=["Companies"])
def company_deals(name: str, db: Session = Depends(get_db)):
    n = f"%{name}%"
    return (
        db.query(DealModel)
        .filter(or_(DealModel.acquirer.ilike(n), DealModel.target.ilike(n)))
        .order_by(desc(DealModel.published_at))
        .all()
    )

# ─────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["System"])
def health():
    return {"status": "ok", "version": "1.0.0", "timestamp": datetime.utcnow()}

@app.get("/api/v1/system/feeds", response_model=FeedStatusOut, tags=["System"])
def feeds_status():
    return FeedStatusOut(
        last_run=FEED_SYNC_STATE.get("last_run"),
        last_added=int(FEED_SYNC_STATE.get("last_added") or 0),
        last_error=FEED_SYNC_STATE.get("last_error"),
        sync_interval_minutes=FEED_SYNC_INTERVAL_MINUTES,
        rss_feed_urls=_configured_feed_urls(),
        newsapi_configured=bool(NEWSAPI_KEY),
    )

@app.post("/api/v1/system/sync-feeds", tags=["System"])
def trigger_feed_sync(
    x_admin_sync_secret: Optional[str] = Header(None, alias="X-Admin-Sync-Secret"),
):
    """
    Run ingestion immediately (RSS + optional NewsAPI). On Railway, call from a
    Cron job or workflow. Set ADMIN_SYNC_SECRET and pass it in this header.
    """
    if ADMIN_SYNC_SECRET:
        if not x_admin_sync_secret or x_admin_sync_secret != ADMIN_SYNC_SECRET:
            raise HTTPException(401, "Missing or invalid X-Admin-Sync-Secret")
    added, err = run_live_feed_sync()
    if err:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err)
    return {"ok": True, "added": added, "last_run": FEED_SYNC_STATE["last_run"]}

# ─────────────────────────────────────────────────────────────
# FRONTEND (served at root — full embedded SPA)
# ─────────────────────────────────────────────────────────────
FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DealFlow AI</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Mono:wght@400;500&family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
/* ── RESET & ROOT ───────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:       #0a0c0f;
  --surface:  #111418;
  --border:   #1e2329;
  --border2:  #2a3040;
  --text:     #e8eaf0;
  --muted:    #6b7280;
  --accent:   #00d4aa;
  --accent2:  #7c3aed;
  --gold:     #f59e0b;
  --red:      #ef4444;
  --green:    #22c55e;
  --font-head:'DM Serif Display',serif;
  --font-body:'Manrope',sans-serif;
  --font-mono:'DM Mono',monospace;
  --r:10px; --r2:6px;
  --shadow: 0 4px 24px rgba(0,0,0,.5);
}
html{scroll-behavior:smooth}
body{
  background:var(--bg);
  color:var(--text);
  font-family:var(--font-body);
  font-size:14px;
  line-height:1.6;
  min-height:100vh;
  overflow-x:hidden;
}

/* ── NOISE TEXTURE OVERLAY ─────────────────────────────── */
body::before{
  content:'';
  position:fixed;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
  pointer-events:none;z-index:0;opacity:.4;
}

/* ── SCROLLBAR ─────────────────────────────────────────── */
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:4px}

/* ── LAYOUT ─────────────────────────────────────────────── */
#app{position:relative;z-index:1;display:flex;flex-direction:column;min-height:100vh}

/* ── TOPBAR ──────────────────────────────────────────────── */
header{
  position:sticky;top:0;z-index:100;
  background:rgba(10,12,15,.92);
  backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);
  padding:0 24px;
  height:60px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{
  font-family:var(--font-head);
  font-size:22px;
  color:var(--text);
  letter-spacing:-.02em;
  display:flex;align-items:center;gap:8px;
}
.logo-dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--accent);
  box-shadow:0 0 12px var(--accent);
  animation:pulse 2s infinite;
}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.nav-actions{display:flex;gap:10px;align-items:center}
.nav-pill{
  padding:6px 14px;border-radius:20px;font-size:13px;
  font-family:var(--font-mono);font-weight:500;cursor:pointer;
  border:1px solid var(--border2);background:transparent;
  color:var(--muted);transition:all .2s;
}
.nav-pill:hover{border-color:var(--accent);color:var(--accent)}
.nav-pill.active{background:var(--accent);border-color:var(--accent);color:#000;font-weight:600}
a.nav-pill{text-decoration:none;display:inline-flex;align-items:center;justify-content:center}
#user-badge{
  display:none;
  align-items:center;gap:8px;
  padding:4px 12px 4px 4px;
  background:var(--surface);
  border:1px solid var(--border2);
  border-radius:20px;
  font-size:13px;color:var(--muted);cursor:pointer;
}
#user-badge .avatar{
  width:28px;height:28px;border-radius:50%;
  background:linear-gradient(135deg,var(--accent2),var(--accent));
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;color:#fff;
}

/* ── MAIN ────────────────────────────────────────────────── */
main{flex:1;padding:32px 24px;max-width:1400px;width:100%;margin:0 auto}

/* ── HERO ────────────────────────────────────────────────── */
.hero{
  text-align:center;
  padding:60px 0 40px;
  animation:fadeUp .6s ease both;
}
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.hero-tag{
  display:inline-block;
  font-family:var(--font-mono);
  font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--accent);border:1px solid rgba(0,212,170,.3);
  padding:4px 14px;border-radius:20px;margin-bottom:20px;
}
.hero h1{
  font-family:var(--font-head);
  font-size:clamp(36px,6vw,72px);
  line-height:1.08;
  letter-spacing:-.03em;
  color:var(--text);
  margin-bottom:16px;
}
.hero h1 em{
  font-style:italic;color:var(--accent);
  text-shadow:0 0 40px rgba(0,212,170,.3);
}
.hero p{
  font-size:16px;color:var(--muted);
  max-width:500px;margin:0 auto 32px;line-height:1.7;
}
.hero-stats{
  display:flex;justify-content:center;gap:40px;flex-wrap:wrap;
}
.stat{text-align:center}
.stat-num{
  font-family:var(--font-mono);font-size:28px;font-weight:500;
  color:var(--text);letter-spacing:-.02em;
}
.stat-num span{color:var(--accent)}
.stat-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}

/* ── SEARCH BAR ─────────────────────────────────────────── */
.search-wrap{
  max-width:700px;margin:0 auto 32px;
  position:relative;
}
.search-wrap input{
  width:100%;padding:14px 20px 14px 48px;
  background:var(--surface);border:1px solid var(--border2);
  border-radius:var(--r);color:var(--text);font-family:var(--font-body);
  font-size:15px;outline:none;transition:border-color .2s;
}
.search-wrap input:focus{border-color:var(--accent)}
.search-wrap input::placeholder{color:var(--muted)}
.search-icon{
  position:absolute;left:16px;top:50%;transform:translateY(-50%);
  color:var(--muted);font-size:18px;pointer-events:none;
}

/* ── FILTERS ─────────────────────────────────────────────── */
.filters{
  display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;
  align-items:center;
}
.filter-chip{
  padding:6px 14px;border-radius:20px;font-size:12px;font-weight:500;
  font-family:var(--font-mono);cursor:pointer;border:1px solid var(--border2);
  background:transparent;color:var(--muted);transition:all .15s;white-space:nowrap;
}
.filter-chip:hover{border-color:var(--accent);color:var(--accent)}
.filter-chip.active{background:rgba(0,212,170,.12);border-color:var(--accent);color:var(--accent)}
.filter-chip.sort-chip.active{background:rgba(124,58,237,.12);border-color:var(--accent2);color:var(--accent2)}
.filter-divider{width:1px;height:24px;background:var(--border2);margin:0 4px}
.filter-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-right:4px}

/* ── GRID ────────────────────────────────────────────────── */
#deals-grid,#saved-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
  gap:16px;
  animation:fadeUp .4s ease both;
}

/* ── DEAL CARD ──────────────────────────────────────────── */
.deal-card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--r);
  padding:20px;
  cursor:pointer;
  transition:border-color .2s, transform .15s, box-shadow .2s;
  position:relative;
  overflow:hidden;
}
.deal-card::before{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(0,212,170,.03),transparent 60%);
  opacity:0;transition:opacity .2s;
}
.deal-card:hover{
  border-color:var(--border2);
  transform:translateY(-2px);
  box-shadow:0 8px 32px rgba(0,0,0,.4);
}
.deal-card:hover::before{opacity:1}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}
.card-badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{
  padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;
  font-family:var(--font-mono);letter-spacing:.04em;text-transform:uppercase;
}
.badge-sector{background:rgba(0,212,170,.1);color:var(--accent);border:1px solid rgba(0,212,170,.2)}
.badge-buyer{background:rgba(124,58,237,.1);color:#a78bfa;border:1px solid rgba(124,58,237,.2)}
.badge-cb{background:rgba(245,158,11,.1);color:var(--gold);border:1px solid rgba(245,158,11,.2)}
.card-score{
  font-family:var(--font-mono);font-size:11px;font-weight:600;
  color:var(--muted);white-space:nowrap;
  display:flex;align-items:center;gap:4px;
  flex-shrink:0;
}
.score-bar{
  width:32px;height:3px;border-radius:2px;background:var(--border2);overflow:hidden;
}
.score-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent2),var(--accent))}
.card-title{
  font-size:15px;font-weight:600;line-height:1.4;
  color:var(--text);margin-bottom:8px;
}
.card-summary{
  font-size:13px;color:var(--muted);line-height:1.6;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  margin-bottom:14px;
}
.card-footer{
  display:flex;justify-content:space-between;align-items:center;
  border-top:1px solid var(--border);padding-top:12px;
}
.card-meta{display:flex;gap:14px;align-items:center}
.meta-item{font-size:11px;color:var(--muted);font-family:var(--font-mono)}
.meta-item strong{color:var(--text);font-weight:600}
.deal-size{
  font-family:var(--font-mono);font-size:13px;font-weight:600;
  color:var(--accent);
}
.source-tag{
  font-size:10px;color:var(--muted);padding:2px 7px;
  border:1px solid var(--border);border-radius:3px;
  font-family:var(--font-mono);
}
.bookmark-btn{
  background:none;border:none;cursor:pointer;
  color:var(--muted);font-size:16px;padding:2px;
  transition:color .15s;
}
.bookmark-btn:hover{color:var(--gold)}
.bookmark-btn.active{color:var(--gold)}

/* ── TRENDING ────────────────────────────────────────────── */
#trending-section{
  margin-bottom:32px;
  animation:fadeUp .5s ease .1s both;
}
.section-header{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:16px;
}
.section-title{
  font-size:13px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.1em;font-family:var(--font-mono);
  display:flex;align-items:center;gap:8px;
}
.section-title::before{
  content:'';width:12px;height:1px;background:var(--accent);
}
.trending-row{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px}
.trending-row::-webkit-scrollbar{height:2px}
.trend-card{
  flex-shrink:0;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r2);padding:14px 18px;
  min-width:180px;transition:border-color .2s;cursor:default;
}
.trend-card:hover{border-color:var(--border2)}
.trend-sector{font-size:13px;font-weight:700;color:var(--text);margin-bottom:8px}
.trend-stats{display:flex;gap:16px}
.trend-stat{font-size:11px;color:var(--muted);font-family:var(--font-mono)}
.trend-stat strong{display:block;font-size:15px;color:var(--text);font-weight:600}
.trend-bar-wrap{margin-top:8px;height:2px;background:var(--border);border-radius:1px}
.trend-bar{height:100%;border-radius:1px;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .6s ease}

/* ── PAGINATION ─────────────────────────────────────────── */
.pagination{
  display:flex;justify-content:center;align-items:center;gap:8px;
  margin-top:32px;
}
.page-btn{
  width:36px;height:36px;border-radius:var(--r2);
  background:var(--surface);border:1px solid var(--border);
  color:var(--muted);cursor:pointer;font-family:var(--font-mono);font-size:13px;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;
}
.page-btn:hover{border-color:var(--border2);color:var(--text)}
.page-btn.active{background:var(--accent);border-color:var(--accent);color:#000;font-weight:700}
.page-btn:disabled{opacity:.3;cursor:default;pointer-events:none}
.page-info{font-family:var(--font-mono);font-size:12px;color:var(--muted);padding:0 8px}

/* ── MODAL ───────────────────────────────────────────────── */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.75);
  backdrop-filter:blur(6px);z-index:200;
  display:flex;align-items:center;justify-content:center;
  padding:24px;opacity:0;pointer-events:none;transition:opacity .2s;
}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{
  background:var(--surface);border:1px solid var(--border2);
  border-radius:14px;max-width:580px;width:100%;
  max-height:85vh;overflow-y:auto;
  transform:translateY(16px);transition:transform .2s;
}
.modal-overlay.open .modal{transform:translateY(0)}
.modal-header{
  padding:24px 24px 16px;
  border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:flex-start;
  position:sticky;top:0;background:var(--surface);
}
.modal-close{
  background:none;border:none;color:var(--muted);cursor:pointer;
  font-size:20px;padding:0;line-height:1;transition:color .15s;
}
.modal-close:hover{color:var(--text)}
.modal-body{padding:24px}
.modal-label{
  font-size:11px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em;font-family:var(--font-mono);
  margin-bottom:6px;
}
.modal-value{font-size:15px;color:var(--text);margin-bottom:20px;line-height:1.6}
.modal-badges{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.modal-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.modal-actions{display:flex;gap:10px;padding-top:16px;border-top:1px solid var(--border)}
.btn{
  padding:9px 20px;border-radius:var(--r2);font-size:13px;font-weight:600;
  cursor:pointer;border:none;transition:all .15s;font-family:var(--font-body);
}
.btn-primary{background:var(--accent);color:#000}
.btn-primary:hover{background:#00f0c0}
.btn-outline{background:transparent;border:1px solid var(--border2);color:var(--muted)}
.btn-outline:hover{border-color:var(--text);color:var(--text)}

/* ── AUTH PANEL ──────────────────────────────────────────── */
#auth-panel{
  position:fixed;right:0;top:0;bottom:0;width:380px;
  background:var(--surface);border-left:1px solid var(--border2);
  z-index:150;transform:translateX(100%);transition:transform .3s cubic-bezier(.4,0,.2,1);
  padding:0;overflow-y:auto;
}
#auth-panel.open{transform:translateX(0)}
.auth-inner{padding:40px 32px}
.auth-title{font-family:var(--font-head);font-size:28px;color:var(--text);margin-bottom:8px}
.auth-sub{color:var(--muted);font-size:14px;margin-bottom:32px}
.form-field{margin-bottom:18px}
.form-field label{
  display:block;font-size:12px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.07em;margin-bottom:7px;
  font-family:var(--font-mono);
}
.form-field input{
  width:100%;padding:11px 14px;background:var(--bg);
  border:1px solid var(--border2);border-radius:var(--r2);
  color:var(--text);font-size:14px;font-family:var(--font-body);
  outline:none;transition:border-color .2s;
}
.form-field input:focus{border-color:var(--accent)}
.form-error{font-size:12px;color:var(--red);margin-top:6px;display:none}
.auth-switch{
  font-size:13px;color:var(--muted);margin-top:20px;text-align:center;
}
.auth-switch a{color:var(--accent);cursor:pointer;text-decoration:none}
.auth-close{
  position:absolute;top:20px;right:20px;
  background:none;border:none;color:var(--muted);font-size:20px;
  cursor:pointer;padding:4px;line-height:1;
}

/* ── ALERTS PAGE ─────────────────────────────────────────── */
#alerts-page{display:none;animation:fadeUp .4s ease both}
.alert-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:12px;transition:border-color .2s;
}
.alert-card:hover{border-color:var(--border2)}
.alert-name{font-weight:600;font-size:15px;color:var(--text);margin-bottom:4px}
.alert-details{font-size:12px;color:var(--muted);font-family:var(--font-mono)}
.alert-actions{display:flex;gap:8px}
.btn-danger{
  padding:6px 14px;border-radius:var(--r2);font-size:12px;
  background:transparent;border:1px solid rgba(239,68,68,.3);color:var(--red);
  cursor:pointer;transition:all .15s;
}
.btn-danger:hover{background:rgba(239,68,68,.1)}
.new-alert-form{
  background:var(--surface);border:1px solid var(--border2);
  border-radius:var(--r);padding:24px;margin-bottom:24px;
}
.new-alert-form h3{font-size:14px;font-weight:700;color:var(--text);margin-bottom:18px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.inline-input{
  width:100%;padding:9px 12px;background:var(--bg);
  border:1px solid var(--border2);border-radius:var(--r2);
  color:var(--text);font-size:13px;font-family:var(--font-body);outline:none;
}
.inline-input:focus{border-color:var(--accent)}

/* ── TOAST ───────────────────────────────────────────────── */
#toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
  background:var(--surface);border:1px solid var(--border2);
  padding:10px 20px;border-radius:var(--r2);font-size:13px;color:var(--text);
  z-index:999;opacity:0;transition:all .3s;pointer-events:none;
  font-family:var(--font-mono);white-space:nowrap;
  box-shadow:var(--shadow);
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.success{border-color:var(--accent);color:var(--accent)}
#toast.error{border-color:var(--red);color:var(--red)}

/* ── LOADING ─────────────────────────────────────────────── */
.skeleton{
  background:linear-gradient(90deg,var(--surface) 25%,var(--border) 50%,var(--surface) 75%);
  background-size:200% 100%;
  animation:shimmer 1.5s infinite;
  border-radius:4px;
}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
.empty-state{
  text-align:center;padding:80px 24px;color:var(--muted);
  grid-column:1/-1;
}
.empty-icon{font-size:48px;margin-bottom:16px}
.empty-state h3{font-size:18px;color:var(--text);margin-bottom:8px}

/* ── RESPONSIVE ──────────────────────────────────────────── */
@media(max-width:640px){
  main{padding:20px 16px}
  .hero{padding:32px 0 24px}
  #deals-grid{grid-template-columns:1fr}
  .hero-stats{gap:24px}
  #auth-panel{width:100%}
  .modal-grid{grid-template-columns:1fr}
  .form-row{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div id="app">

<!-- HEADER -->
<header>
  <div class="logo">
    <div class="logo-dot"></div>
    DealFlow <em style="font-style:italic;color:var(--accent)">AI</em>
  </div>
  <div class="nav-actions">
    <button type="button" class="nav-pill active" id="nav-deals" onclick="showPage('deals', this)">Deals</button>
    <button type="button" class="nav-pill" id="nav-saved" onclick="showPage('saved', this)">Saved</button>
    <button type="button" class="nav-pill" id="nav-alerts" onclick="showPage('alerts', this)">Alerts</button>
    <div id="user-badge" onclick="logout()" title="Click to sign out">
      <div class="avatar" id="user-avatar">U</div>
      <span id="user-name-badge">User</span>
      <span id="user-stats-hint" style="font-size:10px;color:var(--muted);margin-left:6px"></span>
      <span style="font-size:10px;color:var(--muted)">⏻</span>
    </div>
    <a href="/login" class="nav-pill" id="login-btn">Sign In</a>
    <a href="/register" class="nav-pill active" id="register-btn">Sign up</a>
  </div>
</header>

<!-- MAIN -->
<main id="main-content">

  <!-- DEALS PAGE -->
  <div id="deals-page">
    <div class="hero">
      <div class="hero-tag">⚡ Live M&A Intelligence</div>
      <h1>Track Every <em>Deal</em><br>Before Your Competition</h1>
      <p>AI-powered M&A signal detection across 50+ news sources. Classify, score, and alert on every transaction that matters.</p>
      <div class="hero-stats">
        <div class="stat"><div class="stat-num" id="stat-deals">—</div><div class="stat-label">Deals Tracked</div></div>
        <div class="stat"><div class="stat-num">7 <span>Sectors</span></div><div class="stat-label">Coverage</div></div>
        <div class="stat"><div class="stat-num">15<span>min</span></div><div class="stat-label">Update Cycle</div></div>
      </div>
    </div>

    <!-- TRENDING -->
    <div id="trending-section">
      <div class="section-header">
        <div class="section-title">Sector Momentum</div>
      </div>
      <div class="trending-row" id="trending-row">
        <div class="trend-card"><div class="skeleton" style="height:80px;width:150px"></div></div>
      </div>
    </div>

    <!-- SEARCH -->
    <div class="search-wrap">
      <span class="search-icon">⌕</span>
      <input type="text" id="search-input" placeholder="Search deals, companies, sectors…"
        oninput="onSearch(this.value)"/>
    </div>

    <!-- FILTERS -->
    <div class="filters">
      <span class="filter-label">Sector</span>
      <button class="filter-chip active" onclick="setFilter('sector',null,this)">All</button>
      <button class="filter-chip" onclick="setFilter('sector','Technology',this)">Tech</button>
      <button class="filter-chip" onclick="setFilter('sector','Healthcare',this)">Healthcare</button>
      <button class="filter-chip" onclick="setFilter('sector','Energy',this)">Energy</button>
      <button class="filter-chip" onclick="setFilter('sector','Financial',this)">Financial</button>
      <button class="filter-chip" onclick="setFilter('sector','Consumer',this)">Consumer</button>
      <button class="filter-chip" onclick="setFilter('sector','Industrial',this)">Industrial</button>
      <button class="filter-chip" onclick="setFilter('sector','Infrastructure',this)">Infra</button>
      <div class="filter-divider"></div>
      <span class="filter-label">Sort</span>
      <button class="filter-chip sort-chip active" onclick="setSort('published_at',this)">Latest</button>
      <button class="filter-chip sort-chip" onclick="setSort('deal_score',this)">Top Score</button>
      <button class="filter-chip sort-chip" onclick="setSort('deal_size_m',this)">Largest</button>
    </div>

    <!-- GRID -->
    <div id="deals-grid"></div>

    <!-- PAGINATION -->
    <div class="pagination" id="pagination"></div>
  </div>

  <!-- ALERTS PAGE -->
  <div id="alerts-page">
    <div style="max-width:720px;margin:0 auto">
      <div class="hero" style="text-align:left;padding:40px 0 24px">
        <div class="hero-tag">🔔 Smart Alerts</div>
        <h1 style="font-size:36px">Deal <em>Alerts</em></h1>
        <p style="text-align:left">Get notified instantly when deals matching your criteria hit the wire.</p>
      </div>

      <div class="new-alert-form" id="alert-form-section">
        <h3>+ Create New Alert</h3>
        <div class="form-row" style="margin-bottom:14px">
          <div>
            <label class="modal-label">Alert Name</label>
            <input class="inline-input" id="alert-name" placeholder="e.g. Large PE Tech Buyouts"/>
          </div>
          <div>
            <label class="modal-label">Min Deal Size ($M)</label>
            <input class="inline-input" id="alert-size" type="number" placeholder="e.g. 500"/>
          </div>
        </div>
        <div class="form-row" style="margin-bottom:14px">
          <div>
            <label class="modal-label">Sector</label>
            <select class="inline-input" id="alert-sector">
              <option value="">Any</option>
              <option>Technology</option><option>Healthcare</option>
              <option>Energy</option><option>Financial</option>
              <option>Consumer</option><option>Industrial</option>
              <option>Infrastructure</option>
            </select>
          </div>
          <div>
            <label class="modal-label">Keywords</label>
            <input class="inline-input" id="alert-keywords" placeholder="e.g. cybersecurity, cloud"/>
          </div>
        </div>
        <button class="btn btn-primary" onclick="createAlert()">Create Alert</button>
      </div>

      <div id="alerts-list"></div>
    </div>
  </div>

  <!-- SAVED / BOOKMARKS -->
  <div id="saved-page" style="display:none">
    <div class="hero">
      <div class="hero-tag">♥ Your Pipeline</div>
      <h1>Saved <em>Deals</em></h1>
      <p>Bookmarks sync to your account — add PostgreSQL on Railway for persistence across deploys.</p>
    </div>
    <div id="saved-grid" class="deal-grid-page"></div>
  </div>

</main>
</div>

<!-- DEAL MODAL -->
<div class="modal-overlay" id="deal-modal" onclick="closeModal(event)">
  <div class="modal" id="modal-inner">
    <div class="modal-header">
      <div>
        <div id="modal-badges" class="modal-badges"></div>
        <div id="modal-title" style="font-family:var(--font-head);font-size:22px;line-height:1.3;margin-top:8px"></div>
      </div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="modal-label">Summary</div>
      <div id="modal-summary" class="modal-value"></div>
      <div class="modal-grid">
        <div><div class="modal-label">Acquirer</div><div id="modal-acquirer" class="modal-value" style="margin-bottom:0"></div></div>
        <div><div class="modal-label">Target</div><div id="modal-target" class="modal-value" style="margin-bottom:0"></div></div>
        <div><div class="modal-label">Deal Size</div><div id="modal-size" class="modal-value" style="margin-bottom:0"></div></div>
        <div><div class="modal-label">Deal Score</div><div id="modal-score" class="modal-value" style="margin-bottom:0"></div></div>
        <div><div class="modal-label">Published</div><div id="modal-date" class="modal-value" style="margin-bottom:0"></div></div>
        <div><div class="modal-label">Source</div><div id="modal-source" class="modal-value" style="margin-bottom:0"></div></div>
      </div>
      <div class="modal-actions">
        <a id="modal-link" href="#" target="_blank" class="btn btn-primary">View Source →</a>
        <button class="btn btn-outline" onclick="closeModal()">Close</button>
      </div>
    </div>
  </div>
</div>

<!-- AUTH PANEL -->
<div id="auth-panel">
  <button class="auth-close" onclick="closeAuth()">✕</button>
  <div class="auth-inner">
    <div id="auth-login-view">
      <div class="auth-title">Welcome Back</div>
      <div class="auth-sub">Sign in to your DealFlow account</div>
      <div class="form-field">
        <label>Email</label>
        <input type="email" id="login-email" placeholder="analyst@firm.com" onkeydown="if(event.key==='Enter')doLogin()"/>
        <div class="form-error" id="login-error"></div>
      </div>
      <div class="form-field">
        <label>Password</label>
        <input type="password" id="login-password" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()"/>
      </div>
      <button class="btn btn-primary" style="width:100%;padding:12px" onclick="doLogin()">Sign In</button>
      <div class="auth-switch">No account? <a onclick="switchAuth('register')">Create one →</a></div>
    </div>
    <div id="auth-register-view" style="display:none">
      <div class="auth-title">Get Access</div>
      <div class="auth-sub">Create your DealFlow AI account</div>
      <div class="form-field">
        <label>Full Name</label>
        <input type="text" id="reg-name" placeholder="Alex Morgan"/>
      </div>
      <div class="form-field">
        <label>Email</label>
        <input type="email" id="reg-email" placeholder="analyst@firm.com"/>
        <div class="form-error" id="reg-error"></div>
      </div>
      <div class="form-field">
        <label>Password</label>
        <input type="password" id="reg-password" placeholder="Min. 8 characters" onkeydown="if(event.key==='Enter')doRegister()"/>
      </div>
      <button class="btn btn-primary" style="width:100%;padding:12px" onclick="doRegister()">Create Account</button>
      <div class="auth-switch">Have an account? <a onclick="switchAuth('login')">Sign in →</a></div>
    </div>
  </div>
</div>

<!-- TOAST -->
<div id="toast"></div>

<script>
// ── STATE ────────────────────────────────────────────────────
const API = '/api/v1';
let state = {
  token: localStorage.getItem('df_token'),
  user: JSON.parse(localStorage.getItem('df_user') || 'null'),
  page: 1, pages: 1, total: 0,
  sector: null, sort: 'published_at',
  search: '', searchTimer: null,
  bookmarks: new Set(),
};

// ── INIT ────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  updateAuthUI();
  if (state.token) { refreshBookmarks(); fetchMe(); }
  loadDeals();
  loadTrending();
  const nav = (new URLSearchParams(location.search)).get('nav');
  if (nav === 'saved' && state.token) showPage('saved', document.getElementById('nav-saved'));
  if (nav === 'alerts' && state.token) showPage('alerts', document.getElementById('nav-alerts'));
});

// ── AUTH UI ──────────────────────────────────────────────────
function updateAuthUI() {
  const li = document.getElementById('login-btn');
  const ri = document.getElementById('register-btn');
  const ub = document.getElementById('user-badge');
  const hint = document.getElementById('user-stats-hint');
  if (state.user) {
    li.style.display = 'none'; ri.style.display = 'none';
    ub.style.display = 'flex';
    document.getElementById('user-avatar').textContent = state.user.name[0].toUpperCase();
    document.getElementById('user-name-badge').textContent = state.user.name.split(' ')[0];
    refreshStats();
  } else {
    li.style.display = ''; ri.style.display = '';
    ub.style.display = 'none';
    if (hint) hint.textContent = '';
  }
}

async function refreshStats() {
  const hint = document.getElementById('user-stats-hint');
  if (!state.token || !hint) return;
  try {
    const r = await apiFetch('/auth/stats');
    if (!r.ok) return;
    const s = await r.json();
    hint.textContent = '♥ ' + s.bookmark_count + ' · 🔔 ' + s.alert_count;
  } catch(e) {}
}

async function refreshBookmarks() {
  if (!state.token) return;
  try {
    const r = await apiFetch('/deals/bookmarks');
    if (!r.ok) return;
    const deals = await r.json();
    state.bookmarks = new Set(deals.map(d => d.id));
  } catch(e) {}
}

function openAuth(mode) {
  document.getElementById('auth-panel').classList.add('open');
  switchAuth(mode);
}
function closeAuth() { document.getElementById('auth-panel').classList.remove('open'); }
function switchAuth(mode) {
  document.getElementById('auth-login-view').style.display = mode==='login' ? '' : 'none';
  document.getElementById('auth-register-view').style.display = mode==='register' ? '' : 'none';
}

async function doLogin() {
  const email = document.getElementById('login-email').value;
  const pw    = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.style.display = 'none';
  try {
    const r = await fetch(`${API}/auth/login`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email, password: pw})
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.detail || 'Login failed'; errEl.style.display='block'; return; }
    state.token = d.access_token;
    localStorage.setItem('df_token', d.access_token);
    await fetchMe();
    await refreshBookmarks();
    closeAuth(); toast('Welcome back!', 'success');
    loadDeals();
  } catch(e) { errEl.textContent = 'Connection error'; errEl.style.display='block'; }
}

async function doRegister() {
  const name  = document.getElementById('reg-name').value;
  const email = document.getElementById('reg-email').value;
  const pw    = document.getElementById('reg-password').value;
  const errEl = document.getElementById('reg-error');
  errEl.style.display = 'none';
  if (!name||!email||!pw) { errEl.textContent='All fields required'; errEl.style.display='block'; return; }
  try {
    const r = await fetch(`${API}/auth/register`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, email, password: pw})
    });
    const d = await r.json();
    if (!r.ok) { errEl.textContent = d.detail || 'Registration failed'; errEl.style.display='block'; return; }
    state.token = d.access_token;
    localStorage.setItem('df_token', d.access_token);
    await fetchMe();
    await refreshBookmarks();
    closeAuth(); toast('Account created! Welcome.', 'success');
    loadDeals();
  } catch(e) { errEl.textContent = 'Connection error'; errEl.style.display='block'; }
}

async function fetchMe() {
  const r = await apiFetch('/auth/me');
  if (r.ok) {
    state.user = await r.json();
    localStorage.setItem('df_user', JSON.stringify(state.user));
    updateAuthUI();
  }
}

function logout() {
  state.token = null; state.user = null;
  localStorage.removeItem('df_token');
  localStorage.removeItem('df_user');
  localStorage.removeItem('df_refresh');
  updateAuthUI(); toast('Signed out');
  const nd = document.getElementById('nav-deals');
  document.querySelectorAll('header .nav-pill').forEach(b => b.classList.remove('active'));
  if (nd) nd.classList.add('active');
  document.getElementById('deals-page').style.display = '';
  document.getElementById('alerts-page').style.display = 'none';
  document.getElementById('saved-page').style.display = 'none';
  loadDeals();
}

// ── API HELPER ───────────────────────────────────────────────
function apiFetch(path, opts={}) {
  const headers = {'Content-Type':'application/json', ...(opts.headers||{})};
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  return fetch(API+path, {...opts, headers});
}

// ── DEALS ────────────────────────────────────────────────────
async function loadDeals() {
  const grid = document.getElementById('deals-grid');
  grid.innerHTML = Array(6).fill(0).map(() =>
    `<div class="deal-card" style="min-height:200px"><div class="skeleton" style="height:100%;min-height:180px;border-radius:8px"></div></div>`
  ).join('');

  const params = new URLSearchParams({
    page: state.page, page_size: 12, sort_by: state.sort,
  });
  if (state.sector) params.set('sector', state.sector);
  if (state.search) params.set('search', state.search);

  try {
    const r = await apiFetch(`/deals?${params}`);
    const d = await r.json();
    state.pages = d.pages; state.total = d.total;
    document.getElementById('stat-deals').textContent = d.total;
    renderDeals(d.items);
    renderPagination();
  } catch(e) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">⚡</div><h3>Could not load deals</h3><p>Check your connection</p></div>`;
  }
}

function renderDeals(deals) {
  const grid = document.getElementById('deals-grid');
  if (!deals.length) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">🔍</div><h3>No deals found</h3><p>Try adjusting your filters</p></div>`;
    return;
  }
  grid.innerHTML = deals.map(d => dealCard(d)).join('');
}

function dealCard(d) {
  const size = d.deal_size_m ? `$${d.deal_size_m >= 1000 ? (d.deal_size_m/1000).toFixed(1)+'B' : d.deal_size_m.toFixed(0)+'M'}` : '—';
  const date = new Date(d.published_at).toLocaleDateString('en-US',{month:'short',day:'numeric'});
  const score = d.deal_score || 0;
  const bk = state.bookmarks.has(d.id);
  return `
  <div class="deal-card" onclick="openDeal(${d.id})">
    <div class="card-header">
      <div class="card-badges">
        ${d.sector ? `<span class="badge badge-sector">${d.sector}</span>` : ''}
        ${d.buyer_type ? `<span class="badge badge-buyer">${d.buyer_type}</span>` : ''}
        ${d.cross_border ? `<span class="badge badge-cb">Cross-Border</span>` : ''}
      </div>
      <div class="card-score">
        <div class="score-bar"><div class="score-fill" style="width:${score}%"></div></div>
        ${score.toFixed(0)}
      </div>
    </div>
    <div class="card-title">${escHtml(d.title)}</div>
    ${d.summary ? `<div class="card-summary">${escHtml(d.summary)}</div>` : ''}
    <div class="card-footer">
      <div class="card-meta">
        ${d.acquirer ? `<div class="meta-item"><strong>${escHtml(d.acquirer)}</strong></div>` : ''}
        <div class="meta-item">${date}</div>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        ${d.deal_size_m ? `<span class="deal-size">${size}</span>` : ''}
        <span class="source-tag">${escHtml(d.source_name||'')}</span>
        <button class="bookmark-btn ${bk?'active':''}" 
          onclick="event.stopPropagation();toggleBookmark(${d.id},this)"
          title="Bookmark">${bk?'♥':'♡'}</button>
      </div>
    </div>
  </div>`;
}

let currentDeals = {};
async function openDeal(id) {
  const r = await apiFetch(`/deals/${id}`);
  const d = await r.json();
  currentDeals[id] = d;
  const fmtSize = d.deal_size_m
    ? (d.deal_size_m >= 1000 ? `$${(d.deal_size_m/1000).toFixed(2)}B` : `$${d.deal_size_m.toFixed(0)}M`)
    : 'Undisclosed';

  document.getElementById('modal-badges').innerHTML = [
    d.sector && `<span class="badge badge-sector">${d.sector}</span>`,
    d.buyer_type && `<span class="badge badge-buyer">${d.buyer_type}</span>`,
    d.cross_border && `<span class="badge badge-cb">Cross-Border</span>`,
  ].filter(Boolean).join('');

  document.getElementById('modal-title').textContent = d.title;
  document.getElementById('modal-summary').textContent = d.summary || 'No summary available.';
  document.getElementById('modal-acquirer').textContent = d.acquirer || '—';
  document.getElementById('modal-target').textContent = d.target || '—';
  document.getElementById('modal-size').innerHTML = `<span style="color:var(--accent);font-family:var(--font-mono)">${fmtSize}</span>`;
  document.getElementById('modal-score').innerHTML = `<span style="font-family:var(--font-mono)">${d.deal_score}/100</span>`;
  document.getElementById('modal-date').textContent = new Date(d.published_at).toLocaleDateString('en-US',{year:'numeric',month:'long',day:'numeric'});
  document.getElementById('modal-source').textContent = d.source_name || '—';
  document.getElementById('modal-link').href = d.source_url || '#';

  document.getElementById('deal-modal').classList.add('open');
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('deal-modal')) {
    document.getElementById('deal-modal').classList.remove('open');
  }
}

async function toggleBookmark(id, btn) {
  if (!state.token) { window.location.href='/login'; return; }
  const r = await apiFetch(`/deals/${id}/bookmark`, {method:'POST'});
  const d = await r.json();
  if (d.bookmarked) {
    state.bookmarks.add(id); btn.classList.add('active');
    btn.textContent = '♥'; toast('Bookmarked', 'success');
  } else {
    state.bookmarks.delete(id); btn.classList.remove('active');
    btn.textContent = '♡'; toast('Removed bookmark');
  }
  refreshStats();
  if (document.getElementById('saved-page').style.display==='block') loadSaved();
}

// ── TRENDING ─────────────────────────────────────────────────
async function loadTrending() {
  try {
    const r = await apiFetch('/deals/trending');
    const data = await r.json();
    const maxCount = Math.max(...data.map(d=>d.deal_count), 1);
    document.getElementById('trending-row').innerHTML = data.map(t => `
      <div class="trend-card">
        <div class="trend-sector">${t.sector}</div>
        <div class="trend-stats">
          <div class="trend-stat"><strong>${t.deal_count}</strong>deals</div>
          <div class="trend-stat"><strong>${t.total_value_m >= 1000 ? '$'+(t.total_value_m/1000).toFixed(1)+'B' : '$'+t.total_value_m.toFixed(0)+'M'}</strong>value</div>
          <div class="trend-stat"><strong>${t.avg_score}</strong>avg score</div>
        </div>
        <div class="trend-bar-wrap"><div class="trend-bar" style="width:${Math.round(t.deal_count/maxCount*100)}%"></div></div>
      </div>
    `).join('');
  } catch(e) {}
}

// ── FILTERS ──────────────────────────────────────────────────
function setFilter(type, value, btn) {
  if (type === 'sector') {
    state.sector = value; state.page = 1;
    document.querySelectorAll('.filter-chip:not(.sort-chip)').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  loadDeals();
}
function setSort(sort, btn) {
  state.sort = sort; state.page = 1;
  document.querySelectorAll('.sort-chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadDeals();
}
function onSearch(val) {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    state.search = val; state.page = 1; loadDeals();
  }, 350);
}

// ── PAGINATION ────────────────────────────────────────────────
function renderPagination() {
  const pg = document.getElementById('pagination');
  if (state.pages <= 1) { pg.innerHTML = ''; return; }
  let html = '';
  html += `<button class="page-btn" onclick="goPage(${state.page-1})" ${state.page===1?'disabled':''}>‹</button>`;
  for (let i=1; i<=state.pages; i++) {
    if (i===1||i===state.pages||Math.abs(i-state.page)<=1)
      html += `<button class="page-btn ${i===state.page?'active':''}" onclick="goPage(${i})">${i}</button>`;
    else if (Math.abs(i-state.page)===2)
      html += `<span class="page-info">…</span>`;
  }
  html += `<button class="page-btn" onclick="goPage(${state.page+1})" ${state.page===state.pages?'disabled':''}>›</button>`;
  pg.innerHTML = html;
}
function goPage(p) { state.page = p; loadDeals(); window.scrollTo({top:0,behavior:'smooth'}); }

// ── PAGES ────────────────────────────────────────────────────
function showPage(page, btn) {
  document.getElementById('deals-page').style.display = page==='deals' ? '' : 'none';
  document.getElementById('alerts-page').style.display = page==='alerts' ? 'block' : 'none';
  document.getElementById('saved-page').style.display = page==='saved' ? 'block' : 'none';
  if (btn) {
    document.querySelectorAll('header .nav-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  if (page==='alerts') {
    if (!state.token) { window.location.href='/login'; return; }
    loadAlerts();
  }
  if (page==='saved') {
    if (!state.token) { window.location.href='/login'; return; }
    loadSaved();
  }
}

async function loadSaved() {
  const grid = document.getElementById('saved-grid');
  grid.innerHTML = Array(4).fill(0).map(() =>
    `<div class="deal-card" style="min-height:160px"><div class="skeleton" style="height:100%;min-height:140px;border-radius:8px"></div></div>`
  ).join('');
  try {
    const r = await apiFetch('/deals/bookmarks');
    if (!r.ok) throw new Error('auth');
    const deals = await r.json();
    state.bookmarks = new Set(deals.map(d => d.id));
    if (!deals.length) {
      grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">♥</div><h3>No saved deals yet</h3><p>Open a deal card and tap the heart to save it to your pipeline.</p></div>`;
      return;
    }
    grid.innerHTML = deals.map(d => dealCard(d)).join('');
  } catch(e) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><h3>Could not load saved deals</h3><p>Try signing in again.</p></div>`;
  }
}

// ── ALERTS ───────────────────────────────────────────────────
async function loadAlerts() {
  const list = document.getElementById('alerts-list');
  list.innerHTML = '<div style="color:var(--muted);text-align:center;padding:40px">Loading…</div>';
  try {
    const r = await apiFetch('/alerts');
    const data = await r.json();
    if (!data.length) {
      list.innerHTML = '<div class="empty-state"><div class="empty-icon">🔔</div><h3>No alerts yet</h3><p>Create your first deal alert above</p></div>';
      return;
    }
    list.innerHTML = data.map(a => {
      let sec = '';
      try { if (a.sectors) sec = JSON.parse(a.sectors).join(', '); } catch(x) {}
      let bt = '';
      try { if (a.buyer_types) bt = JSON.parse(a.buyer_types).join(', '); } catch(x) {}
      return `
      <div class="alert-card">
        <div>
          <div class="alert-name">${escHtml(a.name)}</div>
          <div class="alert-details">
            ${sec ? 'Sectors: '+escHtml(sec)+' · ' : ''}
            ${bt ? 'Buyers: '+escHtml(bt)+' · ' : ''}
            ${a.min_size_m ? 'Min $'+a.min_size_m+'M · ' : ''}
            ${a.keywords ? 'Keywords: '+escHtml(a.keywords) : ''}
            Created ${new Date(a.created_at).toLocaleDateString()}
          </div>
        </div>
        <div class="alert-actions">
          <button class="btn-danger" onclick="deleteAlert(${a.id})">Delete</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    list.innerHTML = '<div style="color:var(--red);text-align:center;padding:40px">Could not load alerts</div>';
  }
}

async function createAlert() {
  if (!state.token) { window.location.href='/login'; return; }
  const name = document.getElementById('alert-name').value;
  const size = document.getElementById('alert-size').value;
  const sector = document.getElementById('alert-sector').value;
  const kw = document.getElementById('alert-keywords').value;
  if (!name) { toast('Alert name is required', 'error'); return; }
  const body = { name, min_size_m: size ? parseFloat(size) : null,
    sectors: sector ? [sector] : null, keywords: kw || null };
  const r = await apiFetch('/alerts', {method:'POST', body: JSON.stringify(body)});
  if (r.ok) { toast('Alert created!', 'success'); loadAlerts(); refreshStats(); }
  else toast('Failed to create alert', 'error');
}

async function deleteAlert(id) {
  const r = await apiFetch(`/alerts/${id}`, {method:'DELETE'});
  if (r.ok) { toast('Alert deleted'); loadAlerts(); refreshStats(); }
}

// ── UTILS ─────────────────────────────────────────────────────
function escHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (type ? ' '+type : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = '', 2600);
}

// close auth on outside click
document.addEventListener('click', e => {
  const panel = document.getElementById('auth-panel');
  if (panel.classList.contains('open') && !panel.contains(e.target) &&
      !e.target.closest('.nav-pill')) {
    closeAuth();
  }
});

// keyboard shortcut
document.addEventListener('keydown', e => {
  if (e.key==='Escape') { closeModal(); closeAuth(); }
  if (e.key==='/' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
    e.preventDefault(); document.getElementById('search-input').focus();
  }
});
</script>
</body>
</html>"""


def standalone_auth_page(mode: str) -> str:
    """Dedicated full-page login/register (same API + localStorage keys as main app)."""
    is_login = mode == "login"
    endpoint = "/auth/login" if is_login else "/auth/register"
    flip_href = "/register" if is_login else "/login"
    flip_label = "Need an account? Register →" if is_login else "← Already have an account?"
    h1 = "Welcome back" if is_login else "Create your account"
    sub = (
        "Sign in to sync bookmarks, alerts, and saved deals with your account."
        if is_login
        else "Password must be at least 8 characters. On Railway, add PostgreSQL so accounts survive redeploys."
    )
    btn = "Sign in" if is_login else "Create account"
    if is_login:
        fields = """
      <div class="form-field"><label>Email</label>
        <input type="email" id="email" required autocomplete="username" placeholder="you@company.com"/></div>
      <div class="form-field"><label>Password</label>
        <input type="password" id="password" required autocomplete="current-password"/></div>"""
        body_js = "const body = { email, password };"
    else:
        fields = """
      <div class="form-field"><label>Full name</label>
        <input type="text" id="name" required autocomplete="name" placeholder="Alex Morgan"/></div>
      <div class="form-field"><label>Email</label>
        <input type="email" id="email" required autocomplete="username"/></div>
      <div class="form-field"><label>Password</label>
        <input type="password" id="password" required autocomplete="new-password" placeholder="8+ characters"/></div>"""
        body_js = """const name = document.getElementById('name').value.trim();
  if (!name) { err.textContent = 'Name required'; err.style.display = 'block'; return; }
  const body = { email, name, password };"""

    script = """async function submitAuth(e) {
  e.preventDefault();
  const err = document.getElementById('err');
  err.style.display = 'none';
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  __BODY__
  try {
    const r = await fetch(API + '__EP__', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) {
      let msg = 'Request failed';
      if (typeof d.detail === 'string') msg = d.detail;
      else if (Array.isArray(d.detail)) msg = d.detail.map(x => x.msg || x.message || JSON.stringify(x)).join('; ');
      err.textContent = msg;
      err.style.display = 'block';
      return;
    }
    localStorage.setItem('df_token', d.access_token);
    localStorage.setItem('df_refresh', d.refresh_token || '');
    const mr = await fetch(API + '/auth/me', { headers: { 'Authorization': 'Bearer ' + d.access_token } });
    if (mr.ok) localStorage.setItem('df_user', JSON.stringify(await mr.json()));
    const next = new URLSearchParams(location.search).get('next') || '/';
    window.location.href = next;
  } catch (x) {
    err.textContent = 'Network error';
    err.style.display = 'block';
  }
}
document.getElementById('f').addEventListener('submit', submitAuth);
""".replace(
        "__BODY__", body_js
    ).replace(
        "__EP__", endpoint
    )

    page_title = "Sign in" if is_login else "Register"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{page_title} — DealFlow AI</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Manrope:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
:root{{--bg:#0a0c0f;--surface:#111418;--border:#2a3040;--text:#e8eaf0;--muted:#6b7280;--accent:#00d4aa;--red:#ef4444}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;background:var(--bg);color:var(--text);font-family:'Manrope',sans-serif;display:flex;flex-direction:column}}
.top{{display:flex;justify-content:space-between;align-items:center;padding:20px 28px;border-bottom:1px solid var(--border)}}
.brand{{font-family:'DM Serif Display',serif;font-size:22px;color:var(--text);text-decoration:none;display:flex;align-items:center;gap:10px}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}}
.top a.link{{color:var(--muted);text-decoration:none;font-size:14px}}
.top a.link:hover{{color:var(--accent)}}
.wrap{{flex:1;display:flex;align-items:center;justify-content:center;padding:32px 20px}}
.card{{width:100%;max-width:420px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:36px 32px}}
h1{{font-family:'DM Serif Display',serif;font-size:32px;margin-bottom:8px}}
.sub{{color:var(--muted);font-size:14px;margin-bottom:28px;line-height:1.6}}
.form-field{{margin-bottom:18px}}
label{{display:block;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}}
input{{width:100%;padding:12px 14px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:15px;outline:none}}
input:focus{{border-color:var(--accent)}}
.btn-primary{{width:100%;margin-top:8px;padding:14px;border:none;border-radius:8px;background:var(--accent);color:#000;font-weight:700;font-size:15px;cursor:pointer}}
.btn-primary:hover{{filter:brightness(1.06)}}
.err{{display:none;color:var(--red);font-size:13px;margin-top:12px}}
.flip{{text-align:center;margin-top:22px;font-size:14px;color:var(--muted)}}
.flip a{{color:var(--accent);text-decoration:none}}
footer{{text-align:center;padding:20px;color:var(--muted);font-size:12px;max-width:520px;margin:0 auto;line-height:1.5}}
</style></head>
<body>
<header class="top">
  <a class="brand" href="/"><span class="dot"></span> DealFlow <em style="color:var(--accent);font-style:italic">AI</em></a>
  <a class="link" href="/">← Back to deals</a>
</header>
<div class="wrap">
  <div class="card">
    <h1>{h1}</h1>
    <p class="sub">{sub}</p>
    <form id="f">
      {fields}
      <button type="submit" class="btn-primary">{btn}</button>
      <div class="err" id="err"></div>
    </form>
    <p class="flip"><a href="{flip_href}">{flip_label}</a></p>
  </div>
</div>
<footer>User data lives in the app database. For production on Railway, attach the PostgreSQL plugin and use the injected <code style="color:var(--accent)">DATABASE_URL</code>.</footer>
<script>
const API = '/api/v1';
{script}
</script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def auth_login_page():
    return HTMLResponse(content=standalone_auth_page("login"))


@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
def auth_register_page():
    return HTMLResponse(content=standalone_auth_page("register"))


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return HTMLResponse(content=FRONTEND_HTML)

# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dealflow_ai:app", host="0.0.0.0", port=APP_PORT, reload=False)
