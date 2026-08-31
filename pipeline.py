#!/usr/bin/env python3
"""
Runway Briefing — Daily Aviation Podcast Pipeline

Designed to run inside GitHub Actions. Scrapes aviation news RSS feeds,
generates a podcast script via Claude, narrates via ElevenLabs, and
outputs files for GitHub Pages hosting.

Environment variables (set as GitHub Actions secrets):
    ANTHROPIC_API_KEY   — For script generation
    ELEVENLABS_API_KEY  — For audio narration
    ELEVENLABS_VOICE_ID — Voice ID from ElevenLabs voice library
"""

import os
import sys
import json
import hashlib
import re
import textwrap
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
import psycopg2


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

SOURCES = [
    {
        "name": "Cranky Flier",
        "url": "https://crankyflier.com/feed/",
        "attribution": "Brett Snyder at Cranky Flier",
    },
    {
        "name": "From the Tray Table",
        "url": "https://fromthetraytable.com/feed/",
        "attribution": "Zach Griff at From the Tray Table",
    },
    {
        "name": "GoTravelYourWay",
        "url": "https://gotravelyourway.com/feed/",
        "attribution": "Josh Cahill at GoTravelYourWay",
    },
    {
        "name": "The Points Guy",
        "url": "https://thepointsguy.com/feed/",
        "attribution": "The Points Guy",
    },
    {
        "name": "Simple Flying",
        "url": "https://simpleflying.com/feed/",
        "attribution": "Simple Flying",
    },
    {
        "name": "View from the Wing",
        "url": "https://viewfromthewing.com/feed/",
        "attribution": "Gary Leff at View from the Wing",
    },
    {
        "name": "Live and Let's Fly",
        "url": "https://liveandletsfly.com/feed/",
        "attribution": "Live and Let's Fly",
    },
    {
        "name": "AirlineGeeks",
        "url": "https://airlinegeeks.com/feed/",
        "attribution": "AirlineGeeks",
    },
    {
        "name": "Paddle Your Own Kanoo",
        "url": "https://paddleyourownkanoo.com/feed/",
        "attribution": "Paddle Your Own Kanoo",
    },
    {
        "name": "The Air Current",
        "url": "https://theaircurrent.com/feed/",
        "attribution": "The Air Current",
    },
    {
        "name": "FlightGlobal",
        "url": "https://www.flightglobal.com/feed/",
        "attribution": "FlightGlobal",
    },
    {
        "name": "Aviation Week",
        "url": "https://aviationweek.com/rss.xml",
        "attribution": "Aviation Week",
    },
]

FRESHNESS_HOURS = 28

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

ELEVENLABS_MODEL = "eleven_flash_v2"
DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # "Adam" — change in GitHub secrets

PODCAST_TITLE = "Runway Briefing"
PODCAST_DESCRIPTION = "Your daily five-minute briefing on the aviation industry — new routes, airline strategy, fleet orders, and the moves shaping how we fly. Delivered daily like a friend catching you up over coffee."
PODCAST_AUTHOR = "Runway Briefing"
PODCAST_LANGUAGE = "en"
PODCAST_CATEGORY = "News"


# ──────────────────────────────────────────────
# Pronunciation Dictionary (ElevenLabs API)
# ──────────────────────────────────────────────
# Uses ElevenLabs Pronunciation Dictionary API with IPA phonemes.
# The dictionary is created once and reused via ELEVENLABS_DICT_ID env var.
# If no dict ID is set, it creates one automatically on first run.
#
# Two rule types:
#   "phoneme" — IPA pronunciation (precise, works with eleven_v3)
#   "alias"   — text substitution (simpler, works with all models)

