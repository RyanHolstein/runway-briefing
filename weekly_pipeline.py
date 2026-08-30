#!/usr/bin/env python3
"""
Runway Briefing — Weekly Deep Dive (Conversational Episode)

Runs once a week (Fridays). Queries Neon for the week's biggest story,
generates a two-host dialogue script via Claude, narrates with two
ElevenLabs voices, stitches the audio, and uploads to Buzzsprout.
"""

import os
import sys
import json
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests

# ──────────────────────────────────────────────
# Reuse shared config from daily pipeline
# ──────────────────────────────────────────────

ANTHROPIC_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

ELEVENLABS_MODEL = "eleven_v3"
DEFAULT_VOICE_A = "pNInz6obpgDQGcFmaJgB"  # "Adam" — Host A
DEFAULT_VOICE_B = "ErXwobaYiN019PkySvjV"  # "Antoni" — Host B (change in secrets)

PODCAST_TITLE = "Runway Briefing"

# Import pronunciation tools from main pipeline
from pipeline import PRONUNCIATION_RULES, apply_phonetics, get_or_create_pronunciation_dict


# ──────────────────────────────────────────────
# Database helpers (reused from pipeline.py)
# ──────────────────────────────────────────────

def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("  ⚠ DATABASE_URL not set — cannot run weekly episode")
        return None
    try:
        conn = psycopg2.connect(db_url, sslmode="require")
        return conn
    except Exception as e:
        print(f"  ⚠ DB connection failed: {e}")
        return None


def get_week_articles(conn, days: int = 7) -> list[dict]:
    """Pull all covered articles from the past week."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ca.url, ca.title, ca.source, ca.covered_date,
                   e.title AS episode_title, e.summary AS episode_summary
            FROM covered_articles ca
            LEFT JOIN episodes e ON ca.covered_date = e.episode_date
            WHERE ca.covered_date >= CURRENT_DATE - %s
            ORDER BY ca.covered_date DESC
        """, (days,))
        rows = cur.fetchall()

    articles = []
    for url, title, source, date, ep_title, ep_summary in rows:
        articles.append({
            "url": url,
            "title": title or "",
            "source": source or "",
            "date": str(date),
            "episode_title": ep_title or "",
            "episode_summary": ep_summary or "",
        })
    return articles


def get_week_episodes(conn, days: int = 7) -> list[dict]:
    """Pull episode summaries from the past week."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT episode_date, title, summary, article_count
            FROM episodes
            WHERE episode_date >= CURRENT_DATE - %s
            ORDER BY episode_date ASC
        """, (days,))
        rows = cur.fetchall()

    return [
        {"date": str(d), "title": t or "", "summary": s or "", "count": c or 0}
        for d, t, s, c in rows
    ]


# ──────────────────────────────────────────────
# Step 1: Pick the biggest story of the week
# ──────────────────────────────────────────────

TOPIC_PICKER_PROMPT = """You are the editorial brain for "Runway Briefing", a weekly aviation deep-dive podcast.

Given the articles and episode summaries from this past week, pick THE ONE topic that deserves a 15-minute deep-dive conversation. This should be the story with the most long-term significance for the aviation industry.

Criteria for picking the topic:
1. How many articles/sources covered it? (more = bigger story)
2. How much money or strategic change is involved?
3. Will this still matter in 6 months?
4. Is there genuine debate or tension? (good for conversation)
5. Does it connect to larger industry trends?

Output ONLY a JSON object with these fields:
{
    "topic": "The topic in one sentence",
    "why": "Why this is the biggest story this week (2-3 sentences)",
    "key_facts": ["fact 1", "fact 2", "fact 3", "fact 4", "fact 5"],
    "related_articles": ["article title 1", "article title 2"],
    "angles": ["angle the hosts should explore 1", "angle 2", "angle 3"]
}"""


def pick_weekly_topic(articles: list[dict], episodes: list[dict]) -> dict:
    """Use Claude to pick the week's biggest story."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Build context
    article_text = "\n".join(
        f"- [{a['source']}] {a['title']} ({a['date']})"
        for a in articles
    )
    episode_text = "\n".join(
        f"- {e['date']}: {e['title']} — {e['summary']}"
        for e in episodes
    )

    user_msg = f"""This week's articles ({len(articles)} total):

{article_text}

This week's daily episodes:
{episode_text}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Haiku for fast topic selection
        max_tokens=2048,
        system=TOPIC_PICKER_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text = block.text
            break

    # Parse JSON
    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", response_text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            cleaned = json_match.group(0)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"  Warning: Could not parse topic JSON ({e})")
        # Fallback: just use the most recent episode's topic
        if episodes:
            return {
                "topic": episodes[-1]["title"],
                "why": episodes[-1]["summary"],
                "key_facts": [],
                "related_articles": [],
                "angles": ["What happened", "Why it matters", "What's next"],
            }
        return {"topic": "This week in aviation", "why": "", "key_facts": [], "related_articles": [], "angles": []}


