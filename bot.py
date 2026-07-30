"""
ResX News Bot
Runs 2x/week (Mon/Fri) via GitHub Actions, posts a Slack digest to #news.

Pipeline: research everything (openings + industry/competitor + city/culture + AI/product,
plus any manually pinned stories) into one flat pool of candidate stories, then a single
"editor" pass (edit_and_rank) merges duplicates, assigns each story to exactly one final
category, and ranks importance within category — before rendering.

Sections (in order):
  1. New Openings (NYC + London) — officially opened
  2. Watching — announced but not yet open; graduates to New Openings automatically
  3. Industry & Competitor Watch
  4. City & Culture
  5. AI & Product — every item includes an explicit "Why it matters" for ResX
"""

import os
import json
import re
import subprocess
import time
import urllib.request
import datetime
from pathlib import Path

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

SEEN_OPENINGS_FILE     = Path("data/seen_openings.json")
COMPETITORS_FILE       = Path("data/competitors.json")
WATCHING_FILE          = Path("data/watching.json")
SEEN_STORIES_FILE      = Path("data/seen_stories.json")
PINNED_INPUTS_FILE     = Path("data/pinned_inputs.json")
SKIPPED_ITEMS_LOG_FILE = Path("data/skipped_items_log.json")
LAST_POST_FILE         = Path("data/last_post.json")
RUN_LOG_FILE           = Path("data/run_log.json")

# Only bot.py runs git commands for itself when actually running in CI —
# never touch the caller's working tree during local/manual testing.
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"

CLAIM_STALE_MINUTES = 15  # a "running" claim older than this is treated as an abandoned/crashed run
MAX_GIT_RETRIES = 5

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
# Seed competitor list — actual competitor/platform names for the dedicated Competitor Watch
# (research_competitor_watch). Deliberately excludes pure editorial/media outlets like Eater,
# The Infatuation, and Hot Dinners — those are sources to research FROM (see SOURCES below),
# not competitors to track.
# ---------------------------------------------------------------------------
SEED_COMPETITORS = [
    "Dorsia", "Appointment Trader", "Cita Marketplace", "Access by Resy",
    "Blackbird", "Beli", "OpenTable", "Resy", "SevenRooms", "Tock",
    "Dojo", "Dinova", "Diibs", "Quenelle", "Table Agent",
    "DesignMyNight", "The Spot",
    "reservation scalper bots", "Telegram reservation groups",
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
        "Everything's Toasted newsletter latest",
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


# Basic web tools. web_search finds sources; web_fetch lets the model OPEN a source to confirm a
# UGC/Instagram link genuinely belongs to a specific restaurant (correctness, not just liveness).
# Deliberately the BASIC variants, not the _20260209 "dynamic filtering" ones — those run code
# execution under the hood, which burns the server tool-loop budget (pause_turn) and needs a
# container to resume (learned the hard way in social_bot.py). web_fetch is beta.
ANTHROPIC_BETA = "web-fetch-2025-09-10"
TOOLS = [
    {"type": "web_search_20250305", "name": "web_search"},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3, "max_content_tokens": 6000},
]
REQUEST_TIMEOUT = 300  # socket timeout — a hung request must fail gracefully, never hang the run


def call_anthropic(messages: list, system: str, max_tokens: int = 4096) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": TOOLS,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": ANTHROPIC_BETA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        # Timeout / connection drop / HTTP error / bad JSON: return "" so the caller degrades to
        # an empty section (its existing parse-failure path) instead of hanging or crashing.
        print(f"Anthropic request failed ({type(e).__name__}: {e}); returning empty for this call.")
        return ""

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


def check_broken(url: str, timeout: int = 5) -> bool:
    """Best-effort dead-link check for MANUALLY PINNED links specifically — deliberately more
    lenient than verify_url(). Only a definitive 404/410 counts as broken; network errors,
    timeouts, and blocks (very common on Instagram/TikTok for scripted requests — confirmed
    directly: a real, live Bake Magazine article once returned a 403 to a plain curl request)
    are treated as inconclusive, not broken. Georgia is vouching for a pinned link herself;
    the bar for rejecting it must be "confirmed dead", not "couldn't verify via HEAD request"."""
    if not url or not url.startswith(("http://", "https://")):
        return True
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (404, 410)
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except Exception:
        return False


# A UGC cover must be an Instagram PHOTO post — never a video (reel/tv), a profile, an editorial
# page, or a website. Slack unfurls whatever URL we attach, so the URL shape decides whether the
# reader sees a food photo or a video/nothing. This is a deterministic shape check; correctness
# ("belongs to THIS restaurant") can't be verified over HTTP, so the prompt + omit-when-unsure
# handle that separately.
_PHOTO_POST_RE = re.compile(r"^https?://(www\.)?instagram\.com/p/[\w-]+", re.I)
_IG_PROFILE_RE = re.compile(r"^https?://(www\.)?instagram\.com/[\w.]+/?(\?|$)", re.I)
_NOT_YET_OPEN_RE = re.compile(
    r"\b(coming soon|opens?\b|opening|to open|will open|set to|slated|later this|"
    r"next month|this fall|this autumn|this winter|this spring|this summer|"
    r"early \d{4}|late \d{4}|202[6-9])\b", re.I
)


def is_photo_post_url(url: str) -> bool:
    """True only for a real Instagram photo post (instagram.com/p/<id>). False for /reel/ or /tv/
    (video), a profile, TikTok, an editorial page, or a website."""
    return bool(url) and bool(_PHOTO_POST_RE.match(url.strip()))


def is_ig_profile_url(url: str) -> bool:
    """True for an instagram.com/<handle> profile URL (not a post/reel)."""
    if not url or not _IG_PROFILE_RE.match(url.strip()):
        return False
    # Exclude post/reel/tv paths which also start with instagram.com/
    return not re.match(r"^https?://(www\.)?instagram\.com/(p|reel|tv|explore)/", url.strip(), re.I)


def sanitize_opening_links(item: dict) -> dict:
    """Deterministically enforce the link rules on an opening/culture item, in place:
      - cover_image_post: must be an Instagram PHOTO post AND not a confirmed dead link, else clear
        it (kills the video/profile/broken-cover bugs). Instagram liveness uses the lenient
        check_broken (strict verify_url both 403-clears live IG content and 200-passes soft-404s).
      - instagram_url/handle: must be a real profile URL and not a confirmed dead link, else clear.
      - website/source_url: keep the strict verify_url (not Instagram, so the 200 check is valid).
    Correctness ("is this the right restaurant") is the model's job via the prompt + omit-when-unsure;
    this layer guarantees we never SHOW a video, a profile-as-cover, or a hard-dead link."""
    cover = (item.get("cover_image_post") or "").strip()
    if cover and not (is_photo_post_url(cover) and not check_broken(cover)):
        item["cover_image_post"] = ""

    ig_url = (item.get("instagram_url") or "").strip()
    if ig_url and not (is_ig_profile_url(ig_url) and not check_broken(ig_url)):
        item["instagram_url"] = ""
        item["instagram_handle"] = ""

    for key in ("website", "source_url"):
        if item.get(key) and not verify_url(item[key]):
            item[key] = ""
    return item


def looks_not_yet_open(item: dict) -> bool:
    """Deterministic backstop for the 'a coming-soon place showed up as a New Opening' bug: if the
    item's date/blurb clearly signals it isn't open yet, it belongs in Watching, not Just Opened."""
    text = f"{item.get('date', '')} {item.get('blurb', '')}".strip()
    return bool(_NOT_YET_OPEN_RE.search(text))


# ---------------------------------------------------------------------------
# Git + run-claim helpers
#
# The daily/scheduled-run guard only protects against duplicate posts if its
# state survives a `git push` — and pushes race whenever two runs (a delayed
# native schedule, a watchdog retrigger, a manual click) land close together.
# So instead of "post, then commit at the very end", we CLAIM the run slot
# with a commit+push BEFORE doing any expensive research, using git's
# fast-forward-only push as the atomic arbiter of who wins a race. The loser
# re-fetches, sees the winner's claim, and backs off before ever calling the
# Anthropic API or Slack.
# ---------------------------------------------------------------------------

def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_configure_identity():
    if not IN_CI:
        return
    _git("config", "user.name", "resx-digest-bot")
    _git("config", "user.email", "bot@resx.app")