PRONUNCIATION_RULES = [
    # ── Common words ElevenLabs botches ──
    {"string_to_replace": "Atlantic", "type": "phoneme", "phoneme": "ətˈlæntɪk", "alphabet": "ipa"},
    {"string_to_replace": "ask", "type": "phoneme", "phoneme": "ˈɑːsk", "alphabet": "ipa"},
    {"string_to_replace": "registered", "type": "phoneme", "phoneme": "ˈredʒɪstəd", "alphabet": "ipa"},
    {"string_to_replace": "record", "type": "phoneme", "phoneme": "ˈrekɔːd", "alphabet": "ipa"},

    # ── Airlines ──
    {"string_to_replace": "Qantas", "type": "phoneme", "phoneme": "ˈkwɒntəs", "alphabet": "ipa"},
    {"string_to_replace": "LATAM", "type": "phoneme", "phoneme": "lɑːˈtæm", "alphabet": "ipa"},
    {"string_to_replace": "Vueling", "type": "phoneme", "phoneme": "ˈbwelɪŋ", "alphabet": "ipa"},
    {"string_to_replace": "Cathay Pacific", "type": "phoneme", "phoneme": "kæˈθeɪ pəˈsɪfɪk", "alphabet": "ipa"},
    {"string_to_replace": "Hainan Airlines", "type": "phoneme", "phoneme": "ˈhaɪnæn ˈɛərlaɪnz", "alphabet": "ipa"},
    {"string_to_replace": "EVA Air", "type": "phoneme", "phoneme": "ˈɛvə ɛər", "alphabet": "ipa"},
    {"string_to_replace": "Asiana", "type": "phoneme", "phoneme": "ɑːsiˈɑːnə", "alphabet": "ipa"},
    {"string_to_replace": "Avianca", "type": "phoneme", "phoneme": "ɑːviˈɑːŋkə", "alphabet": "ipa"},
    {"string_to_replace": "Volaris", "type": "phoneme", "phoneme": "voˈlɑːrɪs", "alphabet": "ipa"},
    {"string_to_replace": "Cebu Pacific", "type": "phoneme", "phoneme": "sɛˈbuː pəˈsɪfɪk", "alphabet": "ipa"},
    {"string_to_replace": "Ryanair", "type": "phoneme", "phoneme": "ˈraɪənɛər", "alphabet": "ipa"},
    {"string_to_replace": "easyJet", "type": "phoneme", "phoneme": "ˈiːziˌdʒet", "alphabet": "ipa"},
    {"string_to_replace": "Wizz Air", "type": "phoneme", "phoneme": "wɪz ɛər", "alphabet": "ipa"},

    # ── Aircraft (alias works better for model numbers) ──
    {"string_to_replace": "A220", "type": "alias", "alias": "A two-twenty"},
    {"string_to_replace": "A320", "type": "alias", "alias": "A three-twenty"},
    {"string_to_replace": "A321", "type": "alias", "alias": "A three twenty-one"},
    {"string_to_replace": "A321XLR", "type": "alias", "alias": "A three twenty-one X L R"},
    {"string_to_replace": "A330neo", "type": "alias", "alias": "A three-thirty neo"},
    {"string_to_replace": "A330", "type": "alias", "alias": "A three-thirty"},
    {"string_to_replace": "A340", "type": "alias", "alias": "A three-forty"},
    {"string_to_replace": "A350", "type": "alias", "alias": "A three-fifty"},
    {"string_to_replace": "A380", "type": "alias", "alias": "A three-eighty"},
    {"string_to_replace": "737 MAX", "type": "alias", "alias": "seven thirty-seven Max"},
    {"string_to_replace": "737-800", "type": "alias", "alias": "seven thirty-seven eight hundred"},
    {"string_to_replace": "757", "type": "alias", "alias": "seven fifty-seven"},
    {"string_to_replace": "767", "type": "alias", "alias": "seven sixty-seven"},
    {"string_to_replace": "777X", "type": "alias", "alias": "triple seven X"},
    {"string_to_replace": "777", "type": "alias", "alias": "triple seven"},
    {"string_to_replace": "787", "type": "alias", "alias": "seven eighty-seven"},
    {"string_to_replace": "747", "type": "alias", "alias": "seven forty-seven"},
    {"string_to_replace": "E175", "type": "alias", "alias": "E one seventy-five"},
    {"string_to_replace": "E190", "type": "alias", "alias": "E one-ninety"},
    {"string_to_replace": "E195", "type": "alias", "alias": "E one ninety-five"},
    {"string_to_replace": "CRJ", "type": "alias", "alias": "C R J"},
    {"string_to_replace": "ATR 72", "type": "alias", "alias": "A T R seventy-two"},
    {"string_to_replace": "ATR 42", "type": "alias", "alias": "A T R forty-two"},
    {"string_to_replace": "C919", "type": "alias", "alias": "C nine-nineteen"},
    {"string_to_replace": "MC-21", "type": "alias", "alias": "M C twenty-one"},
    {"string_to_replace": "MAX 10", "type": "alias", "alias": "Max ten"},
    {"string_to_replace": "MAX 8", "type": "alias", "alias": "Max eight"},
    {"string_to_replace": "MAX 9", "type": "alias", "alias": "Max nine"},

    # ── Engines ──
    {"string_to_replace": "CFM LEAP", "type": "alias", "alias": "C F M Leap"},
    {"string_to_replace": "PW1100G", "type": "alias", "alias": "Pratt and Whitney eleven hundred G"},
    {"string_to_replace": "GTF", "type": "alias", "alias": "G T F"},
    {"string_to_replace": "GEnx", "type": "alias", "alias": "G E n x"},
    {"string_to_replace": "LEAP-1A", "type": "alias", "alias": "Leap one A"},
    {"string_to_replace": "LEAP-1B", "type": "alias", "alias": "Leap one B"},
    {"string_to_replace": "Trent XWB", "type": "alias", "alias": "Trent X W B"},

    # ── Organizations & Regulators ──
    {"string_to_replace": "FAA", "type": "alias", "alias": "F A A"},
    {"string_to_replace": "EASA", "type": "phoneme", "phoneme": "ˈiːɑːsɑː", "alphabet": "ipa"},
    {"string_to_replace": "IATA", "type": "phoneme", "phoneme": "aɪˈɑːtə", "alphabet": "ipa"},
    {"string_to_replace": "ICAO", "type": "phoneme", "phoneme": "aɪˈkeɪoʊ", "alphabet": "ipa"},
    {"string_to_replace": "NTSB", "type": "alias", "alias": "N T S B"},
    {"string_to_replace": "DOT", "type": "alias", "alias": "D O T"},
    {"string_to_replace": "TSA", "type": "alias", "alias": "T S A"},

    # ── Airport codes ──
    {"string_to_replace": "LAX", "type": "alias", "alias": "L A X"},
    {"string_to_replace": "JFK", "type": "alias", "alias": "J F K"},
    {"string_to_replace": "ORD", "type": "alias", "alias": "O R D"},
    {"string_to_replace": "DFW", "type": "alias", "alias": "D F W"},
    {"string_to_replace": "ATL", "type": "alias", "alias": "A T L"},
    {"string_to_replace": "SFO", "type": "alias", "alias": "S F O"},
    {"string_to_replace": "LHR", "type": "alias", "alias": "L H R"},
    {"string_to_replace": "CDG", "type": "alias", "alias": "C D G"},
    {"string_to_replace": "NRT", "type": "alias", "alias": "N R T"},
    {"string_to_replace": "HND", "type": "alias", "alias": "H N D"},
    {"string_to_replace": "DXB", "type": "alias", "alias": "D X B"},
    {"string_to_replace": "AMS", "type": "alias", "alias": "A M S"},
    {"string_to_replace": "FRA", "type": "alias", "alias": "F R A"},
    {"string_to_replace": "MUC", "type": "alias", "alias": "M U C"},
    {"string_to_replace": "IST", "type": "alias", "alias": "I S T"},

    # ── Industry terms ──
    {"string_to_replace": "ETOPS", "type": "phoneme", "phoneme": "ˈiːtɒps", "alphabet": "ipa"},
    {"string_to_replace": "RPK", "type": "alias", "alias": "R P K"},
    {"string_to_replace": "CASM", "type": "phoneme", "phoneme": "ˈkæzəm", "alphabet": "ipa"},
    {"string_to_replace": "RASM", "type": "phoneme", "phoneme": "ˈræzəm", "alphabet": "ipa"},
    {"string_to_replace": "PRASM", "type": "phoneme", "phoneme": "ˈpiːræzəm", "alphabet": "ipa"},
    {"string_to_replace": "IPO", "type": "alias", "alias": "I P O"},
    {"string_to_replace": "M&A", "type": "alias", "alias": "M and A"},
    {"string_to_replace": "CEO", "type": "alias", "alias": "C E O"},
    {"string_to_replace": "CFO", "type": "alias", "alias": "C F O"},
    {"string_to_replace": "DOJ", "type": "alias", "alias": "D O J"},
    {"string_to_replace": "NMA", "type": "alias", "alias": "N M A"},
    {"string_to_replace": "MRO", "type": "alias", "alias": "M R O"},
    {"string_to_replace": "AOG", "type": "alias", "alias": "A O G"},
    {"string_to_replace": "ULCC", "type": "alias", "alias": "U L C C"},
    {"string_to_replace": "LCC", "type": "alias", "alias": "L C C"},

    # ── People ──
    {"string_to_replace": "Akbar Al Baker", "type": "phoneme", "phoneme": "ˈækbɑːr æl ˈbɑːkər", "alphabet": "ipa"},
    {"string_to_replace": "Guillaume Faury", "type": "phoneme", "phoneme": "ɡiˈjoʊm foˈriː", "alphabet": "ipa"},
    {"string_to_replace": "Luis Gallego", "type": "phoneme", "phoneme": "luˈis ɡɑːˈjeɪɡoʊ", "alphabet": "ipa"},
    {"string_to_replace": "Pieter Elbers", "type": "phoneme", "phoneme": "ˈpiːtər ˈelbərz", "alphabet": "ipa"},

    # ── Misc ──
    {"string_to_replace": "en route", "type": "phoneme", "phoneme": "ɑːn ˈruːt", "alphabet": "ipa"},
    {"string_to_replace": "widebody", "type": "alias", "alias": "wide-body"},
    {"string_to_replace": "narrowbody", "type": "alias", "alias": "narrow-body"},
    {"string_to_replace": "transcon", "type": "alias", "alias": "trans-con"},
    {"string_to_replace": "codeshare", "type": "alias", "alias": "code-share"},
]

