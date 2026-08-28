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
        "attribution": "Cranky Flier",
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
        "url": "https://thepointsguy.com/news/feed/",
        "attribution": "The Points Guy",
    },
]

FRESHNESS_HOURS = 28

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

ELEVENLABS_MODEL = "eleven_multilingual_v2"
DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # "Adam" — change in GitHub secrets

PODCAST_TITLE = "Runway Briefing"
PODCAST_DESCRIPTION = "Your daily five-minute download on everything happening in the skies."
PODCAST_AUTHOR = "Runway Briefing"
PODCAST_LANGUAGE = "en"
PODCAST_CATEGORY = "News"


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
- NO LONG PARAGRAPHS. Max 2-3 sentences before a break. If you've written more than 3 sentences in a row, you've written too much. Break it up.
- No corporate language. Say "new routes" not "network expansion." Say "first class" not "premium cabin product."
- Sound like you're TALKING, not WRITING. If it sounds like a blog post being read aloud, start over.

## CRITICAL RULES ABOUT LENGTH

- Each story gets ONE key takeaway and ONE "why it matters." That's it. Move on.
- The lead story is 60-90 seconds MAX. That's about 150-200 words. Count them.
- Every other story is 30-60 seconds. About 75-120 words each.
- The ENTIRE script must be 400-600 words. Not 700. Not 800. 400-600.
- If there are 3 articles about the same topic, that's ONE story in the podcast, not three.
- NEVER list out every route, every city, every detail. Pick the 2-3 most interesting facts and skip the rest. Your listener can Google it.
- If you only have one story, make a 90-second episode. That's fine. Don't stretch.

## What NOT to do

- Don't spend the whole episode on one story. Even if all articles are about the same thing, cover it once and keep it tight.
- Don't rehash source articles. Distill them.
- Don't use transitions like "Now let's turn our attention to..." — just go. "So, American."
- Don't be sarcastic or try to be a comedian. Just be natural.
- Never use: "genuinely", "notably", "frankly", "consequential", "let's pump the brakes", "let's be honest"

## Structure

1. **Hook** — 1 sentence that makes someone keep their earbuds in. No intro yet.
2. **Intro** — "You're listening to Runway Briefing. I'm your host. Let's get into it."
3. **Stories** — Cover each story in 45-90 seconds. Lead with the biggest. Hit 2-4 stories total. Keep it moving.
4. **Sign-off** — "That's it for {today}. If you like the show, hit subscribe. See you tomorrow."

## Rules

- Freshness: only cover stories published in the last 24 hours (by original post date, not scrape time).
- Target 400-800 words total. That's 3-5 minutes. Short is better than padded.
- Spoken text only — no stage directions, no [pause], no sound cues.
- Use --- between sections.
- Bold the episode title at the top.
- Include one-line Episode Summary after the title.
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
    """Convert script to MP3 via ElevenLabs API."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set")
        sys.exit(1)

    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
    print(f"Generating audio (voice: {voice_id})...")

    # Strip markdown for cleaner speech
    clean = script
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    clean = re.sub(r"^Episode Summary:.*$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"^---+$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\n{3,}", "\n\n", clean)

    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        json={
            "text": clean.strip(),
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.3,
                "use_speaker_boost": True,
            },
        },
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
    title_match = re.search(r"\*\*\"?(.+?)\"?\*\*", script)
    summary_match = re.search(r"Episode Summary:\s*(.+)", script)
    ep_title = title_match.group(1) if title_match else f"Runway Briefing — {today_str}"
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

    # Step 5: Save to database
    if conn:
        print("=" * 50)
        print("STEP 5: Save to DB")
        print("=" * 50)
        save_episode(conn, articles, ep_title, ep_summary)
        conn.close()

    print("\n✓ Pipeline complete!")


if __name__ == "__main__":
    main()