def git_sync_to_remote_main():
    """Discard any local commit and reset to whatever origin/main actually has —
    safe here because this is an ephemeral CI checkout, never a local working tree."""
    _git("fetch", "origin", "main")
    _git("reset", "--hard", "origin/main")


def git_commit_and_push(paths: list, message: str) -> bool:
    """Commit the given paths and push, retrying on a losing race by resetting
    to origin and letting the caller recompute + retry. Returns True iff the
    push (or an empty no-op commit) landed."""
    if not IN_CI:
        return True  # local/manual runs never write to git; treat as a no-op success

    for attempt in range(1, MAX_GIT_RETRIES + 1):
        _git("add", *paths)
        diff = _git("diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return True  # nothing changed — already up to date, not a failure
        commit = _git("commit", "-m", message)
        if commit.returncode != 0:
            print(f"git commit failed (attempt {attempt}): {commit.stderr.strip()}")
            return False
        push = _git("push", "origin", "HEAD:main")
        if push.returncode == 0:
            return True
        print(f"git push lost the race (attempt {attempt}/{MAX_GIT_RETRIES}): {push.stderr.strip()}")
        git_sync_to_remote_main()
        time.sleep(1.5 * attempt)
    return False


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(ts: str):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def claim_todays_run(today_iso: str, trigger: str, run_id: str, force: bool) -> tuple[bool, str]:
    """
    Attempts to atomically claim today's run slot. Returns (claimed, reason).
    reason is only meaningful when claimed is False, for logging.
    """
    for attempt in range(1, MAX_GIT_RETRIES + 1):
        state = load_json(LAST_POST_FILE, {})
        if state.get("date") == today_iso:
            status = state.get("status", "completed")  # older files predate "status"; treat as completed
            if status == "completed" and not force:
                return False, f"already completed today at {state.get('posted_at', 'unknown time')}"
            if status == "running":
                started = _parse_iso(state.get("started_at", ""))
                stale = started is None or (_now_utc() - started) > datetime.timedelta(minutes=CLAIM_STALE_MINUTES)
                if not stale:
                    return False, f"another run claimed this slot at {state.get('started_at')} (trigger={state.get('trigger')}) and hasn't finished or timed out yet"
                print(f"Found a stale 'running' claim from {state.get('started_at')} — treating as abandoned and reclaiming")

        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "running", "run_id": run_id,
            "trigger": trigger, "started_at": _now_utc().isoformat(),
        })
        if git_commit_and_push([str(LAST_POST_FILE)], f"bot: claim {today_iso} run ({trigger}) [skip ci]"):
            return True, ""
        print(f"Lost the claim race, retrying ({attempt}/{MAX_GIT_RETRIES})...")

    return False, "could not win the claim race after retries"


def log_run_event(today_iso: str, trigger: str, outcome: str, detail: str = ""):
    """Best-effort structured history: data/run_log.json, 30-day rolling window."""
    log = load_json(RUN_LOG_FILE, [])
    log.append({
        "date": today_iso, "timestamp": _now_utc().isoformat(),
        "trigger": trigger, "outcome": outcome, "detail": detail,
    })
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    log = [e for e in log if e.get("date", "") >= cutoff]
    save_json(RUN_LOG_FILE, log)
    git_commit_and_push([str(RUN_LOG_FILE)], f"bot: log {outcome} run [skip ci]")


# ---------------------------------------------------------------------------
# Holiday & food-day calendar
# (month, day, name, special_header_template, prompt_hint, is_food_day)
# special_header_template=None for food days; July 4 header is computed dynamically.
# ---------------------------------------------------------------------------

CALENDAR = [
    # Food/drink days and one-off events are NOT here — they live in the shared
    # data/calendar_moments.json (read by both bots). This list is holidays/observances only,
    # where a special_header may apply.
    (1,  1,  "New Year's Day",          "🥂 New Year's Edition",    "New Year's is tomorrow — look for NYE dining, top tables for the year, and resolution menus",                       False),
    (2, 14,  "Valentine's Day",          "❤️ Valentine's Edition",   "Valentine's Day is coming — look for romantic dining, prix-fixe specials, and date-night spots",                     False),
    (7,  4,  "Fourth of July",           "🇺🇸 Special Edition",      "Fourth of July is days away — look for patriotic dining content, summer entertaining, and July 4th specials in NYC", False),
    (8, 25,  "UK Summer Bank Holiday",   None,                       "UK Bank Holiday weekend — London pop-ups, long-weekend dining, and things to do",                                    False),
    (10, 31, "Halloween",                "🎃 Halloween Edition",     "Halloween is days away — spooky dining events, themed menus, Halloween pop-ups and collabs",                          False),
    (11,  5, "Guy Fawkes Night",         None,                       "Guy Fawkes Night is days away — London fireworks dining, bonfire night restaurant events",                            False),
    (12, 25, "Christmas",                "🎄 Holiday Edition",       "Christmas is approaching — festive menus, holiday dining, Christmas party venues in NYC and London",                  False),
    (12, 26, "Boxing Day",               None,                       "Boxing Day is coming — post-Christmas London dining, Boxing Day brunch spots",                                        False),
]


