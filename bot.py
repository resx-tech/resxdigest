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
WATCHING_FILE      = Path("data/watching.json")

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
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Slack error {e.code}: {body}")
        raise


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
        start = clean.index("[")
        new_entries, _ = json.JSONDecoder().raw_decode(clean, start)
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

def research_openings(city: str, seen: set, watching: list) -> dict:
    """
    Returns:
      {
        "just_opened": {"items": [...], "ugc": [...]},
        "coming_soon": [...]
      }
    """
    seen_str = ", ".join(seen) if seen else "none yet"
    city_label = "NYC" if city == "nyc" else "London"
    sources = SOURCES[f"openings_{city}"]
    signal_accounts = NYC_SIGNAL_ACCOUNTS if city == "nyc" else LONDON_SIGNAL_ACCOUNTS

    city_key = "NYC" if city == "nyc" else "LDN"
    city_watching = [w for w in watching if w.get("city", "").upper() in (city_key, "BOTH")]
    watching_str = ", ".join(w["name"] for w in city_watching) if city_watching else "none"

    prompt = f"""
You are researching restaurant openings in {city_label} for a weekly digest aimed at the team
at ResX — a last-minute restaurant reservation marketplace for 25-35 year olds in NYC and London.

Use these sources: {', '.join(sources)}

As a vibe calibration, the target audience is similar to followers of these accounts
(use as signal only, do NOT cite them): {', '.join(signal_accounts)}

Currently watching (announced but not yet open as of last run): {watching_str}
If any of these have now opened, include them in JUST OPENED.

Return TWO lists:

1. JUST OPENED: Up to 3 restaurants that actually opened THIS WEEK (verifiably open, taking reservations or walk-ins).
   EXCLUDE already seen: {seen_str}

2. COMING SOON: Up to 3 noteworthy restaurants announced for an upcoming opening (not yet open).
   These will be tracked week-to-week until they open.

For each restaurant in BOTH lists return:
- name: restaurant name
- date: opening date (e.g. "June 18") or "opens [date]" for coming soon
- blurb: 1 punchy sentence, max 12 words — vibe, concept, what makes it notable
- city: "{city_key}"
- website: only if you can confirm the URL resolves. Leave blank if unsure.
- instagram_handle: official Instagram handle e.g. @restaurantname — search for it, required
- instagram_url: full official Instagram profile URL — required, search for it (e.g. https://www.instagram.com/restaurantname)
- cover_image_post: URL to a UGC post from an influencer or regular person (NOT the restaurant's own account) that showcases the food or vibe — Instagram reel, TikTok, or post. Must be a real person's account. Only include if you've confirmed the URL exists.

For JUST OPENED also find 3-5 UGC posts (Instagram reels or TikToks) from food creators
(not the restaurant's own account). For each include the URL and a short label — creator handle
plus what it shows, e.g. "@foodie_nyc reviews the tasting menu". Only include URLs you've confirmed exist.

Return ONLY valid JSON:
{{
  "just_opened": {{
    "items": [
      {{
        "name": "...", "date": "...", "blurb": "...", "city": "...",
        "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "..."
      }}
    ],
    "ugc": [{{"url": "...", "label": "..."}}]
  }},
  "coming_soon": [
    {{
      "name": "...", "date": "...", "blurb": "...", "city": "...",
      "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "..."
    }}
  ]
}}
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system="You are a food media researcher. Only include URLs you have actually verified exist. Return only valid JSON, no markdown.",
        max_tokens=2500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        # Find the start of the JSON object and use raw_decode to stop at its end
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        all_items = data.get("just_opened", {}).get("items", []) + data.get("coming_soon", [])
        for item in all_items:
            if item.get("website") and not verify_url(item["website"]):
                item["website"] = ""
            if item.get("instagram_url") and not verify_url(item["instagram_url"]):
                item["instagram_url"] = ""
                item["instagram_handle"] = ""
            if item.get("cover_image_post") and not verify_url(item["cover_image_post"]):
                item["cover_image_post"] = ""
        ugc = data.get("just_opened", {}).get("ugc", [])
        if ugc:
            # Handle both dict {"url":..,"label":..} and plain string formats
            data["just_opened"]["ugc"] = [
                u for u in ugc
                if isinstance(u, dict) and verify_url(u.get("url", ""))
            ]
        return data
    except Exception as e:
        print(f"Error parsing openings for {city}: {e}")

    return {"just_opened": {"items": [], "ugc": []}, "coming_soon": []}


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
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words, why it matters for ResX), url (direct article link if available), city.
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
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words), url (direct article link if available), city.
"""

    elif section == "industry":
        sources_str = ", ".join(SOURCES["industry"])
        prompt = f"""
Search these industry sources this week: {sources_str}

Look for: M&A in hospitality tech, platform updates (OpenTable, Resy, SevenRooms, DoorDash, Uber Eats),
restaurant industry business news, funding rounds, policy changes affecting restaurants.

Find 2-3 items. {city_label_instruction}
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words, why it matters for ResX), url (direct article link if available), city.
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
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words), url (direct article link if available), city.
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
For each return: headline (punchy, include dish/collab name, max 8 words), detail (max 12 words including dates), so_what (max 10 words, social/content angle for ResX), url (direct link if available), city.
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
For each return: headline (max 8 words), detail (max 12 words), so_what (max 10 words on how it applies), url (direct link if available), city.
"""
    else:
        return []

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a sharp analyst writing for a small startup team. "
            "Be punchy and specific. Return only a valid JSON array of objects with keys: "
            "headline, detail, so_what, url, city. No markdown fences."
        ),
        max_tokens=1500,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        return data
    except Exception as e:
        print(f"Error parsing {section}: {e}")

    return []