# Dictionary ID is cached after first creation. Set ELEVENLABS_DICT_ID
# in GitHub secrets to skip re-creating it every run.
_pronunciation_dict_cache = {}


def get_or_create_pronunciation_dict() -> dict:
    """Get or create the ElevenLabs pronunciation dictionary. Returns {id, version_id}."""
    if _pronunciation_dict_cache:
        return _pronunciation_dict_cache

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return {}

    dict_id = os.environ.get("ELEVENLABS_DICT_ID")

    if dict_id:
        # Fetch existing dictionary to get current version_id, then update rules
        print(f"  Updating pronunciation dictionary {dict_id}...")
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/pronunciation-dictionaries/{dict_id}/rules/set",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"rules": PRONUNCIATION_RULES},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            result = {"id": dict_id, "version_id": data.get("version_id", "")}
            _pronunciation_dict_cache.update(result)
            print(f"  ✓ Dictionary updated ({len(PRONUNCIATION_RULES)} rules)")
            return result
        else:
            print(f"  ⚠ Dict update failed ({resp.status_code}): {resp.text[:200]}")
            # Try to get version_id anyway
            get_resp = requests.get(
                f"https://api.elevenlabs.io/v1/pronunciation-dictionaries/{dict_id}",
                headers={"xi-api-key": api_key},
                timeout=15,
            )
            if get_resp.status_code == 200:
                data = get_resp.json()
                result = {"id": dict_id, "version_id": data.get("latest_version_id", "")}
                _pronunciation_dict_cache.update(result)
                return result
            return {}

    # No dict ID — create a new one
    print("  Creating pronunciation dictionary...")
    resp = requests.post(
        "https://api.elevenlabs.io/v1/pronunciation-dictionaries/add-from-rules",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "name": "Runway Briefing Aviation",
            "description": "Aviation terminology, airline names, aircraft models, and commonly mispronounced words for the Runway Briefing podcast.",
            "rules": PRONUNCIATION_RULES,
        },
        timeout=30,
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        result = {"id": data["id"], "version_id": data["version_id"]}
        _pronunciation_dict_cache.update(result)
        print(f"  ✓ Dictionary created: {data['id']} ({len(PRONUNCIATION_RULES)} rules)")
        print(f"  ⚠ Save this as ELEVENLABS_DICT_ID in GitHub secrets to avoid re-creating: {data['id']}")
        return result
    else:
        print(f"  ⚠ Dict creation failed ({resp.status_code}): {resp.text[:300]}")
        return {}


