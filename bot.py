"""
ResX News Bot
Runs 3x/week via GitHub Actions. Searches for curated news across 7 sections,
formats a Slack digest, and posts to #news channel via Incoming Webhook.

Sections (in order):
  1. New Openings (NYC + London)
  2. Hospitality
  3. Industry
  4. Landscape
  5. City Pulse
  6. Specials & Collabs
  7. AI & Tech
"""

import os
import json
import re
import urllib.request
import datetime
from pathlib import Path

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

SEEN_OPENINGS_FILE = Path("data/seen_openings.json")
COMPETITORS_FILE   = Path("data/competitors.json")

# ---------------------------------------------------------------------------
# Signal accounts — used as vibe/trend calibration, NOT cited directly
# ---------------------------------------------------------------------------
NYC_SIGNAL_ACCOUNTS = [
    "@tinx (aspirational 25-35 NYC city life, Rich Mom energy)",
    "@dinnerserviceny (hospitality insider, restaurant industry pulse)",
    "@nolitadirtbag (downtown NYC cultural barometer, Dimes Square / Nolita scene)",
    "@chatprojectpal (things to do with friends, social plans lens)",
    "@juliamervis (normal cool girl in NYC, 25-35 taste)",
]
LONDON_SIGNAL_ACCOUNTS = [
    "@realhousewivesofclapton (London equivalent of Nolita Dirtbag, east London creative scene)",
    "@socks_house_meeting (art school / high-fashion London scene)",
    "@dinnerbyben (London restaurant insider content)",
    "@prettylittlelondon (aspirational London lifestyle, going out)",
    "@poundlandbandit (broader London culture meme account)",
]

# ---------------------------------------------------------------------------
# Seed competitor list
# ---------------------------------------------------------------------------
SEED_COMPETITORS = [
    "Appointment Trader", "Dorsia", "Diibs", "Quenelle", "Table Agent",
    "Resy Notify", "OpenTable Notify",
    "Tock", "Blackbird", "The Infatuation", "Eater",
    "reservation scalper bots", "Telegram reservation groups",
    "DesignMyNight", "Hot Dinners",
]