# ──────────────────────────────────────────────
# Step 2: Generate two-host dialogue script
# ──────────────────────────────────────────────

WEEKLY_SCRIPT_PROMPT = """You write scripts for "Runway Briefing Weekly", a conversational deep-dive podcast where two hosts discuss the biggest aviation story of the week.

## The Hosts
- HOST A ("Alex"): The aviation nerd. Follows every fleet order, knows airline history, has strong opinions. More analytical.
- HOST B ("Sam"): The curious generalist. Asks the questions a smart listener would ask. Good at analogies. Keeps things accessible.

## How it should SOUND

This is a REAL CONVERSATION between two people, not a scripted back-and-forth. Think of how two friends who follow the industry would actually discuss this over drinks.

- They interrupt each other (not literally, but short exchanges — no long monologues)
- They disagree sometimes. "I don't know, I think you're being too optimistic about that."
- They laugh at the absurdity of the industry
- They make pop culture references and analogies
- They use filler words occasionally: "I mean...", "Right, so...", "Okay but here's the thing..."
- Neither one gives a speech. Max 3-4 sentences before the other jumps in.
- They build on each other's points. "That's actually a good point. And it connects to..."
- They speculate: "I think what happens next is..." / "If I had to bet..."

## Context and Analysis — THIS IS THE WHOLE POINT

This is a DEEP DIVE, not a news recap. The daily show already covered the headlines. This episode exists to explain:
- The full history and context behind this story
- How much money is at stake
- Who wins and who loses
- What it signals about where the industry is going
- What could happen next — informed speculation

Use your knowledge of aviation history, airline economics, fleet strategy, regulation, and industry trends. Be specific: name airlines, cite approximate numbers, reference past events.

## CRITICAL RULES

- Script must be 2000-2500 words. That's about 15 minutes spoken.
- Format each line as: "ALEX: text" or "SAM: text"
- NO stage directions, NO sound cues, NO [laughs] or [pause]
- NO markdown formatting. No bold, italic, asterisks, or headers. Straight to TTS.
- The first 3 lines are metadata (not spoken):
  Line 1: Episode title — e.g. "Weekly Deep Dive: Why Qantas Retiring the A380 Changes Everything"
  Line 2: Episode Summary: 2-3 sentences for the podcast listing.
  Line 3+: Start the dialogue.
- Alex opens: "Hey, welcome to Runway Briefing Weekly. I'm Alex."
- Sam: "And I'm Sam. So this week we're digging into [topic]..."
- End naturally: "All right, that's the deep dive for this week. We'll be back Monday with the daily. Later."
- Don't rehash every headline from the week. Pick the ONE topic and go deep.
- Never use: "genuinely", "notably", "frankly", "consequential", "let's pump the brakes", "let's be honest"

## The Topic

{topic_json}

## This Week's Daily Episodes (for context, don't repeat these)

{episodes_context}

## Today's Date
{today}
"""


def generate_weekly_script(topic: dict, episodes: list[dict]) -> str:
    """Generate a two-host dialogue script using Claude."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.now().strftime("%A, %B %d, %Y")

    episodes_context = "\n".join(
        f"- {e['date']}: {e['title']} — {e['summary']}"
        for e in episodes
    ) or "No daily episodes this week."

    prompt = WEEKLY_SCRIPT_PROMPT.format(
        topic_json=json.dumps(topic, indent=2),
        episodes_context=episodes_context,
        today=today,
    )

    print("  Generating weekly script...")
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        system=prompt,
        messages=[{"role": "user", "content": "Write the weekly deep-dive episode script."}],
    )

    # Handle potential ThinkingBlock responses
    script = ""
    for block in message.content:
        if hasattr(block, "text"):
            script = block.text
            break

    return script


# ──────────────────────────────────────────────
# Step 3: Generate two-voice audio
# ──────────────────────────────────────────────

def generate_voice_clip(text: str, voice_id: str, api_key: str, pdict: dict = None) -> bytes:
    """Generate audio for a single speaker's line with pronunciation dictionary."""
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.3,
            "use_speaker_boost": True,
        },
    }

    if pdict and pdict.get("id") and pdict.get("version_id"):
        payload["pronunciation_dictionary_locators"] = [{
            "pronunciation_dictionary_id": pdict["id"],
            "version_id": pdict["version_id"],
        }]

    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def parse_dialogue(script: str) -> list[dict]:
    """Parse script into a list of {speaker, text} dicts."""
    # Strip metadata lines (title + summary)
    lines = script.strip().split("\n")
    dialogue_lines = []
    metadata_done = False
    summary_seen = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not metadata_done:
            if stripped.lower().startswith("episode summary:"):
                summary_seen = True
                continue
            if not summary_seen:
                # First non-empty line is the title, skip it
                summary_seen = False  # next line should be summary
                metadata_done = False
                # Actually let's be simpler: first line = title, second starts with "Episode Summary"
                continue
            metadata_done = True

        # Parse speaker lines
        match = re.match(r"^(ALEX|SAM):\s*(.+)", stripped, re.IGNORECASE)
        if match:
            speaker = match.group(1).upper()
            text = match.group(2).strip()
            dialogue_lines.append({"speaker": speaker, "text": text})
        elif dialogue_lines:
            # Continuation of previous speaker's line
            dialogue_lines[-1]["text"] += " " + stripped

    return dialogue_lines