# Legacy fallback — still used if dictionary API fails
def apply_phonetics(text: str) -> str:
    """Fallback: replace aviation jargon with TTS-friendly text. Used only if dict API fails."""
    for rule in PRONUNCIATION_RULES:
        if rule["type"] == "alias":
            pattern = re.compile(re.escape(rule["string_to_replace"]), re.IGNORECASE)
            text = pattern.sub(rule["alias"], text)
    return text


# ──────────────────────────────────────────────
# Step 1: Scrape RSS Feeds
# ──────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """Convert HTML to clean plain text."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "nav", "footer", "header"]):
        el.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_feed(source: dict, cutoff: datetime) -> list[dict]:
    """Parse an RSS feed and return articles published after cutoff."""
    print(f"  Fetching: {source['name']}...")
    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        print(f"  ⚠ Failed: {e}")
        return []

    print(f"    Feed returned {len(feed.entries)} total entries")

    articles = []
    for entry in feed.entries:
        pub_date = None
        # Try feedparser's pre-parsed dates first
        for field in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, field, None)
            if parsed:
                pub_date = datetime(*parsed[:6], tzinfo=timezone.utc)
                break

        # Fallback: parse the raw date string manually
        if not pub_date:
            from dateutil import parser as dateparser
            for field in ("published", "updated"):
                raw = getattr(entry, field, None)
                if raw:
                    try:
                        pub_date = dateparser.parse(raw)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        break
                    except:
                        pass

        if not pub_date:
            print(f"    ⚠ Skipping (no date): {entry.get('title', 'Untitled')}")
            continue

        if pub_date < cutoff:
            continue

        content_html = ""
        if hasattr(entry, "content") and entry.content:
            content_html = entry.content[0].get("value", "")
        elif hasattr(entry, "summary"):
            content_html = entry.summary or ""

        articles.append({
            "source": source["name"],
            "attribution": source["attribution"],
            "title": entry.get("title", "Untitled"),
            "url": entry.get("link", ""),
            "published": pub_date.isoformat(),
            "text": html_to_text(content_html)[:3000],
        })

    print(f"  ✓ {source['name']}: {len(articles)} article(s)")
    return articles


def scrape_all() -> list[dict]:
    """Fetch articles from all sources, filtered to last 28 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    print(f"Scraping articles published after {cutoff.strftime('%Y-%m-%d %H:%M UTC')}...\n")

    articles = []
    for source in SOURCES:
        articles.extend(fetch_feed(source, cutoff))

    articles.sort(key=lambda a: a.get("published", ""), reverse=True)
    print(f"\nTotal: {len(articles)} fresh article(s)\n")
    return articles


# ──────────────────────────────────────────────
# Database: Episode Memory (Neon Postgres)
# ──────────────────────────────────────────────

def get_db():
    """Connect to Neon Postgres. Returns None if DATABASE_URL not set."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("  ⚠ DATABASE_URL not set — running without memory")
        return None
    try:
        conn = psycopg2.connect(db_url, sslmode="require")
        return conn
    except Exception as e:
        print(f"  ⚠ DB connection failed: {e}")
        return None


