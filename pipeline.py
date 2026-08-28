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

FRESHNESS_HOURS = 72  # Set to 72h for testing; change back to 28 once confirmed working

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
# Step 2: Generate Script (Claude API)
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """You are a podcast scriptwriter for "Runway Briefing", a daily aviation news podcast. You write scripts that sound like a knowledgeable friend catching the listener up on the day's aviation news over coffee — informed, opinionated where appropriate, but never dry or robotic.

## Voice & Tone
- Conversational and energetic, like a host who genuinely loves aviation
- Use contractions, rhetorical questions, and natural speech patterns
- Okay to have opinions ("This is a big deal because…", "I'm not sure that math works out…")
- Avoid corporate-speak and press-release language — rewrite everything in your own words
- Reference context the audience would know
- Light humor is welcome; forced jokes are not

## Structure
1. Cold Open (1-2 punchy sentences teasing the biggest story)
2. Intro: "You're listening to Runway Briefing — your daily five-minute download on everything happening in the skies. I'm your host. Let's taxi to the runway."
3. Lead Story (60-90 seconds spoken)
4. Additional Stories (30-60 seconds each, 2-4 stories)
5. Quick Hits (optional, 30 seconds for minor stories)
6. Sign-Off: "That's your Runway Briefing for [TODAY'S DATE]. If you're enjoying the show, hit subscribe — it helps more than you know. I'll see you back here tomorrow. Blue skies."

## Rules
- Only cover stories whose original publish date falls within the last 24 hours. Freshness is based on when the article was posted, not when it was scraped or ingested.
- If input includes older articles, ignore them unless they provide essential context for a fresh story.
- Target 750-1,200 words (~5-8 minutes spoken)
- Write as spoken text only — no stage directions, no sound cues, no [pause] markers
- Use --- to separate sections
- Attribute sources naturally: "According to Cranky Flier…", "Zach Griff at From the Tray Table is reporting…"
- Merge overlapping stories from different sources into one richer segment
- If fewer than 3 stories, make a shorter episode (~3-4 minutes) rather than padding
- Skip credit card deals, hotel news, and points strategy unless directly tied to an airline story
- Bold the episode title at the top
- Include a one-line Episode Summary after the title
- On slow news days, it's okay to be shorter. Never pad.

## Today's Date
{today}
"""


def generate_script(articles: list[dict]) -> str:
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
        system=SYSTEM_PROMPT.format(today=today),
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

    # Step 1: Scrape
    print("=" * 50)
    print("STEP 1: Scrape")
    print("=" * 50)
    articles = scrape_all()

    if not articles:
        print("No fresh articles. Skipping episode.")
        # Write a flag file so the workflow knows to skip
        Path("skip_episode").touch()
        sys.exit(0)

    # Step 2: Script
    print("=" * 50)
    print("STEP 2: Script")
    print("=" * 50)
    script = generate_script(articles)
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

    print("\n✓ Pipeline complete!")


if __name__ == "__main__":
    main()