# Cultural moments live in a SHARED data file (data/calendar_moments.json) read by BOTH bots, so a
# moment added once shows up in the digest AND the social bot. Shared as DATA, not code — bot.py and
# social_bot.py stay independent modules with no cross-imports. Three kinds:
#   fixed:   recurring food/drink days on a fixed month/day
#   movable: recurring food days on the Nth weekday of a month (e.g. 1st Friday of June)
#   moments: one-off dated events/releases (US Open, a festival, a premiere) — year-specific
# A hint only PRIMES research toward that moment; it never forces content, and the relevance filter
# still gates what posts. Both bots ALSO surface these prominently at the top so nobody misses them.
CALENDAR_MOMENTS_FILE = Path("data/calendar_moments.json")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """Date of the nth <weekday> in a month (weekday: Mon=0..Sun=6) — for days that fall on
    e.g. the 'first Friday of June' rather than a fixed calendar date."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def upcoming_calendar_moments(today: datetime.date, window: int = 7) -> list:
    """Every cultural moment within `window` days (or currently ongoing, for multi-day events),
    soonest first. Returns dicts: {name, hint, days_until} (days_until clamped to 0 for
    today/ongoing). Drives both the research hint-injection AND the top-of-message callouts."""
    data = load_json(CALENDAR_MOMENTS_FILE, {}) or {}
    out = []
    for e in data.get("fixed", []) or []:
        try:
            day = datetime.date(today.year, int(e["month"]), int(e["day"]))
        except (KeyError, TypeError, ValueError):
            continue
        du = (day - today).days
        if 0 <= du <= window:
            out.append({"name": e.get("name", ""), "hint": e.get("hint", ""), "days_until": du})
    for e in data.get("movable", []) or []:
        try:
            day = _nth_weekday(today.year, int(e["month"]), int(e["weekday"]), int(e["nth"]))
        except (KeyError, TypeError, ValueError):
            continue
        du = (day - today).days
        if 0 <= du <= window:
            out.append({"name": e.get("name", ""), "hint": e.get("hint", ""), "days_until": du})
    for e in data.get("moments", []) or []:
        try:
            start = datetime.date.fromisoformat(e["date"])
            end = datetime.date.fromisoformat(e["end_date"]) if e.get("end_date") else start
        except (KeyError, TypeError, ValueError):
            continue
        du = (start - today).days
        if du <= window and today <= end:  # upcoming within window, or ongoing
            out.append({"name": e.get("name", ""), "hint": e.get("hint", ""), "days_until": max(du, 0)})
    out.sort(key=lambda m: m["days_until"])
    return [m for m in out if m["name"] or m["hint"]]


def get_holiday_context(today: datetime.date) -> dict:
    """
    Returns {"special_header": str|None, "prompt_hints": [str]} for any holiday or food day
    within 10 days (or 7 days for food days). special_header is only set when a major holiday
    is within 3 days.
    """
    hints = []
    special_header = None

    for month, day, name, header_tpl, hint, is_food_day in CALENDAR:
        try:
            holiday = datetime.date(today.year, month, day)
        except ValueError:
            continue
        days_until = (holiday - today).days
        window = 7 if is_food_day else 10
        if 0 <= days_until <= window:
            hints.append(hint)
            if not is_food_day and days_until <= 3 and header_tpl and not special_header:
                if month == 7 and day == 4:
                    years = today.year - 1776
                    special_header = f"🇺🇸 Special Edition — America's {years}th Birthday"
                else:
                    special_header = header_tpl

    # Cultural moments from the shared calendar (food days + events) — 7-day window, hint only.
    hints.extend(m["hint"] for m in upcoming_calendar_moments(today) if m["hint"])

    # Month-long observances
    if today.month == 6:
        hints.append("It's Pride Month — look for LGBTQ+ dining moments, pride events at restaurants, rainbow menus and collabs")
    if today.month == 1:
        hints.append("It's Dry January and Veganuary — look for 0% cocktails, mocktail menus, and vegan restaurant moments")

    return {"special_header": special_header, "prompt_hints": hints}


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
# Stable identity — fixes restaurants silently duplicating across New Openings / Watching
# ---------------------------------------------------------------------------

def normalize_identity(name: str, city: str) -> str:
    """Stable dedup key for a restaurant/venue: lowercase, strip punctuation, drop common
    city/article suffixes (the exact drift pattern that caused "Dishoom" vs "Dishoom NYC" to
    silently fail to match). Deliberately does NOT strip parenthetical qualifiers like
    "(Williamsburg)" — that's left to the edit_and_rank LLM pass, which can tell a dropped
    disambiguator from two genuinely distinct locations of an expanding chain; a plain string
    function can't safely make that call."""
    n = re.sub(r"[''`\".,!]", "", (name or "").lower())
    n = re.sub(r"\b(nyc|ny|london|the)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    city_key = (city or "").strip().upper()
    if city_key not in ("NYC", "LDN"):
        city_key = "BOTH"
    return f"{n}::{city_key}"


# ---------------------------------------------------------------------------
# Step 2 — Research openings
# ---------------------------------------------------------------------------

def research_openings(city: str, seen: set, watching: list) -> dict:
    """
    Returns:
      {
        "just_opened": {"items": [...]},
        "coming_soon": [...]
      }
    Every item is tagged with "category" ("new_opening" or "watching") and a stable "id"
    (see normalize_identity) at ingestion, so downstream code never has to re-derive identity
    from a possibly-drifted name string.
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
{_source_priority_instruction()}

As a vibe calibration, the target audience is similar to followers of these accounts
(use as signal only, do NOT cite them): {', '.join(signal_accounts)}

Currently watching (announced but not yet open as of last run): {watching_str}
If any of these have now opened, include them in JUST OPENED.

Return TWO lists:

1. JUST OPENED: Up to 3 restaurants VERIFIABLY OPEN NOW — backed by concrete evidence it's actually
   operating this week (diners posting from inside, a published review, or confirmed taking
   reservations/walk-ins TODAY). An announcement, a teaser, or a future "opens [date]" is NOT open —
   those go in COMING SOON. When in doubt, it's COMING SOON.
   EXCLUDE already seen: {seen_str}
   Also exclude these restaurants which are tracked separately on the watching list — do NOT independently discover them as new openings. They will only appear if you are graduating them from the watching list above: {watching_str}

2. COMING SOON: Up to 3 noteworthy restaurants announced for an upcoming opening (not yet open).
   These will be tracked week-to-week until they open.

For each restaurant in BOTH lists return:
- name: restaurant name
- date: opening date (e.g. "June 18") or "opens [date]" for coming soon
- blurb: 1 sentence, max 12 words — factual: what it is, the concept, the notable dish/format.
  State facts, not hype. No filler ("everyone's talking about", "the it-spot", "buzzy energy").
- city: "{city_key}"
- website: the official website — try "[name].com", "[name].co.uk", "site:[name] official". Only if the URL resolves; blank otherwise.
- instagram_handle / instagram_url: the restaurant's OWN Instagram profile. Only include a handle/URL
  you actually FOUND in a search result or a page you fetched — never spell one out from the name or
  guess the spelling. If you can't confirm the exact real profile, leave BOTH blank. A wrong or dead
  IG link is worse than none.
- cover_image_post: OPTIONAL — include ONLY if you can fully confirm it, otherwise OMIT it. It must be
  an Instagram PHOTO post (instagram.com/p/…) — NEVER a video/reel — showing the FOOD at THIS exact
  restaurant, from a diner or food creator (NOT the restaurant's own account, NOT editorial like
  Eater / The Infatuation / Time Out / Hot Dinners). To confirm it's really this restaurant,
  web_fetch the post or article and check the caption/venue tag before using it. Never guess or
  construct the URL; never reuse a URL across restaurants.
  ❌ wrong: attaching a @bysaison food post as the cover for Bark BBQ because it looked close.
  ✅ right: a diner's instagram.com/p/… clearly tagged at / captioned about Bark BBQ.
  If you cannot confirm ALL of the above, OMIT cover_image_post — a missing cover is fine; a wrong
  one (wrong restaurant, or a video) is unacceptable.

For COMING SOON items also return:
- source_url: URL to the article or announcement that confirms this opening and its date — required. Only include if you've confirmed it resolves.

Return ONLY valid JSON:
{{
  "just_opened": {{
    "items": [
      {{
        "name": "...", "date": "...", "blurb": "...", "city": "...",
        "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "..."
      }}
    ]
  }},
  "coming_soon": [
    {{
      "name": "...", "date": "...", "blurb": "...", "city": "...",
      "website": "...", "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "...", "source_url": "..."
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
        just_opened_items = data.get("just_opened", {}).get("items", [])
        coming_soon_items = data.get("coming_soon", [])
        # Deterministic link hygiene: photo-only covers, lenient IG liveness, strict website/source.
        for item in just_opened_items + coming_soon_items:
            sanitize_opening_links(item)

        # A "just opened" that clearly isn't open yet belongs in Watching, not New Openings.
        still_open, reclassified = [], []
        for item in just_opened_items:
            (reclassified if looks_not_yet_open(item) else still_open).append(item)
        just_opened_items = still_open
        coming_soon_items = reclassified + coming_soon_items

        for item in just_opened_items:
            item["category"] = "new_opening"
            item["id"] = normalize_identity(item.get("name", ""), item.get("city", ""))
        for item in coming_soon_items:
            item["category"] = "watching"
            item["id"] = normalize_identity(item.get("name", ""), item.get("city", ""))
        return {"just_opened": {"items": just_opened_items}, "coming_soon": coming_soon_items}
    except Exception as e:
        print(f"Error parsing openings for {city}: {e}")

    return {"just_opened": {"items": []}, "coming_soon": []}


# ---------------------------------------------------------------------------
# Step 3 — Research news sections
# ---------------------------------------------------------------------------

def _city_label_instruction() -> str:
    return "For each item, also include a 'city' field: either 'NYC', 'LDN', or 'BOTH'."


def _source_priority_instruction() -> str:
    """Optimize for signal, not completeness — the digest's job is to make sure nobody says
    "wait...how did we miss that?" A newsletter roundup that only cites editorial coverage
    misses things that are already everywhere on restaurant/brand/creator accounts days before
    any outlet writes them up. Search in roughly this priority order:"""
    return """
SOURCE PRIORITY — discovery order (most important first):
1. Restaurant accounts (the venue's own official account)
2. Hotel accounts
3. Hospitality brand accounts (drink/food brands doing collabs, activations, product drops)
4. Food creator accounts (food bloggers, food TikTok/IG creators covering the scene)
5. Hospitality creator accounts (industry insiders, behind-the-scenes hospitality creators)
6. Restaurant group accounts (the parent group/company behind multiple venues)
7. Hospitality newsletters (Feed Me, Emily Sundberg, etc.)
8. Industry publications (Eater, The Infatuation, Time Out, etc.)
9. General news

Actively search for what restaurant, hotel, brand, and creator accounts are ALREADY posting
about and discussing — a collaboration or product drop that's blowing up on social but hasn't
been written up by a publication yet is EXACTLY what you should surface, not skip. Do not wait
for editorial coverage to confirm something is real — if the restaurant/brand account itself
posted it, or a food/hospitality creator is already covering it, that is a real, valid, citable
source on its own."""


def _executive_relevance_instruction() -> str:
    """This is not a news summary — it's an executive briefing. Applied per-item at research
    time (self-filtering at the source) as well as again in edit_and_rank (a second pass with
    visibility across the whole pool)."""
    return """
EXECUTIVE RELEVANCE. This is not a news summary — it's an executive briefing for the ResX
team. Every item must answer: why should the ResX team care? That can be strategic,
operational, cultural, competitive, or product-related. If something is interesting but
wouldn't change what the team would actually discuss internally that week, leave it out —
do not include it just because it happened."""


def _build_exclude_instruction(exclude: list) -> str:
    """Stories already surfaced by another research call THIS run — sequential, same-run dedup."""
    if not exclude:
        return ""
    already_used = "\n".join(
        f"- {item.get('headline', '')} ({item.get('url', 'no url')})"
        for item in exclude
        if item.get("headline")
    )
    return (
        f"\n\nIMPORTANT: The following stories have already appeared earlier in this digest — "
        f"do NOT include them or any article covering the same news:\n{already_used}\n"
        f"Find different stories that do not duplicate any of the above."
    )


def _build_seen_instruction(seen_stories: list) -> str:
    """Stories covered in recent past digests — cross-run dedup, allows a materially new
    development to resurface a subject rather than blocking it outright."""
    if not seen_stories:
        return ""
    covered_str = "\n".join(
        f"- {s.get('headline', '')} — {s.get('detail', '')} ({s.get('so_what', '')})"
        for s in seen_stories[:60]
    )
    return (
        f"\n\nIMPORTANT — already covered in recent digests, do not just re-report these:\n"
        f"{covered_str}\n\n"
        f"For each one: only cover the same underlying subject again if there is a genuinely "
        f"MATERIAL new development since — e.g. a restaurant that was 'coming soon' has now "
        f"officially opened or confirmed its date, a funding rumor is now an official confirmed "
        f"round, a launch expands to a new city, a new partnership is announced, or a teased "
        f"date is now confirmed. Do NOT re-cover the same announcement reworded, the same menu "
        f"item, or the same opening with no new facts — that is a duplicate even if the headline "
        f"or source differs. If you do include a follow-up to something above, make the new "
        f"development explicit in 'detail' or 'so_what' (e.g. 'now confirmed for Sept 12', not "
        f"a repeat of the original teaser)."
    )


def _run_news_research(prompt: str, label: str, exclude: list, seen_stories: list,
                        holiday_hint: str, max_tokens: int = 1600) -> list:
    """Shared plumbing for the news-style research calls (industry/culture/ai_product):
    assembles the holiday/exclude/seen-story instructions, calls Claude, and parses the
    returned JSON array."""
    if holiday_hint:
        prompt = f"CONTEXT FOR THIS RUN: {holiday_hint}\n\n" + prompt.lstrip()
    exclude_instruction = _build_exclude_instruction(exclude)
    if exclude_instruction:
        prompt = prompt.rstrip() + exclude_instruction
    seen_instruction = _build_seen_instruction(seen_stories)
    if seen_instruction:
        prompt = prompt.rstrip() + seen_instruction

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a sharp editor writing for a small startup team. "
            "Be factual and specific — no hype, no marketing language, no AI-sounding phrases. "
            "Only surface articles published within the last 7 days; do not include content "
            "from previous months or years. "
            "Return only a valid JSON array of objects. No markdown fences."
        ),
        max_tokens=max_tokens,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error parsing {label}: {e}")
        return []


def research_competitor_watch(competitors: list = None, seen_stories: list = None,
                               holiday_hint: str = None) -> list:
    """Dedicated Competitor Watch — a mandatory per-name check, not a generic industry-news
    scan. Returns list of dicts: {headline, detail, so_what, url, city}, each tagged
    is_competitor_watch=True so the renderer can give it its own sub-heading."""
    comp_str = ", ".join(competitors or SEED_COMPETITORS)

    prompt = f"""
You are running ResX's dedicated Competitor Watch. This is a mandatory check on named
competitors, not a general industry news scan — check EACH of the following companies by name
for news from the past week: {comp_str}

For each one, specifically check whether it has:
- Launched a PRODUCT or PLATFORM — a new app, feature, AI tool, or operating system. Weight this
  heavily: product/AI launches are the easiest to miss because they're announced on the company's
  OWN channels or niche deal/tech accounts, not restaurant press. (E.g. a reservation competitor
  unveiling an "AI-powered operating system" is squarely this section's job.)
- Expanded to a new city
- Raised funding
- Been acquired, or acquired someone else
- Announced a partnership
- Made an executive hire (CEO/CTO/Head of X, etc.)
- Made a major strategy change (pivot, repositioning, new business model)

HOW TO LOOK — don't rely on restaurant editorial; it won't cover most of these:
- Search each competitor's name with terms like "launch", "AI", "platform", "new product",
  "operating system", "raises", "acquires", "partners with".
- Check the company's OWN announcements (site/blog, LinkedIn, Instagram, X, Facebook) and
  deal / hospitality-tech trackers (e.g. Traded / TradedVC, Skift, PhocusWire). A company's own
  post announcing something IS a real, citable source.
- web_fetch a promising announcement to confirm the specific facts before including it.

If a competitor made a real, confirmed announcement in ANY of these categories in the past
week, it MUST be included — do not skip or omit a real development just to keep the list
short. This is the one section where completeness on the named list matters more than
brevity; if you find 6 genuine competitor developments, return all 6. If you find nothing
concrete for a given competitor, simply don't include them — never pad with speculation or
generic "no news" filler.

{_city_label_instruction()}
For each return: headline (max 8 words, name the competitor), detail (max 15 words — the
specific development, factual, no hype), url (direct link if available), city.
"""
    items = _run_news_research(prompt, "competitor_watch", None, seen_stories, holiday_hint)
    for item in items:
        item["is_competitor_watch"] = True
    return items


def research_industry(exclude: list = None, seen_stories: list = None, holiday_hint: str = None) -> list:
    """Industry & Competitor Watch — broader industry/business/regulatory news NOT tied to a
    specific named competitor (see research_competitor_watch for the dedicated per-name check).
    Returns list of dicts: {headline, detail, so_what, url, city}"""
    sources_str = ", ".join(SOURCES["industry"])

    prompt = f"""
Search these industry sources for the past week: {sources_str}
{_executive_relevance_instruction()}

Look for:
- Restaurant reservation regulation news, reservation bot crackdowns, new reservation-adjacent
  features from Google/Apple Maps, broader dining trend shifts
- Restaurant industry business news, funding rounds, or policy changes NOT already tied to a
  specific named competitor (those are covered by Competitor Watch — don't duplicate them here)
- Notable restaurant/chef business moves — closures, chef departures/hires, brand
  partnerships, acquisitions (the business angle, not the cultural gossip angle — that
  belongs in City & Culture)

Find 2-4 most relevant items. {_city_label_instruction()}
For each return: headline (max 8 words), detail (max 12 words — factual, what happened, no hype),
url (direct article link if available),
city.
"""
    return _run_news_research(prompt, "industry", exclude, seen_stories, holiday_hint)


def research_culture(exclude: list = None, seen_stories: list = None, holiday_hint: str = None) -> list:
    """City & Culture. Returns list of dicts: {headline, detail, so_what, url, city}"""
    nyc_sources = ", ".join(SOURCES["city_pulse_nyc"])
    ldn_sources = ", ".join(SOURCES["city_pulse_london"])
    specials_sources = ", ".join(SOURCES["specials"])
    hospitality_sources = ", ".join(SOURCES["hospitality"])
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)

    prompt = f"""
You are finding city culture moments from the past week for a 25-35 going-out audience
in NYC and London.

NYC sources: {nyc_sources}
London sources: {ldn_sources}
Specials/collab sources: {specials_sources}
Insider hospitality sources: {hospitality_sources}
{_source_priority_instruction()}
{_executive_relevance_instruction()}

Signal accounts (use as vibe calibration, do NOT cite directly): {signal}

Your job is not to summarize hospitality news — it's to make sure nobody on the team ever says
"wait...how did we miss that?" If a food/hospitality brand collab, product drop, or celebrity
moment is already all over restaurant/creator accounts, it belongs here even if no publication
has written it up yet.

Look for:
- Cultural trends: what the city is obsessed with, experiences people are seeking out
- Social moments driving people to make plans
- Big cultural anchors landing this week — the START of a major event (a tournament like the
  US Open, a festival, fashion week), a hyped movie release or TV season finale — with the
  dining / going-out angle ResX could build around it
- Celebrity or cultural figure spotted at a restaurant — the gossip-meets-dining crossover
  (e.g. "Sabrina Carpenter caught at Emmets on Grove" — this kind of micro-moment is gold)
- Brand × food collabs going viral on social media (e.g. a yogurt brand doing a froyo pop-up,
  a bakery's new limited drink, a froyo topping collab with a bakery/creator brand) — check what
  the venue's OWN account and food creators are already posting, not just what's been covered
- Named chef x restaurant collabs with a specific dish (e.g. "Chef X x Restaurant Y = The
  [Dish Name]"), limited-time/seasonal menu items with a story behind them, pop-up residencies
  with a clear end date and specific menu
- Insider hospitality gossip and cultural moments — chef moves as a CULTURAL story (not a
  business one — that belongs in Industry & Competitor Watch), food-media buzz, brand x
  restaurant crossover moments
- NOT generic events listings, NOT general prix-fixe deals/restaurant week/generic seasonal
  menus without a story

Be specific — name the place, the person, the detail. Think insider knowledge, not trend
think-pieces. Think: "Vesper is averaging 1.5 martinis per guest since opening" or "Waiters at
Osteria Vibrato carry Tide Pens in their pockets." Real facts and named details beat vague
observations every time.

Find 4-5 items across NYC and London. {_city_label_instruction()}
For each return: headline (punchy, max 8 words), detail (max 12 words — factual, what happened,
no hype — no "buzzy energy", no "everyone's talking about"), url (direct link if available), city.
"""
    return _run_news_research(prompt, "culture", exclude, seen_stories, holiday_hint)


def research_ai_product(exclude: list = None, seen_stories: list = None, holiday_hint: str = None) -> list:
    """AI & Product. Returns list of dicts: {headline, detail, why_it_matters, url, city}"""
    sources_str = ", ".join(SOURCES["ai_tech"])

    prompt = f"""
Search these sources for the past week: {sources_str}

Look for big AI news AND practical implications for a small software startup — this must NOT
become a generic AI newsletter roundup. Candidates: model releases from Anthropic/OpenAI/Google,
new agent/automation tooling, developer tools relevant to a React Native + Node/TypeScript +
Firebase stack, AI features shipping in tools we already use.

For EVERY item, before including it, answer explicitly: does this matter to ResX because it
could (a) lower our costs, (b) be worth experimenting with, or (c) improve how the team already
works? If you can't answer at least one of those concretely, do NOT include the item — generic
"AI is advancing" news with no team-relevant angle does not belong here.

Find 2-3 items — quality and relevance over volume. City field should always be 'BOTH'.
For each return: headline (max 8 words), detail (max 12 words, what happened), why_it_matters
(REQUIRED, max 15 words — one concrete, actionable takeaway for ResX: what to try, what it
saves, or what to change; not "this could be useful" — name the specific action or saving),
url (direct link if available), city.
"""
    return _run_news_research(prompt, "ai_product", exclude, seen_stories, holiday_hint)


# ---------------------------------------------------------------------------
# Step 2.5 — Pinned inputs: Georgia's manually-submitted leads
#
# Anything manually submitted (a link, a name, a description) is high-priority and must
# never be silently ignored. research_pinned_inputs does the extraction/categorization work
# so Georgia only has to hand over a raw lead, not a pre-written digest entry — this is the
# actual fix for pinned content getting dropped: there was previously no path for "just a
# link" input at all, only for fully-authored story objects.
# ---------------------------------------------------------------------------

def research_pinned_inputs(raw_leads: list, seen_openings: set, watching_context: list,
                            recent_stories: list, holiday_hint: str = None) -> dict:
    """Researches each manually-submitted raw lead (a URL or short description, optionally
    with a hint) via web_search, categorizes it using the same rules as edit_and_rank, and
    extracts full story fields. Returns {"resolved": [...], "rejected": [...]}."""
    if not raw_leads:
        return {"resolved": [], "rejected": []}

    leads_str = "\n".join(
        f'- input: "{lead.get("input", "")}"' + (f'  (hint: {lead["hint"]})' if lead.get("hint") else "")
        for lead in raw_leads
    )
    seen_openings_str = ", ".join(seen_openings) if seen_openings else "none"
    watching_str = ", ".join(w.get("name", "") for w in watching_context) if watching_context else "none"
    covered_str = (
        "\n".join(f"- {s.get('headline', '')} — {s.get('detail', '')}" for s in (recent_stories or [])[:60])
        or "none"
    )

    prompt = f"""
Georgia has manually submitted these leads. They are HIGH-PRIORITY and must never be silently
ignored — research each one and extract a full digest story from it.

Leads to resolve:
{leads_str}

Already-featured restaurant names (permanent list — a match here is a duplicate):
{seen_openings_str}

Currently tracked as "watching" (not yet open):
{watching_str}

Already covered in recent digests (a match here is a duplicate unless materially new):
{covered_str}

For each lead:
1. Investigate it. If it's a link (Instagram/TikTok/X/article), figure out what it actually
   shows or says. If it's a short description (a restaurant name, a competitor, a
   collaboration), search for it to confirm the real facts.
2. Categorize it using these rules (identical to the rest of the digest):
   - Officially opened restaurant/hotel/bakery/bar/members club -> new_opening
   - Announced but not yet open -> watching
   - Competitor/reservation platform/hospitality tech news -> industry
   - Celebrity/viral/collab/cultural moment -> culture
   - AI/product news -> ai_product
3. Extract the real facts. For new_opening/watching: name, date, blurb, city, and website/
   instagram_handle/instagram_url/cover_image_post if findable. For industry/culture/
   ai_product: headline, detail, so_what, city.
4. CRITICAL — PRESERVE THE ORIGINAL LINK. If Georgia's input was a URL, your output's primary
   link field (website/source_url for openings, url for news items) MUST be that EXACT URL,
   character for character. Never substitute a different link you find while researching,
   even one you think is a "better" source — the whole point is that she already chose it.
5. Keep blurb (openings) and detail (news) factual and specific — what it is / what happened —
   no hype or vibe commentary. For ai_product ONLY, so_what states the concrete reason it matters
   (lowers costs / worth testing / improves the team's workflow). For a cover_image_post on an
   opening: only include an Instagram PHOTO post (instagram.com/p/…, never a video/reel) you've
   confirmed shows THIS restaurant's food; if you can't confirm it, omit it.

Only reject a lead if:
- broken: you genuinely cannot find any real content behind it — a dead link, nothing exists
- duplicate: it matches an already-featured opening, a currently-tracked watching item, or a
  recently-covered story above, with no materially new development
- clearly_irrelevant: it has nothing to do with restaurants, hospitality, dining, or ResX's
  business at all

Do NOT reject a lead just because it seems minor, or because you personally wouldn't have
picked it — Georgia already decided it's worth including by sending it. When genuinely in
doubt, resolve it, don't reject it.

Return ONLY valid JSON, no markdown:
{{
  "resolved": [
    {{
      "input": "... (exact copy of the original input, for matching)",
      "category": "new_opening|watching|industry|culture|ai_product",
      "name": "...", "date": "...", "blurb": "...", "website": "...",
      "instagram_handle": "...", "instagram_url": "...", "cover_image_post": "...",
      "source_url": "...",
      "headline": "...", "detail": "...", "so_what": "...", "url": "...", "city": "..."
    }}
  ],
  "rejected": [
    {{"input": "... (exact copy, for matching)", "reason": "broken|duplicate|clearly_irrelevant", "detail": "..."}}
  ]
}}
Only include the fields relevant to the assigned category — name/date/blurb/website/
instagram_handle/instagram_url/cover_image_post/source_url for new_opening/watching;
headline/detail/so_what/url for industry/culture/ai_product. Always include "input",
"category", and "city".
"""
    if holiday_hint:
        prompt = f"CONTEXT FOR THIS RUN: {holiday_hint}\n\n" + prompt.lstrip()

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a meticulous researcher acting on a colleague's direct, high-priority "
            "requests. Never invent facts. Never substitute a different link than the one "
            "given. Return only valid JSON, no markdown."
        ),
        max_tokens=4096,
    )

    try:
        clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if not isinstance(data, dict):
            return {"resolved": [], "rejected": []}
        return {"resolved": data.get("resolved", []) or [], "rejected": data.get("rejected", []) or []}
    except Exception as e:
        print(f"Error parsing pinned inputs research: {e}")
        return {"resolved": [], "rejected": []}


def _is_pre_resolved_pin(entry: dict) -> bool:
    """A pin that already carries full digest-ready content (category + headline/name) —
    e.g. one already hand-verified — skips the research call entirely rather than risk an
    LLM subtly rewriting content that was already confirmed correct."""
    return bool(entry.get("category")) and bool(entry.get("headline") or entry.get("name"))


def _pin_input_key(entry: dict) -> str:
    """The matching key for a pinned_inputs.json entry — used consistently everywhere an
    entry needs to be identified, so the safety net never mismatches a pre-resolved pin
    (which may have no "input" field) against what was actually resolved/rejected."""
    return entry.get("input") or entry.get("url") or entry.get("website") or entry.get("source_url", "")


def _finalize_pinned_story(item: dict, input_text: str, seen_openings: set,
                            watching_context: list, today_iso: str) -> tuple:
    """Applies the deterministic broken-link/duplicate backstop shared by both the
    pre-resolved and freshly-researched pinned-input paths. Returns (story_or_None, skip_or_None)."""
    link_field = "website" if item.get("category") in ("new_opening", "watching") else "url"
    link = item.get(link_field, "") or item.get("source_url", "")
    if link and check_broken(link):
        return None, {
            "date": today_iso, "input": input_text, "reason": "broken_link",
            "detail": f"{link_field}={link!r} did not resolve", "url": link,
        }

    if item.get("category") in ("new_opening", "watching"):
        ident = normalize_identity(item.get("name", ""), item.get("city", ""))
        if item.get("category") == "new_opening" and item.get("name", "") in seen_openings:
            return None, {
                "date": today_iso, "input": input_text, "reason": "duplicate",
                "detail": f"'{item.get('name')}' is already in seen_openings", "url": link,
            }
        watching_ids = {
            w.get("id", normalize_identity(w.get("name", ""), w.get("city", "")))
            for w in watching_context
        }
        if item.get("category") == "watching" and ident in watching_ids:
            return None, {
                "date": today_iso, "input": input_text, "reason": "duplicate",
                "detail": f"'{item.get('name')}' is already tracked in watching", "url": link,
            }

    item["origin"] = "pinned"
    item["id"] = link or f"pinned::{item.get('headline') or item.get('name', '')}"
    return item, None


def process_pinned_inputs(pinned_inputs: list, seen_openings: set, watching_context: list,
                           recent_stories: list, today_iso: str, holiday_hint: str = None) -> tuple:
    """Orchestrates pinned-input resolution with a safety net: every single entry in
    pinned_inputs.json ends up either a resolved story or a logged skip — never silently
    dropped, even if the model forgets one or a link turns out to be dead/duplicate."""
    resolved_stories = []
    skip_entries = []
    matched_inputs = set()

    already_resolved = [e for e in pinned_inputs if _is_pre_resolved_pin(e)]
    raw_leads = [e for e in pinned_inputs if not _is_pre_resolved_pin(e)]

    for entry in already_resolved:
        input_text = _pin_input_key(entry)
        matched_inputs.add(input_text)
        story, skip = _finalize_pinned_story(dict(entry), input_text, seen_openings, watching_context, today_iso)
        (resolved_stories.append(story) if story else skip_entries.append(skip))

    result = research_pinned_inputs(raw_leads, seen_openings, watching_context, recent_stories, holiday_hint)

    for item in result["resolved"]:
        input_text = item.get("input", "")
        matched_inputs.add(input_text)
        story, skip = _finalize_pinned_story(item, input_text, seen_openings, watching_context, today_iso)
        (resolved_stories.append(story) if story else skip_entries.append(skip))

    for rej in result["rejected"]:
        input_text = rej.get("input", "")
        matched_inputs.add(input_text)
        skip_entries.append({
            "date": today_iso, "input": input_text,
            "reason": rej.get("reason", "clearly_irrelevant"), "detail": rej.get("detail", ""),
        })

    # Safety net — never silently drop a pinned input the model didn't address
    for lead in pinned_inputs:
        lead_input = _pin_input_key(lead)
        if lead_input not in matched_inputs:
            skip_entries.append({
                "date": today_iso, "input": lead_input, "reason": "not_addressed_by_model",
                "detail": "the research pass did not return a resolution or rejection for this input",
            })

    return resolved_stories, skip_entries


# ---------------------------------------------------------------------------
# Step 3.5 — Flatten, merge/dedup, categorize, and rank into one story pool
#
# This is the piece that actually fixes stories duplicating across sections: no individual
# research call above has visibility into what any other call found, so a restaurant opening
# could surface independently via research_openings AND research_industry with zero
# cross-awareness. edit_and_rank sees the whole pool at once and is the single place a
# duplicate gets caught and collapsed.
# ---------------------------------------------------------------------------

def normalize_stories(new_opening_items: list, watching_candidate_items: list,
                      industry_items: list, culture_items: list, ai_items: list) -> list:
    """Flattens every research call's output into one pool. Openings/watching items already
    carry category+id from research_openings; this fills in the same fields for the three
    news-style lists, whose id is the source url (or a normalized-title fallback, mirroring
    _story_entries' existing key logic) since they have no other stable identity."""
    pool = list(new_opening_items) + list(watching_candidate_items)

    for items, category in (
        (industry_items, "industry"),
        (culture_items, "culture"),
        (ai_items, "ai_product"),
    ):
        for raw in items:
            item = dict(raw)
            item["category"] = category
            item["id"] = item.get("url") or f"{category}::{item.get('headline', '')}"
            pool.append(item)

    return pool


def _parse_editor_response(text: str, pool: list) -> list:
    """Parses edit_and_rank's JSON response. On failure, falls back to a deterministic
    pass-through — keep each item's research-assigned category, rank by original order —
    so a bad editor-pass response never silently empties the whole digest."""
    try:
        clean = re.sub(r"```[a-z]*", "", text).strip().strip("`").strip()
        start = clean.index("[")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if isinstance(data, list) and data:
            return data
    except Exception as e:
        print(f"Error parsing editor response: {e}")

    print("Falling back to pass-through categorization (no merge/rerank this run)")
    fallback = []
    rank_counters = {}
    for raw in pool:
        item = dict(raw)
        cat = item.get("category", "culture")
        rank_counters[cat] = rank_counters.get(cat, 0) + 1
        item["importance_rank"] = rank_counters[cat]
        item.setdefault("merged_from", [])
        fallback.append(item)
    return fallback


def edit_and_rank(pool: list, watching_context: list, holiday_hint: str = None) -> list:
    """The single consolidation pass: merges duplicate entities researched independently
    across calls, confirms/reassigns each story's final category, and ranks importance
    within category."""
    if not pool:
        return []

    pool_json = json.dumps(pool, indent=2)
    watching_json = json.dumps(watching_context, indent=2) if watching_context else "[]"

    prompt = f"""
You are the final editor for the ResX restaurant-industry Slack digest. Below is the full pool
of candidate stories researched for this issue — some may describe the same underlying
restaurant, event, or entity from different angles (e.g. a chef opening a restaurant surfaced
once as a "new_opening" and again as an "industry" story; a celebrity-at-a-restaurant story
that also mentions the restaurant just opened).

CANDIDATE STORIES (JSON):
{pool_json}

ALREADY TRACKED AS "WATCHING" from prior issues (context only — do not re-emit these, they
are carried forward automatically; only use this to recognize when a NEW candidate above is
actually the same restaurant, even if renamed or a qualifier like "(Williamsburg)" was dropped
now that it's open):
{watching_json}

This is an EXECUTIVE BRIEFING, not a news summary. Do four things:

1. MERGE DUPLICATES. If two or more candidates are fundamentally about the same underlying
   restaurant/entity/event, merge them into ONE story — keep the single best title/summary
   (prefer the more specific, more factual version; combine distinct facts from both if each
   adds something the other lacks). A story must exist exactly once in your output. When
   merging, prefer the MORE ACTIONABLE framing — e.g. if a "chef opening a new restaurant"
   story appears both as an opening-style item and an industry-style item, merge into one
   new_opening (or watching) entry, not an industry entry.

2. ASSIGN FINAL CATEGORY — exactly one of: new_opening, watching, industry, culture, ai_product.
   Rules, in priority order:
   a. If a restaurant/hotel/bakery/bar/members-club etc. has OFFICIALLY OPENED (verifiably
      taking reservations/walk-ins) -> new_opening.
   b. Else if it's an ANNOUNCED-BUT-NOT-YET-OPEN restaurant (teaser, soft opening, confirmed
      future launch) -> watching.
   c. Items whose "origin" field is "pinned" keep their given category exactly as provided —
      do not recategorize pinned items, only rank them.
   d. For everything else, assign whichever of industry / culture / ai_product is the SINGLE
      most actionable section for a ResX team member — i.e. which section someone would most
      usefully look under. If a story plausibly fits two, pick the one where its PRIMARY
      newsworthy angle lives (a chef's new restaurant business deal -> industry; the same
      chef's restaurant being an Instagram-viral celebrity hangout -> culture). Never assign
      the same story to two categories.

3. FILTER FOR EXECUTIVE RELEVANCE — non-pinned items only. Every remaining story must answer:
   why should the ResX team care? That can be strategic, operational, cultural, competitive,
   or product-related. If a story is merely interesting but wouldn't change what the team
   would actually discuss internally that week, DROP IT from your output entirely — do not
   include it just because it happened. This filter does NOT apply to items whose "origin"
   field is "pinned" — those were already explicitly requested by Georgia and were already
   vetted for relevance before reaching you; never drop a pinned item for relevance reasons,
   only for being an exact duplicate of another item in this same pool.

4. RANK BY IMPORTANCE within each of industry/culture/ai_product: assign importance_rank 1..N
   per category (1 = most important). Pinned items ALWAYS outrank non-pinned items in the same
   category — give every pinned item a lower (more important) importance_rank number than any
   non-pinned item, regardless of how individually notable the non-pinned items are; pinned
   input overrides normal ranking. Among pinned items, and separately among non-pinned items,
   rank by specificity, relevance to a last-minute dining reservation marketplace in NYC/London
   for 25-35 year olds, and whether the story has a genuinely notable, concrete hook (not
   generic trend commentary). new_opening/watching items don't need ranking — set
   importance_rank to 0 for those.

Do not invent facts. Do not soften or genericize any story's summary/so_what/detail/why_it_matters
— preserve the existing specificity and insider detail exactly as written.

Return ONLY a valid JSON array, no markdown: every SURVIVING story object (dropped stories
simply don't appear in the array) with all of its original fields preserved, plus "category"
(final), "importance_rank" (int), and "merged_from" (list of the input ids that were merged
into this story, [] if it wasn't a merge).
"""

    if holiday_hint:
        prompt = f"CONTEXT FOR THIS RUN: {holiday_hint}\n\n" + prompt.lstrip()

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are the executive editor of a briefing, not a news aggregator. You never "
            "invent facts, never soften specific details into generic ones, never let the "
            "same story appear twice, and you cut anything that wouldn't change what the "
            "team discusses this week — except pinned items, which are never dropped for "
            "relevance and always outrank non-pinned items in their category. "
            "Return only a valid JSON array, no markdown."
        ),
        max_tokens=6000,
    )

    return _parse_editor_response(result, pool)


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
    source_url = item.get("source_url", "")

    name_str = f"*{safe_link(website, name)}*" if website else f"*{name}*"
    if date:
        date_str = safe_link(source_url, date) if source_url else date
        name_str += f"  _{date_str}_"
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
        url = item.get("url", "")
        headline_str = f"*{safe_link(url, headline)}*" if url else f"*{headline}*"
        tag_str = f"  _{tag}_" if tag else ""
        lines.append(f"• {headline_str}{tag_str}\n  {detail}")
    return "\n\n".join(lines)


def format_ai_item(item: dict) -> str:
    """Unlike format_news_items' compact inline so_what, AI & Product items render an
    explicit, own-line 'Why it matters:' — a literal requirement for this section."""
    headline = item.get("headline", "")
    detail = item.get("detail", "")
    why = item.get("why_it_matters") or item.get("so_what", "")
    url = item.get("url", "")
    headline_str = f"*{safe_link(url, headline)}*" if url else f"*{headline}*"
    lines = [f"• {headline_str}"]
    if detail:
        lines.append(f"  {detail}")
    if why:
        lines.append(f"  *Why it matters:* {why}")
    return "\n".join(lines)


def format_ai_items(items: list) -> str:
    return "\n\n".join(format_ai_item(item) for item in items)


def build_slack_blocks(
    date_str: str,
    stories: list,
    special_header: str = None,
    new_competitors: list = None,
) -> list:
    """Renders the 5-category digest from one unified, already-categorized/ranked story list
    (see edit_and_rank) instead of 7 independently-researched parameter lists."""

    new_openings = [s for s in stories if s.get("category") == "new_opening"]
    watching     = [s for s in stories if s.get("category") == "watching"]
    rank_key     = lambda s: s.get("importance_rank") or 999
    industry     = sorted((s for s in stories if s.get("category") == "industry"), key=rank_key)
    culture      = sorted((s for s in stories if s.get("category") == "culture"), key=rank_key)
    ai_product   = sorted((s for s in stories if s.get("category") == "ai_product"), key=rank_key)

    blocks = []

    # Optional special edition one-liner (holidays, birthdays, etc.)
    if special_header:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*{special_header}*"}],
        })

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
    nyc_new = [s for s in new_openings if s.get("city", "").upper() in ("NYC", "BOTH")]
    ldn_new = [s for s in new_openings if s.get("city", "").upper() in ("LDN", "BOTH")]
    if nyc_new:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🗽  NYC*"}})
        for item in nyc_new:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
    if ldn_new:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"}})
        for item in ldn_new:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
    blocks.append({"type": "divider"})

    # ── 🔭 Watching ─────────────────────────────────────────────────────────
    if watching:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔭  WATCHING*"},
        })
        nyc_watch = [s for s in watching if s.get("city", "").upper() in ("NYC", "BOTH")]
        ldn_watch = [s for s in watching if s.get("city", "").upper() in ("LDN", "BOTH")]
        if nyc_watch:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🗽  NYC*"}})
            for item in nyc_watch:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
        if ldn_watch:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🇬🇧  London*"}})
            for item in ldn_watch:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": format_opening_item(item)}})
        blocks.append({"type": "divider"})

    # ── 2. Industry & Competitor Watch ──────────────────────────────────────
    competitor_watch = [s for s in industry if s.get("is_competitor_watch")]
    other_industry   = [s for s in industry if not s.get("is_competitor_watch")]
    if industry or new_competitors:
        parts = ["*🏢  INDUSTRY & COMPETITOR WATCH*"]
        if competitor_watch:
            parts.append(f"*🎯 Competitor Watch*\n\n{format_news_items(competitor_watch)}")
        if other_industry:
            parts.append(format_news_items(other_industry))
        if new_competitors:
            comp_str = ", ".join(new_competitors)
            parts.append(f"*New competitor spotted:* {comp_str}")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text("\n\n".join(parts))},
        })
        blocks.append({"type": "divider"})

    # ── 3. City & Culture ────────────────────────────────────────────────────
    culture_moments = upcoming_calendar_moments(datetime.date.today())
    if culture or culture_moments:
        parts = ["*🏙️  CITY & CULTURE*"]
        if culture_moments:
            today = datetime.date.today()

            def _when(du):
                if du == 0:
                    return "today"
                if du == 1:
                    return "tomorrow"
                return (today + datetime.timedelta(days=du)).strftime("%a")
            radar = "   ·   ".join(f"{m['name']} _{_when(m['days_until'])}_"
                                   for m in culture_moments if m["name"])
            parts.append(f"📅 *Coming up:*  {radar}")
        if culture:
            parts.append(format_news_items(culture))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text("\n\n".join(parts))},
        })
        blocks.append({"type": "divider"})

    # ── 4. AI & Product ──────────────────────────────────────────────────────
    if ai_product:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": safe_text(f"*🤖  AI & PRODUCT*\n\n{format_ai_items(ai_product)}")},
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