# ---------------------------------------------------------------------------
# Step 4 — Format Slack blocks
# ---------------------------------------------------------------------------

CITY_TAG = {
    "NYC": "NYC",
    "LDN": "LDN",
    "BOTH": "",
}

def city_tag(item: dict) -> str:
    return CITY_TAG.get(item.get("city", "BOTH"), "")


def safe_link(url: str, label: str) -> str:
    """Return a Slack mrkdwn link, encoding chars that break the <url|label> format."""
    url = url.replace("&", "&amp;").replace("<", "").replace(">", "").replace("|", "%7C")
    label = label.replace("<", "").replace(">", "").replace("|", "-").replace("&", "&amp;")
    return f"<{url}|{label}>"


def safe_text(text: str, limit: int = 2950) -> str:
    """Truncate block text to Slack's 3000-char section limit."""
    return text[:limit] if len(text) > limit else text


def format_opening_item(item: dict) -> str:
    name = item.get("name", "")
    date = item.get("date", "")
    blurb = item.get("blurb", "")
    website = item.get("website", "")
    ig_handle = item.get("instagram_handle", "")
    ig_url = item.get("instagram_url", "")
    cover = item.get("cover_image_post", "")

    name_str = f"*{safe_link(website, name)}*" if website else f"*{name}*"
    if date:
        name_str += f"  _{date}_"
    if ig_handle and ig_url:
        name_str += f"  ·  {safe_link(ig_url, ig_handle)}"
    elif ig_handle:
        name_str += f"  ·  {ig_handle}"
    if cover:
        name_str += f"  ·  {safe_link(cover, 'ugc cover')}"

    lines = [name_str]
    if blurb:
        lines.append(blurb)

    return "\n".join(lines)


def format_news_items(items: list) -> str:
    lines = []
    for item in items:
        tag = city_tag(item)
        headline = item.get("headline", "")
        detail = item.get("detail", "")
        so_what = item.get("so_what", "")
        url = item.get("url", "")
        headline_str = f"*{safe_link(url, headline)}*" if url else f"*{headline}*"
        tag_str = f"  _{tag}_" if tag else ""
        lines.append(f"• {headline_str}{tag_str}\n  {detail} _{so_what}_")
    return "\n\n".join(lines)