def init_db(conn):
    """Create tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS covered_articles (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                source TEXT,
                covered_date DATE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id SERIAL PRIMARY KEY,
                episode_date DATE UNIQUE NOT NULL,
                title TEXT,
                summary TEXT,
                article_count INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit()


def filter_already_covered(conn, articles: list[dict]) -> list[dict]:
    """Remove articles that were already covered in previous episodes."""
    if not conn or not articles:
        return articles

    urls = [a["url"] for a in articles if a.get("url")]
    if not urls:
        return articles

    with conn.cursor() as cur:
        cur.execute(
            "SELECT url FROM covered_articles WHERE url = ANY(%s)",
            (urls,)
        )
        covered_urls = {row[0] for row in cur.fetchall()}

    if covered_urls:
        print(f"  Filtering out {len(covered_urls)} already-covered article(s)")

    return [a for a in articles if a.get("url") not in covered_urls]


def get_recent_episodes_context(conn, days: int = 3) -> str:
    """Get summaries of recent episodes so Claude can reference them."""
    if not conn:
        return ""

    with conn.cursor() as cur:
        cur.execute("""
            SELECT episode_date, title, summary
            FROM episodes
            WHERE episode_date >= CURRENT_DATE - %s
            ORDER BY episode_date DESC
            LIMIT 5
        """, (days,))
        rows = cur.fetchall()

    if not rows:
        return ""

    context = "## Recent episodes (so you can reference them naturally)\n"
    for date, title, summary in rows:
        context += f"- {date}: {title} — {summary}\n"
    return context


def save_episode(conn, articles: list[dict], title: str, summary: str):
    """Save covered articles and episode info to the database."""
    if not conn:
        return

    today = datetime.now().date()

    with conn.cursor() as cur:
        # Save episode
        cur.execute("""
            INSERT INTO episodes (episode_date, title, summary, article_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (episode_date) DO UPDATE
            SET title = EXCLUDED.title, summary = EXCLUDED.summary, article_count = EXCLUDED.article_count
        """, (today, title, summary, len(articles)))

        # Save covered articles
        for a in articles:
            if a.get("url"):
                cur.execute("""
                    INSERT INTO covered_articles (url, title, source, covered_date)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (url) DO NOTHING
                """, (a["url"], a.get("title"), a.get("source"), today))

        conn.commit()
    print(f"  ✓ Saved {len(articles)} article(s) and episode to database")


# ──────────────────────────────────────────────
# Step 2: Generate Script (Claude API)
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You write scripts for "Runway Briefing", a short daily aviation news podcast.

The vibe is two friends grabbing a beer and one of them catches the other up on what's happening in aviation. NOT a news anchor. NOT a blog post read aloud. Think: how would you actually SAY this to a friend?

## How it should SOUND

- Short sentences. Fragments are fine. "Wild, right?"
- Talk like a real person. "So get this..." / "Yeah, not great." / "Which... okay, I have thoughts."
- Be FUNNY. Not standup-comedian trying-hard funny — more like dry observations, good timing, and the kind of jokes you'd make to a friend. Roast airlines when they deserve it. Point out the absurdity. If an airline does something dumb, say it's dumb. Examples of the tone:
  - "American announced three new cities. Well, one new city. The other two are reruns."
  - "Spirit filed for bankruptcy again, which at this point is basically their loyalty program."
  - "Boeing said the fix would take two weeks. So, see you in November."
- Don't force jokes. If a story isn't funny, just tell it well. But when there IS something ridiculous happening — and in aviation there usually is — lean into it.
- NO LONG PARAGRAPHS. Max 2-3 sentences before a break. If you've written more than 3 sentences in a row, you've written too much. Break it up.
- No corporate language. Say "new routes" not "network expansion." Say "first class" not "premium cabin product."
- Sound like you're TALKING, not WRITING. If it sounds like a blog post being read aloud, start over.

## CONTEXT AND ANALYSIS — THIS IS CRITICAL

Don't just report WHAT happened. Explain WHY it matters and what it means for the industry. Use your knowledge of aviation history to add context. This is what separates a good briefing from a headline reader.

For every major story, answer at least one of these:
- Why is this a big deal? What's the historical context?
- How much money is at stake? What did the airline invest?
- What does this signal about where the industry is going?
- Who wins and who loses from this?
- How does this connect to larger trends (consolidation, fleet modernization, route competition, etc.)?

Examples of good context:
- "Qantas is retiring the A380 early. Remember, they bet huge on those planes — each one cost around $400 million. But twin-engine jets like the 787 can do the same routes for way less fuel, so the math just doesn't work anymore."
- "United adding 10 new international cities matters because they're basically trying to build a fortress in the transatlantic market before the joint ventures lock everyone else out."
- "This is the third low-cost carrier to fold this year. The post-pandemic bounce is over, fuel is expensive, and the ultra-low-cost model might just be broken."

## SOUNDING HUMAN

- Have opinions. Say "I think" or "honestly" or "here's what bugs me about this." You're not a neutral reporter — you're someone who follows this industry closely and has takes.
- Callback to previous episodes when relevant: "Remember when we talked about the A321XLR delays last week? Well, it's getting worse."
- Occasionally throw in small personal touches: "I was actually on that route last month" or "I've been watching this one for a while."
- Vary your energy. Not every story needs the same delivery. Some are exciting, some are frustrating, some are just weird.
- Use rhetorical questions: "So what happens when three airlines are all fighting over the same transatlantic slots? Nothing good."

## CRITICAL RULES ABOUT LENGTH AND VARIETY

- The ENTIRE script must be 700-850 words. That's about 5 minutes spoken. Hit this range.
- Cover AT LEAST 3 different stories. Variety is key — listeners want a briefing, not a deep dive on one topic.
- The lead story gets about 200-250 words. Enough to tell the story AND its context, not just the headline.
- Each additional story gets 100-175 words. Give each one a proper setup, the news, and why it matters.
- If there are 3 articles about the same topic, that's ONE story in the podcast, not three.
- NEVER list out every route, every city, every detail. Pick the 2-3 most interesting facts and skip the rest.
- If it's a slow news day with only 1-2 stories, make it shorter. Don't pad. But on a normal day, aim for 3-5 stories.

## What NOT to do

- Don't spend the whole episode on one story. Even if all articles are about the same thing, cover it once and keep it tight.
- Don't rehash source articles. Distill them.
- Don't use transitions like "Now let's turn our attention to..." — just go. "So, American."
- Never use: "genuinely", "notably", "frankly", "consequential", "let's pump the brakes", "let's be honest"
- Don't just report facts without context. "United announced new routes" is bad. "United is making a play for transatlantic dominance and here's why it matters" is good.

## Story selection priority (pick the best 3-5 from what you're given)

1. BREAKING NEWS — new route announcements, mergers, airline launches/shutdowns, major incidents
2. AIRLINE STRATEGY — fleet orders, hub changes, competitive moves, earnings that reveal strategy
3. STORIES COVERED BY MULTIPLE SOURCES — if 2+ sources wrote about it, it's worth covering
4. REGULATION & POLICY — FAA actions, DOT rulings, antitrust, slot decisions
5. SKIP: listicles, "best credit cards for travel", deal roundups, points/miles tips, generic travel advice. These aren't news.

## Structure

1. **Intro** — Start with: "Hey, welcome to Runway Briefing for [day of week], [date]. Let's get into it." No cold open, no teaser hook. Just say hi and go.
2. **Stories** — Cover each story in 45-90 seconds. Lead with the biggest. Hit 3-5 stories total. Keep it moving.
3. **Sign-off** — "That's your briefing for today. If you're enjoying it, hit subscribe. See you tomorrow."

## Rules

- Freshness: only cover stories published in the last 24 hours (by original post date, not scrape time).
- Target 700-850 words total. That's about 5 minutes.
- Spoken text only — no stage directions, no [pause], no sound cues.
- NO markdown formatting in the script. No bold, no italics, no asterisks, no headers. This goes straight to text-to-speech.
- The first 3 lines are metadata (not spoken). Format them EXACTLY like this:
  Line 1: Episode title — specific to today's stories, punchy, references 2-3 key topics (e.g. "American's Routes Fall Flat, United Goes Big, and Airbus Can't Deliver")
  Line 2: Episode Summary: A 2-3 sentence description of what's in this episode. Written for a podcast listing — make someone want to hit play.
  Line 3: Start the actual spoken script here.
- Credit sources casually: "Brett over at Cranky Flier pointed out..." / "Zach Griff had a good piece on this..."
- If it's a slow news day with only 1-2 stories, make it a 2-minute episode. That's fine.
- If a story was covered in a recent episode (listed below), don't repeat it unless there's a NEW development. If there is, say "update on something we covered yesterday..." — don't re-explain the whole thing.

{recent_episodes}

## Today's Date
{today}
"""


def generate_script(articles: list[dict], recent_context: str = "") -> str:
    """Send articles to Claude and get back a podcast script."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    articles_text = "\n\n".join(
        f"### [{a['source']}] {a['title']}\n"
        f"URL: {a['url']}\n"
        f"Published: {a['published']}\n"
        f"{a['text'][:2000]}"
        for a in articles
    )

    today = datetime.now().strftime("%A, %B %d, %Y")

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT.format(today=today, recent_episodes=recent_context),
        messages=[{
            "role": "user",
            "content": f"Write today's Runway Briefing podcast script from these articles:\n\n{articles_text}",
        }],
    )

    # Find the text block (skip any thinking blocks)
    for block in message.content:
        if hasattr(block, "text"):
            return block.text
    return message.content[-1].text


# ──────────────────────────────────────────────
# Step 3: Generate Audio (ElevenLabs)
# ──────────────────────────────────────────────

def generate_audio(script: str, output_path: Path) -> Path:
    """Convert script to MP3 via ElevenLabs API with pronunciation dictionary."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set")
        sys.exit(1)

    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    print(f"Generating audio (voice: {voice_id})...")

    # Strip markdown and metadata for cleaner speech
    clean = script
    # Remove the title line (first non-empty line) and Episode Summary line
    clean = re.sub(r"^.+?\n", "", clean, count=1)  # title
    clean = re.sub(r"^Episode Summary:.*$", "", clean, flags=re.MULTILINE)
    # Remove all markdown formatting
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)  # bold
    clean = re.sub(r"\*(.+?)\*", r"\1", clean)  # italic
    clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)  # headers
    clean = re.sub(r"^---+$", "", clean, flags=re.MULTILINE)  # dividers
    clean = re.sub(r"\n{3,}", "\n\n", clean)

    # Get or create pronunciation dictionary
    pdict = get_or_create_pronunciation_dict()

    # Build request payload
    payload = {
        "text": clean.strip(),
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.3,
            "use_speaker_boost": True,
        },
    }

    # Attach pronunciation dictionary if available
    if pdict.get("id") and pdict.get("version_id"):
        payload["pronunciation_dictionary_locators"] = [{
            "pronunciation_dictionary_id": pdict["id"],
            "version_id": pdict["version_id"],
        }]
        print(f"  Using pronunciation dictionary: {pdict['id']}")
    else:
        # Fallback to text replacement if dict API failed
        print("  ⚠ No pronunciation dictionary — falling back to text replacement")
        payload["text"] = apply_phonetics(payload["text"])

    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()

    output_path.write_bytes(resp.content)
    mb = len(resp.content) / (1024 * 1024)
    print(f"✓ Audio: {output_path.name} ({mb:.1f} MB)")
    return output_path