def _story_entries(items: list, today_iso: str) -> list:
    """Build dedup entries (key + the actual coverage text) from a list of story dicts,
    so a later run can judge whether a repeat has a materially new development."""
    entries = []
    for item in items:
        key = item.get("url") or item.get("headline", "")
        if not key:
            continue
        entries.append({
            "key": key, "date": today_iso,
            "headline": item.get("headline", ""),
            "detail": item.get("detail", ""),
            "so_what": item.get("so_what") or item.get("why_it_matters", ""),
        })
    return entries


def main():
    today = datetime.date.today()
    today_str = today.strftime("%B %d, %Y")
    today_iso = today.isoformat()

    force_post = os.environ.get("FORCE_POST") == "1"
    trigger = "forced" if force_post else os.environ.get("TRIGGER_TYPE", "manual")
    run_id = os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")

    print(f"Running ResX News Bot — {today_str} [{trigger.upper()}]")

    git_configure_identity()

    claimed, skip_reason = claim_todays_run(today_iso, trigger, run_id, force=force_post)
    if not claimed:
        print(f"[SKIPPED] {skip_reason}")
        log_run_event(today_iso, trigger, "skipped", skip_reason)
        return

    print("[STARTED] Claimed today's run slot")

    try:
        seen_openings = set(load_json(SEEN_OPENINGS_FILE, []))
        watching = load_json(WATCHING_FILE, [])

        # Load pinned inputs (manually submitted leads — high-priority, never silently ignored)
        pinned_inputs = load_json(PINNED_INPUTS_FILE, [])
        skipped_log = load_json(SKIPPED_ITEMS_LOG_FILE, [])

        # Load cross-run story history (last 14 days = ~4 runs)
        seen_stories_raw = load_json(SEEN_STORIES_FILE, [])
        cutoff = (today - datetime.timedelta(days=14)).isoformat()
        recent_stories = [e for e in seen_stories_raw if e.get("date", "") >= cutoff]
        print(f"Loaded {len(recent_stories)} recent stories for dedup")

        # Compute holiday context
        holiday_ctx = get_holiday_context(today)
        special_header = holiday_ctx["special_header"]
        holiday_hint = " ".join(holiday_ctx["prompt_hints"]) if holiday_ctx["prompt_hints"] else None
        if special_header:
            print(f"Holiday special edition: {special_header}")
        if holiday_hint:
            print(f"Holiday hint injected: {holiday_hint[:80]}...")

        print("Refreshing competitor list...")
        competitors, new_competitors = refresh_competitors()

        print("Researching NYC openings...")
        nyc_result = research_openings("nyc", seen_openings, watching)

        print("Researching London openings...")
        london_result = research_openings("london", seen_openings, watching)

        nyc_data    = nyc_result.get("just_opened", {"items": []})
        london_data = london_result.get("just_opened", {"items": []})

        # Deterministic graduation — carried forward untouched, guarantees the hard
        # "must disappear from Watching" requirement regardless of the editor pass below.
        opened_ids = {i["id"] for i in nyc_data.get("items", []) + london_data.get("items", [])}
        watching_carried = [
            w for w in watching
            if w.get("id", normalize_identity(w.get("name", ""), w.get("city", ""))) not in opened_ids
        ]
        carried_ids = {
            w.get("id", normalize_identity(w.get("name", ""), w.get("city", "")))
            for w in watching_carried
        }

        # Only genuinely NEW coming_soon candidates enter the pool for editing/dedup — everything
        # already tracked is carried forward untouched, never re-derived from what the editor
        # pass happens to re-emit this run (which would silently drop untouched entries).
        new_watching_candidates = [
            item for item in (nyc_result.get("coming_soon", []) + london_result.get("coming_soon", []))
            if item["id"] not in carried_ids
        ]

        print("Researching competitor watch...")
        competitor_watch_items = research_competitor_watch(
            competitors, seen_stories=recent_stories, holiday_hint=holiday_hint
        )

        print("Researching industry & competitor watch...")
        industry_items = competitor_watch_items + research_industry(
            exclude=competitor_watch_items, seen_stories=recent_stories, holiday_hint=holiday_hint
        )

        print("Researching city & culture...")
        culture_items = research_culture(exclude=industry_items, seen_stories=recent_stories, holiday_hint=holiday_hint)

        print("Researching AI & product...")
        ai_items = research_ai_product(
            exclude=industry_items + culture_items, seen_stories=recent_stories, holiday_hint=holiday_hint
        )

        pool = normalize_stories(
            nyc_data.get("items", []) + london_data.get("items", []),
            new_watching_candidates, industry_items, culture_items, ai_items,
        )

        print(f"Processing {len(pinned_inputs)} pinned input(s)...")
        pinned_stories, pinned_skip_entries = process_pinned_inputs(
            pinned_inputs, seen_openings, watching_carried, recent_stories, today_iso, holiday_hint
        )
        for s in pinned_stories:
            print(f"Pinned input resolved -> {s.get('category')}: {s.get('headline') or s.get('name', '')}")
        for e in pinned_skip_entries:
            print(f"Pinned input skipped ({e.get('reason')}): {e.get('input', '')!r} — {e.get('detail', '')}")
        pool += pinned_stories

        print(f"Editing & ranking {len(pool)} candidate stories...")
        final_stories = edit_and_rank(pool, watching_context=watching_carried, holiday_hint=holiday_hint)

        # Final deterministic link hygiene on every opening/watching item — guarantees no video,
        # profile-as-cover, or hard-dead cover/IG link ever renders, regardless of whether it came
        # from research, a pinned input, or an edit_and_rank merge.
        for s in final_stories:
            if s.get("category") in ("new_opening", "watching"):
                sanitize_opening_links(s)

        new_opening_stories = [s for s in final_stories if s.get("category") == "new_opening"]
        watching = watching_carried + [s for s in final_stories if s.get("category") == "watching"]
        seen_openings.update(s["name"] for s in new_opening_stories if s.get("name"))

        # Save cross-run story history (keep last 30 days to cap file size)
        new_story_entries = _story_entries(
            [s for s in final_stories if s.get("category") in ("industry", "culture", "ai_product")],
            today_iso,
        )
        keep_cutoff = (today - datetime.timedelta(days=30)).isoformat()
        all_stories = [e for e in seen_stories_raw if e.get("date", "") >= keep_cutoff] + new_story_entries

        print("Building Slack blocks...")
        blocks = build_slack_blocks(
            date_str=today_str,
            stories=final_stories,
            special_header=special_header,
            new_competitors=new_competitors,
        )

        print(f"Posting to Slack... ({len(blocks)} blocks)")
        for i, b in enumerate(blocks):
            txt = b.get("text", {}).get("text", "")
            if txt:
                print(f"  block[{i}] len={len(txt)}: {txt[:80]!r}")
        post_to_slack(blocks)

        # Persist everything from this run in one shot, now that the post landed
        save_json(WATCHING_FILE, watching)
        save_json(SEEN_OPENINGS_FILE, list(seen_openings))
        save_json(SEEN_STORIES_FILE, all_stories)
        # Every pinned input was resolved-or-skipped-with-reason above (see process_pinned_inputs'
        # safety net) — safe to clear the whole queue, nothing is silently dropped.
        if pinned_inputs:
            save_json(PINNED_INPUTS_FILE, [])
        skip_keep_cutoff = (today - datetime.timedelta(days=30)).isoformat()
        all_skips = [e for e in skipped_log if e.get("date", "") >= skip_keep_cutoff] + pinned_skip_entries
        save_json(SKIPPED_ITEMS_LOG_FILE, all_skips)
        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "completed", "run_id": run_id,
            "trigger": trigger, "posted_at": _now_utc().isoformat(),
        })

        state_paths = [
            str(p) for p in (
                WATCHING_FILE, SEEN_OPENINGS_FILE, SEEN_STORIES_FILE,
                PINNED_INPUTS_FILE, SKIPPED_ITEMS_LOG_FILE, COMPETITORS_FILE, LAST_POST_FILE,
            )
        ]
        if not git_commit_and_push(state_paths, f"bot: update data files [skip ci]"):
            print("WARNING: final state commit did not land after retries — next run may not see today's post")

        log_run_event(today_iso, trigger, "completed", f"{len(blocks)} blocks posted")
        print("[COMPLETED] Done ✓")

    except Exception as e:
        print(f"[FAILED] {e}")
        # Best-effort: mark the claim as failed so a legitimate retry doesn't have to
        # wait out the full staleness window.
        save_json(LAST_POST_FILE, {
            "date": today_iso, "status": "failed", "run_id": run_id,
            "trigger": trigger, "failed_at": _now_utc().isoformat(), "error": str(e),
        })
        git_commit_and_push([str(LAST_POST_FILE)], f"bot: mark {today_iso} run failed [skip ci]")
        log_run_event(today_iso, trigger, "failed", str(e))
        raise


if __name__ == "__main__":
    main()