def generate_weekly_audio(script: str, output_path: Path) -> Path:
    """Generate two-voice audio by stitching speaker clips together."""
    from pydub import AudioSegment
    import io

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set")
        sys.exit(1)

    voice_a = os.environ.get("ELEVENLABS_VOICE_A", os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_A))
    voice_b = os.environ.get("ELEVENLABS_VOICE_B", DEFAULT_VOICE_B)

    print(f"  Voice A (Alex): {voice_a}")
    print(f"  Voice B (Sam): {voice_b}")

    # Get pronunciation dictionary
    pdict = get_or_create_pronunciation_dict()
    if pdict:
        print(f"  Using pronunciation dictionary: {pdict.get('id', 'none')}")

    # Parse dialogue
    dialogue = parse_dialogue(script)
    if not dialogue:
        print("  ERROR: Could not parse any dialogue lines from script")
        sys.exit(1)

    print(f"  Parsed {len(dialogue)} dialogue segments")

    # Generate audio for each segment
    combined = AudioSegment.empty()
    short_pause = AudioSegment.silent(duration=300)   # 300ms between turns
    long_pause = AudioSegment.silent(duration=600)     # 600ms for topic shifts

    for i, segment in enumerate(dialogue):
        speaker = segment["speaker"]
        text = segment["text"]

        # Fallback text replacement only if no dictionary
        if not pdict:
            text = apply_phonetics(text)

        # Pick voice
        voice_id = voice_a if speaker == "ALEX" else voice_b

        print(f"  [{i+1}/{len(dialogue)}] {speaker}: {text[:60]}...")

        try:
            audio_bytes = generate_voice_clip(text, voice_id, api_key, pdict)
            clip = AudioSegment.from_mp3(io.BytesIO(audio_bytes))
            combined += clip + short_pause
        except Exception as e:
            print(f"    ⚠ Failed to generate clip: {e}")
            continue

    # Export
    combined.export(output_path, format="mp3", bitrate="128k")
    mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ Weekly audio: {output_path.name} ({mb:.1f} MB, {len(combined)/1000:.0f}s)")
    return output_path


# ──────────────────────────────────────────────
# Step 4: Upload to Buzzsprout (with custom artwork)
# ──────────────────────────────────────────────

def upload_weekly_to_buzzsprout(audio_path: Path, title: str, description: str, episode_date: str, artwork_url: str = ""):
    """Upload weekly episode to Buzzsprout with optional custom artwork."""
    api_key = os.environ.get("BUZZSPROUT_API_KEY")
    podcast_id = os.environ.get("BUZZSPROUT_PODCAST_ID")

    if not api_key or not podcast_id:
        print("  Skipping Buzzsprout: BUZZSPROUT_API_KEY or BUZZSPROUT_PODCAST_ID not set")
        return

    print(f"  Uploading weekly episode to Buzzsprout...")

    data = {
        "title": title,
        "description": description,
        "published_at": f"{episode_date}T12:00:00-04:00",
        "private": "false",
        "episode_number": "",
        "tags": "weekly,deep-dive",
    }
    if artwork_url:
        data["artwork_url"] = artwork_url

    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"https://www.buzzsprout.com/api/{podcast_id}/episodes.json",
            headers={
                "Authorization": f"Token token={api_key}",
                "User-Agent": "RunwayBriefing/1.0",
            },
            data=data,
            files={"audio_file": (audio_path.name, f, "audio/mpeg")},
            timeout=300,
        )

    if resp.status_code in (200, 201):
        ep_data = resp.json()
        print(f"  ✓ Uploaded weekly to Buzzsprout: {ep_data.get('title', title)}")
    else:
        print(f"  ⚠ Buzzsprout upload failed ({resp.status_code}): {resp.text[:300]}")