# ──────────────────────────────────────────────
# Step 4: Generate RSS Feed
# ──────────────────────────────────────────────

def generate_rss(site_dir: Path, host_url: str, image_url: str = ""):
    """Build the podcast RSS feed from all MP3s in the episodes directory."""
    episodes_dir = site_dir / "episodes"
    episodes = []

    for mp3 in sorted(episodes_dir.glob("*.mp3"), reverse=True):
        match = re.search(r"(\d{4}-\d{2}-\d{2})", mp3.name)
        if not match:
            continue

        ep_date = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        size = mp3.stat().st_size

        # Read episode summary from companion .json
        meta_path = mp3.with_suffix(".json")
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())

        episodes.append({
            "filename": mp3.name,
            "date": ep_date,
            "size": size,
            "title": meta.get("title", f"Runway Briefing — {ep_date.strftime('%B %d, %Y')}"),
            "summary": meta.get("summary", ""),
        })

    items = ""
    for ep in episodes[:100]:
        pub = ep["date"].strftime("%a, %d %b %Y %H:%M:%S +0000")
        url = f"{host_url}/episodes/{ep['filename']}"
        guid = hashlib.md5(ep["filename"].encode()).hexdigest()
        items += f"""
    <item>
      <title>{ep['title']}</title>
      <description><![CDATA[{ep['summary']}]]></description>
      <enclosure url="{url}" length="{ep['size']}" type="audio/mpeg"/>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub}</pubDate>
      <itunes:episodeType>full</itunes:episodeType>
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{PODCAST_TITLE}</title>
    <description>{PODCAST_DESCRIPTION}</description>
    <language>{PODCAST_LANGUAGE}</language>
    <itunes:author>{PODCAST_AUTHOR}</itunes:author>
    <itunes:category text="{PODCAST_CATEGORY}"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{image_url}"/>
    <link>{host_url}</link>
    <atom:link href="{host_url}/feed.xml" rel="self" type="application/rss+xml"/>
    {items}
  </channel>
</rss>"""

    feed_path = site_dir / "feed.xml"
    feed_path.write_text(rss)
    print(f"✓ RSS feed: {len(episodes)} episode(s)")


