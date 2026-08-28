# Runway Briefing

Automated daily aviation news podcast. Scrapes RSS feeds → generates script via Claude → narrates via ElevenLabs → publishes via GitHub Pages.

## Setup (15 minutes)

### 1. Create the repo

Create a new GitHub repo called `runway-briefing` and push this code to it.

### 2. Add your API keys as GitHub secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `ELEVENLABS_API_KEY` | [elevenlabs.io/app/settings/api-keys](https://elevenlabs.io/app/settings/api-keys) |
| `ELEVENLABS_VOICE_ID` | Browse voices at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library), click one, copy the Voice ID |

### 3. Enable GitHub Pages

Go to **Settings → Pages** and set:
- Source: **GitHub Actions**

### 4. Run it

Either wait for the daily cron (10:00 UTC) or go to **Actions → Daily Episode → Run workflow** to trigger manually.

### 5. Submit your RSS feed to podcast platforms

Once the first episode is live, your feed URL is:

```
https://YOUR_USERNAME.github.io/runway-briefing/feed.xml
```

Submit this to:
- [Spotify for Podcasters](https://podcasters.spotify.com) — paste the RSS URL
- [Apple Podcasts Connect](https://podcastsconnect.apple.com) — paste the RSS URL
- Both take 1-3 days to review, then new episodes auto-appear

### 6. Add cover art

Replace `site/cover.jpg` with a 3000×3000 JPEG image for your podcast artwork. This shows up in Spotify, Apple Podcasts, etc.

## File structure

```
runway-briefing/
├── .github/workflows/daily-episode.yml   # Daily cron + deploy
├── pipeline.py                           # The full pipeline
├── requirements.txt                      # Python dependencies
├── site/                                 # GitHub Pages root
│   ├── index.html                        # Landing page
│   ├── feed.xml                          # Podcast RSS feed (auto-generated)
│   ├── cover.jpg                         # Podcast cover art (add your own)
│   └── episodes/                         # Auto-generated
│       ├── 2026-08-28.mp3
│       ├── 2026-08-28.md                 # Script text
│       └── 2026-08-28.json               # Episode metadata
└── README.md
```

## Sources

- [Cranky Flier](https://crankyflier.com) — Industry analysis
- [From the Tray Table](https://fromthetraytable.com) — Airline strategy (Zach Griff)
- [The Points Guy](https://thepointsguy.com) — Consumer airline news
- [GoTravelYourWay](https://gotravelyourway.com) — International aviation (Josh Cahill)

## Customization

- **Change the schedule**: Edit the cron in `.github/workflows/daily-episode.yml`
- **Change the voice**: Update `ELEVENLABS_VOICE_ID` in GitHub secrets
- **Add sources**: Add RSS feed URLs to the `SOURCES` list in `pipeline.py`
- **Edit the script style**: Modify `SYSTEM_PROMPT` in `pipeline.py`
- **Change podcast name**: Update `PODCAST_TITLE` and related constants in `pipeline.py`

## Costs

- **GitHub Actions**: Free (2,000 mins/month on private repos)
- **GitHub Pages**: Free
- **Claude API**: ~$0.01-0.05 per episode
- **ElevenLabs**: ~$5-22/month depending on plan (Starter plan covers a daily 5-min podcast)

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key"
export ELEVENLABS_API_KEY="your-key"
export ELEVENLABS_VOICE_ID="your-voice-id"
python pipeline.py
```
