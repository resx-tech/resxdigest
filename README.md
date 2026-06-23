# ResX Digest

Posts a curated news digest to Slack twice a week — Monday and Friday at 8am ET.

## Sections (in order)
1. 📍 **New Openings** — NYC 🗽 and London 🇬🇧, with website, Instagram, cover image candidate, and UGC links
2. 🌟 **Hospitality** — insider takes from Feed Me, Mercer Street, On The House, Casper Media
3. 🏢 **Industry** — reservation platform news, M&A, policy
4. 🗺️ **Landscape** — competitor monitoring, auto-refreshed each run (only posts if something new spotted)
5. 🏙️ **City Pulse** — NYC + London culture moments for the 25-35 going-out demo
6. ✨ **Specials & Collabs** — chef x restaurant collabs, limited dishes, pop-ups with a story
7. 🤖 **AI & Tech** — TLDR, The Rundown, TechCrunch, Ben's Bites, HN

---

## Setup (one-time, ~10 minutes)

### 1. Create the Slack app + webhook
1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From Scratch
2. Name it `ResX Digest`, pick your workspace
3. Go to **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
4. Select your `#news` channel (or whatever channel you want) → copy the webhook URL

### 2. Add secrets to GitHub
In your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (from console.anthropic.com → API Keys) |
| `SLACK_WEBHOOK_URL` | The webhook URL from step 1 |

### 3. Test it
**Actions** tab → **ResX News Bot** → **Run workflow** → confirm it posts to Slack

---

## Schedule
Runs Monday and Friday at 8am ET automatically via GitHub Actions.

## Data files (auto-managed)
- `data/seen_openings.json` — restaurants already featured; prevents repeats across digests
- `data/competitors.json` — competitor list, seeded and auto-updated each run

To reset seen openings (e.g. new season): clear `data/seen_openings.json` to `[]`
To add a competitor manually: edit `data/competitors.json` and commit

## Cost
~$3-5/month in Anthropic API costs at 2x/week cadence.
To transfer billing: swap the `ANTHROPIC_API_KEY` secret to a different account's key.