# ──────────────────────────────────────────────
# Step 5: Email Digest
# ──────────────────────────────────────────────

EMAIL_DIGEST_PROMPT = """You are formatting an aviation news email digest. Given the articles below, do two things:

1. Assign each article to ONE category from this list: ROUTES & NETWORK, AIRLINE OPERATIONS, DEALS & M&A, TECH & INNOVATION, LAWSUITS & REGULATION, INTERNATIONAL, ALSO TODAY
2. Write a one-sentence summary for each article (concise, factual, include key numbers/names)

Output as JSON — an array of objects with fields: "category", "title", "summary", "url", "source"
Sort by category. Only output the JSON array, nothing else."""


def generate_email_digest(articles: list[dict]) -> str:
    """Use Claude to categorize articles and generate an HTML email digest."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  Skipping email: no ANTHROPIC_API_KEY")
        return ""

    # Cap at 30 articles to avoid output truncation
    digest_articles = articles[:30]
    print(f"  Categorizing {len(digest_articles)} articles...")

    client = anthropic.Anthropic(api_key=api_key)
    article_text = "\n\n".join(
        f"Source: {a['source']}\nTitle: {a['title']}\nURL: {a['url']}\nContent: {a.get('text', '')[:300]}"
        for a in digest_articles
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=EMAIL_DIGEST_PROMPT,
        messages=[{"role": "user", "content": f"Here are today's articles:\n\n{article_text}"}],
    )

    # Extract text from response
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text = block.text
            break

    # Parse JSON from response
    try:
        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", response_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        # Try to extract JSON array if there's extra text around it
        json_match = re.search(r"\[[\s\S]*\]", cleaned)
        if json_match:
            cleaned = json_match.group(0)
        categorized = json.loads(cleaned)
        print(f"  ✓ Categorized {len(categorized)} articles")
    except json.JSONDecodeError as e:
        print(f"  Warning: Could not parse digest JSON ({e})")
        print(f"  Raw response (first 500 chars): {response_text[:500]}")
        print("  Warning: Could not parse digest JSON, using flat list")
        categorized = [
            {"category": "TODAY'S STORIES", "title": a["title"], "summary": a.get("text", "")[:200],
             "url": a["url"], "source": a["source"]}
            for a in articles
        ]

    # Group by category
    categories = {}
    for item in categorized:
        cat = item.get("category", "OTHER")
        categories.setdefault(cat, []).append(item)

    # Build HTML email
    today = datetime.now().strftime("%a, %b %d")
    sections_html = ""
    for cat, items in categories.items():
        items_html = ""
        for item in items:
            source = item.get("source", "")
            url = item.get("url", "#")
            summary = item.get("summary", "")
            items_html += f'<li style="margin-bottom:10px;"><a href="{url}" style="color:#1a73e8;font-weight:bold;text-decoration:none;">[{source}]</a> {summary}</li>\n'
        sections_html += f"""
<h3 style="color:#333;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:24px;font-size:14px;text-transform:uppercase;letter-spacing:1px;">{cat}</h3>
<ul style="list-style:none;padding:0;margin:8px 0;">{items_html}</ul>
"""

    # Build SOURCES section from unique sources
    unique_sources = sorted(set(a["source"] for a in articles))
    sources_html = "".join(f'<li style="margin-bottom:4px;color:#666;font-size:13px;">{s}</li>\n' for s in unique_sources)

    html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#333;">
<h2 style="margin-bottom:4px;">Runway Briefing</h2>
<p style="color:#888;margin-top:0;">Today's bundle for {today}.</p>
<h3 style="color:#333;font-size:14px;text-transform:uppercase;letter-spacing:1px;">TODAY'S RUNDOWN</h3>
{sections_html}
<h3 style="color:#333;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:24px;font-size:14px;text-transform:uppercase;letter-spacing:1px;">SOURCES</h3>
<ul style="list-style:none;padding:0;margin:8px 0;">{sources_html}</ul>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="color:#888;font-size:12px;">Runway Briefing — your daily aviation news digest.<br>
<a href="https://ryanholstein.github.io/runway-briefing/episodes/{datetime.now().strftime('%Y-%m-%d')}.mp3" style="color:#1a73e8;">Listen to today's episode</a></p>
</body></html>"""

    return html