# ---------------------------------------------------------------------------
# Curated source lists per section
# ---------------------------------------------------------------------------
SOURCES = {
    "openings_nyc": [
        "The Infatuation NYC new openings",
        "Eater NY new restaurant openings",
        "Resy blog new NYC restaurants",
        "New York Times dining new openings",
        "Time Out New York new restaurants",
    ],
    "openings_london": [
        "Hot Dinners new London restaurant openings",
        "DesignMyNight new London restaurants June 2026",
        "Time Out London best new restaurants",
        "ES Magazine London restaurant openings",
        "The Nudge London new openings",
    ],
    "hospitality": [
        "Feed Me Emily Sundberg Substack latest issue",
        "Mercer Street Hospitality Substack latest",
        "On The House Substack restaurant news",
        "Casper Media Instagram hospitality news",
        "Eater restaurant industry news this week",
        "Bloomberg Pursuits dining news",
    ],
    "industry": [
        "Restaurant Business Online reservation technology news",
        "Nation's Restaurant News technology this week",
        "Skift Table hospitality business news",
        "Fast Company restaurant tech news",
        "OpenTable Resy SevenRooms DoorDash news",
    ],
    "city_pulse_nyc": [
        "NY Post lifestyle going out NYC this week",
        "The Cut NYC trend what people are doing",
        "NYT Styles New York culture moment",
        "Curbed NY city life neighborhood news",
        "Rachel Janfaza Up and Up Substack Gen Z culture",
        "Blackbird Spyplane NYC culture",
        "Dirt media internet culture NYC",
    ],
    "city_pulse_london": [
        "Time Out London things to do this week",
        "ES Magazine London going out scene",
        "Vittles London food culture",
        "Ganymede magazine London",
        "Secret London what's on",
    ],
    "specials": [
        "NYC chef collaboration limited edition dish restaurant June 2026",
        "London chef collab limited menu pop-up residency June 2026",
        "NYC restaurant special seasonal menu item this week",
        "London restaurant special collab dish this week",
    ],
    "ai_tech": [
        "TLDR newsletter AI this week",
        "The Rundown AI latest",
        "TechCrunch AI agents news",
        "Ben's Bites AI tools this week",
        "Hacker News top AI stories",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def call_anthropic(messages: list, system: str, max_tokens: int = 4096) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )


def post_to_slack(blocks: list):
    payload = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def verify_url(url: str) -> bool:
    """Return True only if the URL returns a 200-range status."""
    if not url or not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Step 1 — Refresh competitor list
# ---------------------------------------------------------------------------

def refresh_competitors() -> tuple[list, list]:
    existing = load_json(COMPETITORS_FILE, SEED_COMPETITORS)

    result = call_anthropic(
        messages=[{
            "role": "user",
            "content": (
                "Search for any NEW restaurant reservation apps, last-minute dining platforms, "
                "or reservation marketplace startups that have launched or received significant press "
                "in the past 2 weeks. Focus on competitors to a last-minute restaurant reservation "
                "marketplace operating in NYC and London. "
                "Return ONLY a JSON array of company/product name strings. "
                "If nothing new, return []."
            ),
        }],
        system="You are a competitive intelligence researcher. Return only valid JSON arrays, no markdown.",
        max_tokens=500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        new_entries = json.loads(clean)
        if not isinstance(new_entries, list):
            new_entries = []
    except Exception:
        new_entries = []

    existing_lower = {e.lower() for e in existing}
    truly_new = [e for e in new_entries if e.lower() not in existing_lower]
    full_list = existing + truly_new

    save_json(COMPETITORS_FILE, full_list)
    return full_list, truly_new


# ---------------------------------------------------------------------------
# Step 2 — Research openings
# ---------------------------------------------------------------------------

def research_openings(city: str, seen: set) -> dict:
    seen_str = ", ".join(seen) if seen else "none yet"
    city_label = "NYC" if city == "nyc" else "London"
    sources = SOURCES[f"openings_{city}"]
    signal_accounts = NYC_SIGNAL_ACCOUNTS if city == "nyc" else LONDON_SIGNAL_ACCOUNTS

    prompt = f"""
You are researching new restaurant openings in {city_label} for a weekly digest aimed at the team 
at ResX — a last-minute restaurant reservation marketplace for 25-35 year olds in NYC and London.

Use these sources: {', '.join(sources)}

As a vibe calibration, the target audience is similar to followers of these accounts 
(use as signal only, do NOT cite them): {', '.join(signal_accounts)}

Find the 3 most notable NEW restaurant openings from the past week.
EXCLUDE these already featured: {seen_str}

For each restaurant return:
- name: restaurant name
- date: opening date (e.g. "June 18" or "opens June 29")  
- blurb: 1 punchy sentence — vibe, concept, what makes it notable. No reservation logistics.
- website: ONLY include if you can confirm the URL actually resolves. Leave blank if unsure.
- instagram_handle: e.g. @restaurantname — only if you can confirm it exists
- instagram_url: full Instagram profile URL — only if confirmed
- cover_image_post: URL to a specific Instagram POST (not profile) with great food or vibe photo 
  we could DM them about for a cover image. Only include if you found a real, specific post URL.

Also find 3-5 UGC posts (Instagram reels or TikToks) about these openings from food creators 
(not the restaurant's own account). Only include URLs you've actually found and confirmed exist.

Return ONLY valid JSON:
{{
  "items": [
    {{
      "name": "...",
      "date": "...",
      "blurb": "...",
      "website": "...",
      "instagram_handle": "...",
      "instagram_url": "...",
      "cover_image_post": "..."
    }}
  ],
  "ugc": ["url1", "url2"]
}}
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system="You are a food media researcher. Only include URLs you have actually verified exist. Return only valid JSON, no markdown.",
        max_tokens=2000,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            data = json.loads(match.group())
            # Verify URLs before returning
            for item in data.get("items", []):
                if item.get("website") and not verify_url(item["website"]):
                    item["website"] = ""
                if item.get("instagram_url") and not verify_url(item["instagram_url"]):
                    item["instagram_url"] = ""
                    item["instagram_handle"] = ""
                if item.get("cover_image_post") and not verify_url(item["cover_image_post"]):
                    item["cover_image_post"] = ""
            data["ugc"] = [u for u in data.get("ugc", []) if verify_url(u)]
            return data
    except Exception as e:
        print(f"Error parsing openings for {city}: {e}")

    return {"items": [], "ugc": []}


# ---------------------------------------------------------------------------
# Step 3 — Research news sections
# ---------------------------------------------------------------------------

def research_section(section: str, competitors: list = None) -> list:
    """Returns list of dicts: {headline, detail, so_what, city}"""

    city_label_instruction = (
        "For each item, also include a 'city' field: either 'NYC', 'LDN', or 'BOTH'."
    )

    if section == "landscape":
        comp_str = ", ".join(competitors or SEED_COMPETITORS)
        prompt = f"""
Search for news from the past week about these restaurant reservation competitors 
and the broader reservation/dining landscape: {comp_str}

Also search for: restaurant reservation regulation news, reservation bot crackdowns,
new reservation-adjacent features from Google/Apple Maps, dining trend shifts.

Find 2-3 most relevant items. {city_label_instruction}
For each return: headline, detail (1-2 sentences), so_what (1 sentence why it matters for ResX), city.
"""

    elif section == "hospitality":
        sources_str = ", ".join(SOURCES["hospitality"])
        signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)
        prompt = f"""
Search these insider hospitality sources: {sources_str}

As vibe calibration for what the 25-35 audience cares about, these accounts are signal 
(do NOT cite them directly): {signal}

Look for: chef moves, brand x restaurant collabs, notable closures, food media moments, 
industry gossip, chef/restaurant cultural moments. Prioritize NYC and London.

Find 2-3 items. {city_label_instruction}
For each return: headline, detail (1-2 sentences), so_what (1 sentence), city.
"""

    elif section == "industry":
        sources_str = ", ".join(SOURCES["industry"])
        prompt = f"""
Search these industry sources this week: {sources_str}

Look for: M&A in hospitality tech, platform updates (OpenTable, Resy, SevenRooms, DoorDash, Uber Eats),
restaurant industry business news, funding rounds, policy changes affecting restaurants.

Find 2-3 items. {city_label_instruction}
For each return: headline, detail (1-2 sentences), so_what (1 sentence for ResX), city.
"""

    elif section == "city_pulse":
        nyc_sources = ", ".join(SOURCES["city_pulse_nyc"])
        ldn_sources = ", ".join(SOURCES["city_pulse_london"])
        signal_nyc = ", ".join(NYC_SIGNAL_ACCOUNTS)
        signal_ldn = ", ".join(LONDON_SIGNAL_ACCOUNTS)
        prompt = f"""
You are finding 3-4 city culture moments from the past week for a 25-35 going-out audience 
in NYC and London.

NYC sources: {nyc_sources}
London sources: {ldn_sources}

NYC signal accounts (use as vibe calibration, do NOT cite): {signal_nyc}
London signal accounts (use as vibe calibration, do NOT cite): {signal_ldn}

Look for: cultural trends, what the city is obsessed with, experiences people are seeking out,
social moments, things driving people to make plans. NOT generic events listings.
Think: gaming clubs having a moment, a film everyone's talking about that ties to a dining scene,
a neighbourhood suddenly having energy, a behaviour shift in how people are going out.

Find 2 NYC items and 2 London items. {city_label_instruction}
For each return: headline, detail (1-2 sentences), so_what (1 sentence connecting to ResX's world), city.
"""

    elif section == "specials":
        sources_str = ", ".join(SOURCES["specials"])
        prompt = f"""
Search for limited-time restaurant specials and chef collaborations this week in NYC and London.

Sources: {sources_str}

You are looking SPECIFICALLY for:
- Named chef x restaurant collabs with a specific dish (e.g. "Chef X x Restaurant Y = The [Dish Name]")
- Limited-time / seasonal menu items at notable spots with a story behind them
- Pop-up residencies with a clear end date and a specific menu
- Things with strong social/visual potential — a great name, a narrative, obvious Instagram appeal

NOT interested in: general prix-fixe deals, restaurant week, generic seasonal menus without a story.

Find 2-3 items across NYC and London. {city_label_instruction}
For each return: headline (punchy, include the dish/collab name), detail (1-2 sentences including 
dates available and what makes it special), so_what (1 sentence on social/content angle for ResX), city.
"""

    elif section == "ai_tech":
        sources_str = ", ".join(SOURCES["ai_tech"])
        prompt = f"""
Search these sources for the past week: {sources_str}

Look for: new AI tools useful for a small software startup, agent/automation launches,
model updates from Anthropic/OpenAI/Google, developer tools, product news relevant to
a React Native + Node/TypeScript + Firebase stack. Include only things with real 
practical relevance — not hype.

Find 2-3 items. City field should always be 'BOTH' for AI/Tech.
For each return: headline, detail (1-2 sentences), so_what (1 sentence on how it applies), city.
"""
    else:
        return []

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a sharp analyst writing for a small startup team. "
            "Be punchy and specific. Return only a valid JSON array of objects with keys: "
            "headline, detail, so_what, city. No markdown fences."
        ),
        max_tokens=1500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"Error parsing {section}: {e}")

    return []


# ---------------------------------------------------------------------------
# Step 4 — Format Slack blocks
# ---------------------------------------------------------------------------

CITY_TAG = {
    "NYC": "[NYC]",
    "LDN": "[LDN]",
    "BOTH": "",
}

def city_prefix(item: dict) -> str:
    tag = CITY_TAG.get(item.get("city", "BOTH"), "")
    return f"{tag} " if tag else ""


def format_opening_item(item: dict) -> str:
    name = item.get("name", "")
    date = item.get("date", "")
    blurb = item.get("blurb", "")
    website = item.get("website", "")
    ig_handle = item.get("instagram_handle", "")
    ig_url = item.get("instagram_url", "")
    cover = item.get("cover_image_post", "")

    # Name line
    name_str = f"*<{website}|{name}>*" if website else f"*{name}*"
    if date:
        name_str += f"  _{date}_"

    lines = [f"▪️ {name_str}"]
    if blurb:
        lines.append(blurb)

    links = []
    if ig_handle and ig_url:
        links.append(f"<{ig_url}|{ig_handle}>")
    elif ig_handle:
        links.append(ig_handle)
    if cover:
        links.append(f"<{cover}|📸 cover image candidate>")
    if links:
        lines.append(" · ".join(links))

    return "\n".join(lines)


def format_news_items(items: list) -> str:
    lines = []
    for item in items:
        prefix = city_prefix(item)
        headline = item.get("headline", "")
        detail = item.get("detail", "")
        so_what = item.get("so_what", "")
        lines.append(f"• {prefix}*{headline}*\n  {detail}\n  _↳ {so_what}_")
    return "\n\n".join(lines)


def build_slack_blocks(
    date_str: str,
    nyc_data: dict,
    london_data: dict,
    hospitality: list,
    industry: list,
    landscape: list,
    city_pulse: list,
    specials: list,
    ai_tech: list,
    new_competitors: list,
) -> list:

    blocks = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🍽️  ResX Weekly Brief  ·  {date_str}"},
    })
    blocks.append({"type": "divider"})

    # ── 1. New Openings ─────────────────────────────────────────────────────
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*📍  NEW OPENINGS*"},
    })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*🗽  NYC*"},
    })
    for item in nyc_data.get("items", []):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": format_opening_item(item)},
        })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"},
    })
    for item in london_data.get("items", []):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": format_opening_item(item)},
        })

    # UGC
    all_ugc = nyc_data.get("ugc", []) + london_data.get("ugc", [])
    if all_ugc:
        ugc_links = "  ·  ".join(f"<{u}|[post]>" for u in all_ugc[:6])
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📲  UGC to repost:*  {ugc_links}"},
        })

    blocks.append({"type": "divider"})

    # ── 2. Hospitality ──────────────────────────────────────────────────────
    if hospitality:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🌟  HOSPITALITY*\n\n{format_news_items(hospitality)}"},
        })
        blocks.append({"type": "divider"})

    # ── 3. Industry ─────────────────────────────────────────────────────────
    if industry:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🏢  INDUSTRY*\n\n{format_news_items(industry)}"},
        })
        blocks.append({"type": "divider"})

    # ── 4. Landscape ────────────────────────────────────────────────────────
    if landscape or new_competitors:
        landscape_text = format_news_items(landscape) if landscape else ""
        if new_competitors:
            comp_str = ", ".join(new_competitors)
            new_comp_block = f"\n\n👀 *New competitor spotted:* {comp_str}"
            landscape_text = (landscape_text + new_comp_block).strip()
        if landscape_text:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🗺️  LANDSCAPE*\n\n{landscape_text}"},
            })
            blocks.append({"type": "divider"})

    # ── 5. City Pulse ───────────────────────────────────────────────────────
    if city_pulse:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🏙️  CITY PULSE*\n\n{format_news_items(city_pulse)}"},
        })
        blocks.append({"type": "divider"})

    # ── 6. Specials & Collabs ───────────────────────────────────────────────
    if specials:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*✨  SPECIALS & COLLABS*\n\n{format_news_items(specials)}"},
        })
        blocks.append({"type": "divider"})

    # ── 7. AI & Tech ────────────────────────────────────────────────────────
    if ai_tech:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🤖  AI & TECH*\n\n{format_news_items(ai_tech)}"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "ResX News Bot  ·  Powered by Claude  ·  Mon / Wed / Fri",
        }],
    })

    return blocks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.date.today().strftime("%B %d, %Y")
    print(f"Running ResX News Bot — {today}")

    seen_openings = set(load_json(SEEN_OPENINGS_FILE, []))

    print("Refreshing competitor list...")
    competitors, new_competitors = refresh_competitors()

    print("Researching NYC openings...")
    nyc_data = research_openings("nyc", seen_openings)

    print("Researching London openings...")
    london_data = research_openings("london", seen_openings)

    print("Researching hospitality...")
    hospitality = research_section("hospitality")

    print("Researching industry...")
    industry = research_section("industry")

    print("Researching landscape...")
    landscape = research_section("landscape", competitors)

    print("Researching city pulse...")
    city_pulse = research_section("city_pulse")

    print("Researching specials & collabs...")
    specials = research_section("specials")

    print("Researching AI & tech...")
    ai_tech = research_section("ai_tech")

    # Update seen openings
    new_names = (
        [item["name"] for item in nyc_data.get("items", [])]
        + [item["name"] for item in london_data.get("items", [])]
    )
    seen_openings.update(new_names)
    save_json(SEEN_OPENINGS_FILE, list(seen_openings))

    print("Building Slack blocks...")
    blocks = build_slack_blocks(
        date_str=today,
        nyc_data=nyc_data,
        london_data=london_data,
        hospitality=hospitality,
        industry=industry,
        landscape=landscape,
        city_pulse=city_pulse,
        specials=specials,
        ai_tech=ai_tech,
        new_competitors=new_competitors,
    )

    print("Posting to Slack...")
    post_to_slack(blocks)
    print("Done ✓")


if __name__ == "__main__":
    main()