def build_slack_blocks(
    date_str: str,
    nyc_data: dict,
    london_data: dict,
    watching: list,
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
        "text": {"type": "plain_text", "text": f"ResX Digest  ·  {date_str}"},
    })
    blocks.append({"type": "divider"})

    # ── 1. New Openings ─────────────────────────────────────────────────────
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*📍  NEW OPENINGS*"},
    })

    if nyc_data.get("items"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🗽  NYC*"},
        })
        for item in nyc_data["items"]:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_opening_item(item)},
            })

    if london_data.get("items"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"},
        })
        for item in london_data["items"]:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_opening_item(item)},
            })

    # UGC
    all_ugc = nyc_data.get("ugc", []) + london_data.get("ugc", [])
    if all_ugc:
        ugc_lines = "\n".join(
            f"  · {safe_link(u['url'], u['label'])}" for u in all_ugc[:6] if u.get("url")
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*UGC to repost:*\n{ugc_lines}"},
        })

    blocks.append({"type": "divider"})

    # ── 🔭 Watching ─────────────────────────────────────────────────────────
    if watching:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔭  WATCHING*"},
        })
        nyc_watch = [w for w in watching if w.get("city", "").upper() in ("NYC", "BOTH")]
        ldn_watch  = [w for w in watching if w.get("city", "").upper() in ("LDN", "BOTH")]
        if nyc_watch:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🗽  NYC*"},
            })
            for item in nyc_watch:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": format_opening_item(item)},
                })
        if ldn_watch:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"},
            })
            for item in ldn_watch:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": format_opening_item(item)},
                })
        blocks.append({"type": "divider"})

    # ── 2. Hospitality ──────────────────────────────────────────────────────
    if hospitality:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🌟  HOSPITALITY*\n\n{format_news_items(hospitality)}")},
        })
        blocks.append({"type": "divider"})

    # ── 3. Industry ─────────────────────────────────────────────────────────
    if industry:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🏢  INDUSTRY*\n\n{format_news_items(industry)}")},
        })
        blocks.append({"type": "divider"})

    # ── 4. Landscape ────────────────────────────────────────────────────────
    if landscape or new_competitors:
        landscape_text = format_news_items(landscape) if landscape else ""
        if new_competitors:
            comp_str = ", ".join(new_competitors)
            new_comp_block = f"\n\n*New competitor spotted:* {comp_str}"
            landscape_text = (landscape_text + new_comp_block).strip()
        if landscape_text:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": safe_text(f"*🗺️  LANDSCAPE*\n\n{landscape_text}")},
            })
            blocks.append({"type": "divider"})

    # ── 5. City Pulse ───────────────────────────────────────────────────────
    if city_pulse:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🏙️  CITY PULSE*\n\n{format_news_items(city_pulse)}")},
        })
        blocks.append({"type": "divider"})

    # ── 6. Specials & Collabs ───────────────────────────────────────────────
    if specials:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*✨  SPECIALS & COLLABS*\n\n{format_news_items(specials)}")},
        })
        blocks.append({"type": "divider"})

    # ── 7. AI & Tech ────────────────────────────────────────────────────────
    if ai_tech:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🤖  AI & TECH*\n\n{format_news_items(ai_tech)}")},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "ResX News Bot  ·  Powered by Claude  ·  Mon / Fri",
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
    watching = load_json(WATCHING_FILE, [])

    print("Refreshing competitor list...")
    competitors, new_competitors = refresh_competitors()

    print("Researching NYC openings...")
    nyc_result = research_openings("nyc", seen_openings, watching)

    print("Researching London openings...")
    london_result = research_openings("london", seen_openings, watching)

    nyc_data   = nyc_result.get("just_opened", {"items": [], "ugc": []})
    london_data = london_result.get("just_opened", {"items": [], "ugc": []})

    # Graduate watching items that have now opened
    opened_names = {
        i["name"].lower()
        for i in nyc_data.get("items", []) + london_data.get("items", [])
    }
    watching = [w for w in watching if w["name"].lower() not in opened_names]

    # Merge new coming_soon items (deduplicated by name)
    watching_names = {w["name"].lower() for w in watching}
    for item in nyc_result.get("coming_soon", []) + london_result.get("coming_soon", []):
        if item["name"].lower() not in watching_names:
            watching.append(item)
            watching_names.add(item["name"].lower())

    save_json(WATCHING_FILE, watching)

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
        watching=watching,
        hospitality=hospitality,
        industry=industry,
        landscape=landscape,
        city_pulse=city_pulse,
        specials=specials,
        ai_tech=ai_tech,
        new_competitors=new_competitors,
    )

    print(f"Posting to Slack... ({len(blocks)} blocks)")
    for i, b in enumerate(blocks):
        txt = b.get("text", {}).get("text", "")
        if txt:
            print(f"  block[{i}] len={len(txt)}: {txt[:80]!r}")
    post_to_slack(blocks)
    print("Done ✓")


if __name__ == "__main__":
    main()