def send_email(subject: str, html_body: str, host_url: str, audio_path: Path = None):
    """Send the digest email via Gmail SMTP, optionally attaching the MP3."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT", "ryan.holstein@gmail.com")

    if not gmail_address or not gmail_password:
        print("  Skipping email: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set")
        return

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"Runway Briefing <{gmail_address}>"
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    # Attach MP3 if available
    if audio_path and audio_path.exists():
        with open(audio_path, "rb") as f:
            audio_part = MIMEBase("audio", "mpeg")
            audio_part.set_payload(f.read())
            encoders.encode_base64(audio_part)
            audio_part.add_header("Content-Disposition", f"attachment; filename={audio_path.name}")
            msg.attach(audio_part)
            print(f"  ✓ Attached {audio_path.name}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_password)
        server.sendmail(gmail_address, recipient, msg.as_string())

    print(f"  ✓ Email sent to {recipient}")


# ──────────────────────────────────────────────
# Step 6: Upload to Buzzsprout
# ──────────────────────────────────────────────

def upload_to_buzzsprout(audio_path: Path, title: str, description: str, episode_date: str):
    """Upload an episode to Buzzsprout via their API."""
    api_key = os.environ.get("BUZZSPROUT_API_KEY")
    podcast_id = os.environ.get("BUZZSPROUT_PODCAST_ID")

    if not api_key or not podcast_id:
        print("  Skipping Buzzsprout: BUZZSPROUT_API_KEY or BUZZSPROUT_PODCAST_ID not set")
        return

    print(f"  Uploading to Buzzsprout (podcast {podcast_id})...")

    # Upload audio file
    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"https://www.buzzsprout.com/api/{podcast_id}/episodes.json",
            headers={
                "Authorization": f"Token token={api_key}",
                "User-Agent": "RunwayBriefing/1.0",
            },
            data={
                "title": title,
                "description": description,
                "published_at": f"{episode_date}T10:00:00-04:00",
                "private": "false",
            },
            files={"audio_file": (audio_path.name, f, "audio/mpeg")},
            timeout=180,
        )

    if resp.status_code in (200, 201):
        ep_data = resp.json()
        print(f"  ✓ Uploaded to Buzzsprout: {ep_data.get('title', title)}")
    else:
        print(f"  ⚠ Buzzsprout upload failed ({resp.status_code}): {resp.text[:300]}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    site_dir = Path(os.environ.get("SITE_DIR", "site"))
    episodes_dir = site_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    host_url = os.environ.get("PODCAST_HOST_URL", "https://yourusername.github.io/runway-briefing")
    image_url = os.environ.get("PODCAST_IMAGE_URL", "")

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Connect to database (optional — works without it)
    print("=" * 50)
    print("DATABASE")
    print("=" * 50)
    conn = get_db()
    if conn:
        init_db(conn)
        print("  ✓ Connected to Neon")

    # Step 1: Scrape
    print("=" * 50)
    print("STEP 1: Scrape")
    print("=" * 50)
    articles = scrape_all()

    if not articles:
        print("No fresh articles. Skipping episode.")
        Path("skip_episode").touch()
        if conn:
            conn.close()
        sys.exit(0)

    # Filter out already-covered articles
    if conn:
        articles = filter_already_covered(conn, articles)
        if not articles:
            print("All articles already covered. Skipping episode.")
            Path("skip_episode").touch()
            conn.close()
            sys.exit(0)

    # Get recent episode context for Claude
    recent_context = get_recent_episodes_context(conn) if conn else ""

    # Step 2: Script
    print("=" * 50)
    print("STEP 2: Script")
    print("=" * 50)
    script = generate_script(articles, recent_context)
    print(f"✓ Script generated ({len(script.split())} words)")

    # Extract title and summary from the script
    # Title is the first non-empty line; summary follows "Episode Summary:"
    lines = [l.strip() for l in script.strip().split("\n") if l.strip()]
    title_match = re.search(r"\*\*\"?(.+?)\"?\*\*", script)
    ep_title = title_match.group(1) if title_match else (lines[0] if lines else f"Runway Briefing — {today_str}")
    # Strip metadata prefixes like "Episode Title: "
    ep_title = re.sub(r"^Episode\s+Title:\s*", "", ep_title, flags=re.IGNORECASE).strip()
    summary_match = re.search(r"Episode Summary:\s*(.+)", script)
    ep_summary = summary_match.group(1).strip() if summary_match else ""

    # Save script
    script_path = episodes_dir / f"{today_str}.md"
    script_path.write_text(script)

    # Save metadata
    meta_path = episodes_dir / f"{today_str}.json"
    meta_path.write_text(json.dumps({
        "title": ep_title,
        "summary": ep_summary,
        "date": today_str,
        "article_count": len(articles),
        "sources": list({a["source"] for a in articles}),
    }, indent=2))

    # Step 3: Audio
    print("=" * 50)
    print("STEP 3: Audio")
    print("=" * 50)
    audio_path = episodes_dir / f"{today_str}.mp3"
    generate_audio(script, audio_path)

    # Step 4: RSS
    print("=" * 50)
    print("STEP 4: RSS")
    print("=" * 50)
    generate_rss(site_dir, host_url, image_url)

    # Step 5: Email digest
    print("=" * 50)
    print("STEP 5: Email Digest")
    print("=" * 50)
    try:
        digest_html = generate_email_digest(articles)
        if digest_html:
            today_nice = datetime.now().strftime("%a, %b %d")
            send_email(f"Runway Briefing — {today_nice}", digest_html, host_url, audio_path)
    except Exception as e:
        print(f"  Warning: Email failed ({e}), continuing...")

    # Step 6: Upload to Buzzsprout
    print("=" * 50)
    print("STEP 6: Buzzsprout")
    print("=" * 50)
    try:
        upload_to_buzzsprout(audio_path, ep_title, ep_summary, today_str)
    except Exception as e:
        print(f"  Warning: Buzzsprout upload failed ({e}), continuing...")

    # Step 7: Save to database
    if conn:
        print("=" * 50)
        print("STEP 7: Save to DB")
        print("=" * 50)
        save_episode(conn, articles, ep_title, ep_summary)
        conn.close()

    print("\n✓ Pipeline complete!")


if __name__ == "__main__":
    main()