# ──────────────────────────────────────────────
# Step 5: Save weekly episode to DB
# ──────────────────────────────────────────────

def save_weekly_episode(conn, title: str, summary: str, topic: dict):
    """Save the weekly episode to the database."""
    if not conn:
        return

    today = datetime.now().date()
    with conn.cursor() as cur:
        # Create weekly_episodes table if needed
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weekly_episodes (
                id SERIAL PRIMARY KEY,
                episode_date DATE UNIQUE NOT NULL,
                title TEXT,
                summary TEXT,
                topic_json JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            INSERT INTO weekly_episodes (episode_date, title, summary, topic_json)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (episode_date) DO UPDATE
            SET title = EXCLUDED.title, summary = EXCLUDED.summary, topic_json = EXCLUDED.topic_json
        """, (today, title, summary, json.dumps(topic)))
        conn.commit()
    print(f"  ✓ Saved weekly episode to database")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    site_dir = Path(os.environ.get("SITE_DIR", "site"))
    episodes_dir = site_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    host_url = os.environ.get("PODCAST_HOST_URL", "https://yourusername.github.io/runway-briefing")
    weekly_artwork_url = os.environ.get("WEEKLY_ARTWORK_URL", "")

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Connect to DB
    print("=" * 50)
    print("WEEKLY DEEP DIVE")
    print("=" * 50)
    conn = get_db()
    if not conn:
        print("ERROR: Database required for weekly episodes (need the week's data)")
        sys.exit(1)

    # Step 1: Get this week's data from Neon
    print("\n" + "=" * 50)
    print("STEP 1: Gather Week's Data")
    print("=" * 50)
    articles = get_week_articles(conn)
    episodes = get_week_episodes(conn)
    print(f"  Found {len(articles)} articles from {len(episodes)} daily episodes")

    if not articles:
        print("  No articles this week. Skipping weekly episode.")
        Path("skip_episode").touch()
        conn.close()
        sys.exit(0)

    # Step 2: Pick the biggest topic
    print("\n" + "=" * 50)
    print("STEP 2: Pick Topic")
    print("=" * 50)
    topic = pick_weekly_topic(articles, episodes)
    print(f"  ✓ Topic: {topic.get('topic', 'Unknown')}")
    print(f"    Why: {topic.get('why', '')[:200]}")

    # Step 3: Generate dialogue script
    print("\n" + "=" * 50)
    print("STEP 3: Generate Script")
    print("=" * 50)
    script = generate_weekly_script(topic, episodes)
    print(f"  ✓ Script generated ({len(script.split())} words)")

    # Extract title and summary
    lines = [l.strip() for l in script.strip().split("\n") if l.strip()]
    ep_title = lines[0] if lines else f"Runway Briefing Weekly — {today_str}"
    # Clean title of any leftover formatting
    ep_title = re.sub(r"\*\*\"?(.+?)\"?\*\*", r"\1", ep_title)

    summary_match = re.search(r"Episode Summary:\s*(.+)", script)
    ep_summary = summary_match.group(1).strip() if summary_match else topic.get("why", "")

    # Save script
    script_path = episodes_dir / f"weekly-{today_str}.md"
    script_path.write_text(script)

    # Save metadata
    meta_path = episodes_dir / f"weekly-{today_str}.json"
    meta_path.write_text(json.dumps({
        "title": ep_title,
        "summary": ep_summary,
        "date": today_str,
        "type": "weekly",
        "topic": topic,
    }, indent=2))

    # Step 4: Generate two-voice audio
    print("\n" + "=" * 50)
    print("STEP 4: Generate Audio")
    print("=" * 50)
    audio_path = episodes_dir / f"weekly-{today_str}.mp3"
    generate_weekly_audio(script, audio_path)

    # Step 5: Upload to Buzzsprout
    print("\n" + "=" * 50)
    print("STEP 5: Buzzsprout")
    print("=" * 50)
    try:
        upload_weekly_to_buzzsprout(audio_path, ep_title, ep_summary, today_str, weekly_artwork_url)
    except Exception as e:
        print(f"  ⚠ Buzzsprout upload failed ({e}), continuing...")

    # Step 6: Save to DB
    print("\n" + "=" * 50)
    print("STEP 6: Save to DB")
    print("=" * 50)
    save_weekly_episode(conn, ep_title, ep_summary, topic)
    conn.close()

    print(f"\n✓ Weekly pipeline complete!")


if __name__ == "__main__":
    main()
