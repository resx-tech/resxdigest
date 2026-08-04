"""
ResX Social Bot
Runs daily via GitHub Actions, posts to #social. A social media editor with taste, not a
hospitality newsletter: every item answers "what should ResX post TODAY", never "what happened."

Three buckets: REPOST (one post to reshare), POST_IDEA (a concrete thing to make, backed by real
posts), TRENDING_AUDIO (song + link only). No generated captions/comments/copy — the bot surfaces
the opportunity and the real link; the team writes their own words.

The links are the product. Plain web_search can't return Instagram/TikTok permalinks, so the bot
web_searches fresh articles, web_fetches them, and mines the embedded post link out of the page
(see call_anthropic + research_ugc). Link fallback ladder (tier_and_label): a real permalink →
else a specific editorial article + the account (a labeled "lead") → never dropped for a good
moment. It is NEVER empty: an empty digest is a failure, not a quiet day. Scoring drives rank
order only (best 3-5/day). Dedup is permanent — the exact post/song never repeats; a venue can
return only for a genuinely new "moment". Both cities' key restaurants are tracked every run
(data/social_tracked_restaurants.json).
"""

import os
import json
import re
import time
import urllib.request
import datetime
from pathlib import Path

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL  = os.environ["SLACK_SOCIAL_WEBHOOK_URL"]

SEEN_UGC_FILE = Path("data/seen_ugc.json")
LAST_POST_FILE = Path("data/last_social_post.json")
PINNED_LEADS_FILE = Path("data/social_pinned_leads.json")
SKIPPED_LOG_FILE = Path("data/social_skipped_log.json")
TRACKED_RESTAURANTS_FILE = Path("data/social_tracked_restaurants.json")
# Shared with bot.py (the digest) — one source of truth for cultural moments (food/drink days +
# one-off events/releases), so a moment added once shows up in BOTH bots. Data, not code.
CALENDAR_MOMENTS_FILE = Path("data/calendar_moments.json")

# Scoring now drives RANK ORDER only (not an absolute cutoff). The axes are the taste
# rubric a good social editor actually uses to decide "should ResX post this today?"
SCORE_AXES = ("momentum", "stop_scroll", "desire_fit", "timeliness", "source_quality")
DAILY_TARGET_N = 5  # rank all candidates, surface the best ~this many; never zero.
# Researched reposts/posts whose POST is older than this (or undated) are dropped — a weeks-old
# reel is not "fresh". Real gate now, not a prompt hope: web_fetch lets the model verify dates, so
# this no longer nukes everything like the pre-web_fetch version did. Pinned leads are exempt.
FRESHNESS_CUTOFF_DAYS = 14

# "Never repeat anything": dedup is permanent, not a rolling window. We block re-sending
# the exact post URL or the exact song forever; a venue can only come back for a genuinely
# NEW moment (dedup keys on the moment, not the venue name). Kept effectively forever.
SEEN_RETENTION_DAYS = 3650

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


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# Server-side tools. web_search finds fresh articles; web_fetch opens them so the model
# can mine the real Instagram/TikTok permalink out of the embed (the whole point — plain
# web_search can't retrieve post-level links, but articles embed them). Both are the
# _20260209 dynamic-filtering variants, supported on claude-sonnet-4-6. max_content_tokens
# caps how much of each fetched article is pulled in, to bound cost.
# Use the BASIC web tool variants, NOT the _20260209 "dynamic filtering" ones. The _20260209
# variants run code execution under the hood, which (a) burns the server tool-loop iteration
# budget so the turn always pauses before writing the JSON, and (b) makes resuming a pause
# require a code-execution container_id (400 "container_id is required..."). The basic variants
# do neither: each tool use is one plain iteration, no container, so the whole search→fetch→mine
# chain completes in a single streamed response with no pause_turn to resume. web_fetch is beta.
ANTHROPIC_BETA = "web-fetch-2025-09-10"
TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 6000},
]

# Two hard-won facts drive this design (both observed 2026-07-11):
# 1. We MUST stream. A non-streaming request that runs long server-tool loops sits idle (no
#    bytes) while the server works, and Anthropic's edge drops the idle connection
#    (RemoteDisconnected after ~4 min). Streaming keeps it alive with events/pings.
# 2. We MUST handle pause_turn. The server tool loop caps at ~10 iterations per response (and the
#    _20260209 tools spend extra iterations on dynamic-filtering code execution), so a real
#    search→fetch→mine chain pauses BEFORE emitting the final JSON. We reconstruct the assistant
#    turn from the stream and re-send to resume, until it finishes (end_turn) or we hit the budget.
REQUEST_TIMEOUT = 300   # seconds allowed between streamed events before we give up
OVERALL_BUDGET = 780    # seconds across all pause_turn continuations (job cap is 25 min)


def _stream_once(convo: list, system: str, max_tokens: int) -> tuple:
    """Stream one request. Returns (answer_text, stop_reason, assistant_content_blocks). The
    content blocks are reconstructed from the SSE stream so we can echo them back to resume a
    pause_turn (they include the server_tool_use + web_search/web_fetch result blocks)."""
    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": convo,
        "tools": TOOLS,
        "stream": True,
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

    blocks = {}          # index -> content block (reconstructed)
    json_buffers = {}    # index -> accumulated input_json string (for server_tool_use inputs)
    text_parts = []
    stop_reason = None
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            for raw in resp:  # server-sent events, one line at a time
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[len("data:"):].strip()
                if not blob:
                    continue
                try:
                    evt = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "content_block_start":
                    blocks[evt.get("index")] = dict(evt.get("content_block", {}) or {})
                elif t == "content_block_delta":
                    idx = evt.get("index")
                    delta = evt.get("delta", {})
                    dt = delta.get("type")
                    if dt == "text_delta":
                        text_parts.append(delta.get("text", ""))
                        blk = blocks.setdefault(idx, {"type": "text", "text": ""})
                        blk["text"] = blk.get("text", "") + delta.get("text", "")
                    elif dt == "input_json_delta":
                        json_buffers[idx] = json_buffers.get(idx, "") + delta.get("partial_json", "")
                elif t == "content_block_stop":
                    idx = evt.get("index")
                    if idx in json_buffers and idx in blocks:
                        try:
                            blocks[idx]["input"] = json.loads(json_buffers[idx] or "{}")
                        except json.JSONDecodeError:
                            blocks[idx].setdefault("input", {})
                elif t == "message_delta":
                    stop_reason = evt.get("delta", {}).get("stop_reason") or stop_reason
                elif t == "error":
                    print(f"Anthropic stream error: {evt.get('error')}")
                    break
    except urllib.error.HTTPError as e:  # e.g. 400 on a resume request — log the body to diagnose
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:1200]
        except Exception:
            pass
        print(f"Anthropic HTTP {e.code} on stream request: {body}")
    except Exception as e:  # timeout, connection drop — return whatever we have
        print(f"Anthropic request failed ({type(e).__name__}: {e}); returning partial output.")

    # Sanitize the reconstructed turn before it's echoed back to resume a pause_turn: the API
    # rejects empty text blocks in assistant content, and a server_tool_use needs an input key.
    content = []
    for i in sorted(blocks.keys()):
        b = blocks[i]
        if b.get("type") == "text" and not (b.get("text") or "").strip():
            continue
        if b.get("type") == "server_tool_use" and "input" not in b:
            b["input"] = {}
        content.append(b)
    return "".join(text_parts), stop_reason, content


def call_anthropic(messages: list, system: str, max_tokens: int = 4000,
                   max_continuations: int = 4) -> str:
    """One logical Claude turn with the article-link-mining tools, streamed and resumed across
    pause_turn until it finishes. Returns the final answer text (the JSON)."""
    convo = list(messages)
    final_text = ""
    started = time.monotonic()
    for turn in range(max_continuations + 1):
        text, stop_reason, content = _stream_once(convo, system, max_tokens)
        if text.strip():
            final_text = text  # the JSON lands in the final (non-paused) turn
        if stop_reason != "pause_turn":
            break
        if time.monotonic() - started > OVERALL_BUDGET:
            print("Hit overall research budget; stopping pause_turn continuation.")
            break
        if not content:
            break  # nothing to resume from
        print(f"pause_turn — resuming research (continuation {turn + 1})")
        convo = convo + [{"role": "assistant", "content": content}]
    return final_text


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


def safe_link(url: str, label: str) -> str:
    url = url.replace("&", "&amp;").replace("<", "").replace(">", "").replace("|", "%7C")
    label = label.replace("<", "").replace(">", "").replace("|", "-").replace("&", "&amp;")
    return f"<{url}|{label}>"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """Date of the nth <weekday> in a month (weekday: Mon=0..Sun=6) — for days that fall on
    e.g. the 'first Friday of June' rather than a fixed calendar date."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def upcoming_calendar_moments(today: datetime.date, window: int = 7) -> list:
    """Cultural moments within `window` days (or ongoing, for multi-day events), soonest first,
    from the calendar shared with the digest bot: food/drink days AND one-off events/releases
    (US Open, a festival, a premiere). Returns {name, hint, days_until}. These are prime social
    moments and get surfaced at the TOP of the digest so nobody misses them."""
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
        if du <= window and today <= end:
            out.append({"name": e.get("name", ""), "hint": e.get("hint", ""), "days_until": max(du, 0)})
    out.sort(key=lambda m: m["days_until"])
    return [m for m in out if m["name"] or m["hint"]]


def _salvage_json_array(text: str, key: str) -> list:
    """Best-effort recovery of the complete objects from a JSON array named `key` inside a
    possibly-truncated response — returns as many elements as decode cleanly, ignoring a
    cut-off tail. Lets a truncated model response still yield its finished items instead of
    zeroing out the whole run."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if not m:
        return []
    i = m.end()
    dec = json.JSONDecoder()
    out = []
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break  # truncated element — stop here, keep what we have
        out.append(obj)
        i = end
    return out


def research_ugc(seen_urls: set, seen_moments: set, seen_songs: set,
                 pinned_leads: list, tracked: list) -> dict:
    seen_str = "\n".join(f"- {u}" for u in list(seen_urls)[:80]) if seen_urls else "none"
    moments_str = "\n".join(f"- {s}" for s in sorted(seen_moments)) if seen_moments else "none"
    songs_str = "\n".join(f"- {s}" for s in sorted(seen_songs)) if seen_songs else "none"
    signal = ", ".join(NYC_SIGNAL_ACCOUNTS + LONDON_SIGNAL_ACCOUNTS)
    pinned_str = (
        "\n".join(f'- "{p.get("input", "")}"' + (f'  (note: {p["note"]})' if p.get("note") else "")
                   for p in pinned_leads)
        if pinned_leads else "none"
    )
    tracked_str = (
        "\n".join(f"- {t.get('name','')} ({t.get('city','')})" for t in tracked)
        if tracked else "none"
    )
    food_hints = [m["hint"] for m in upcoming_calendar_moments(datetime.date.today()) if m["hint"]]
    if food_hints:
        food_day_str = (
            "\n═══════════════════════════════════════════════════════════════════════════\n"
            "CALENDAR MOMENTS IN THE NEXT WEEK — prime social opportunities\n"
            "═══════════════════════════════════════════════════════════════════════════\n"
            + "\n".join(f"- {h}" for h in food_hints)
            + "\nFor each of these: find the SPECIFIC venues actually doing something for it — a"
              " special, a collab, a themed menu, a limited drop — and the ACTUAL post. A generic"
              " mention of the day is worthless. If nothing specific is happening, skip it rather"
              " than forcing a generic tie-in.\n"
        )
    else:
        food_day_str = ""

    prompt = f"""
You are the social media editor for ResX — a last-minute restaurant reservation app for cool
25-35 year olds in NYC and London. You spend all day on Instagram and TikTok and you have
incredible taste. The ONE question you answer every morning is: "what should ResX post TODAY?"
Never "what happened in hospitality today?" — that distinction is everything.

Today is {datetime.date.today().strftime("%A, %B %d, %Y")}.
Target audience vibe (calibration only, do NOT cite): {signal}

═══════════════════════════════════════════════════════════════════════════
THE TASTE TEST (this is the whole job)
═══════════════════════════════════════════════════════════════════════════
For every candidate, ask: would a sharp social editor screenshot this and say "we HAVE to post
this today"? If it doesn't spark "I hadn't seen that yet" or "we should absolutely post that" —
cut it. Rank everything by these instincts, in priority order:

1. MOMENTUM, NOT PEAK. Catch the wave on the way UP. A spot people are JUST starting to post
   from beats one that already saturated the feed. Early > complete.
2. STOP THE SCROLL. Visually striking, surprising, craveable, or genuinely funny. Strongly prefer
   a post that actually SHOWS the food / drink / experience (the craveable thing) over a storefront
   or exterior shot. Forgettable = out.
3. DESIRE / FOMO. ResX sells the feeling of "I need to be there / eat that / book this tonight."
   Lead with content that creates want.
4. SHAREABLE & SAVEABLE. Stuff people send to a friend or save ("omg we have to go"). That
   send-to-a-friend impulse is exactly what converts for ResX.
5. A REASON IT'S TODAY. Heatwave, Bastille Day, a premiere, a proposal, a pop-up ending this
   weekend. Ride the moment while it's hot.
6. ON-BRAND. Cool, downtown, 25-35, NYC/London — restaurants, going out, and the culture around it.
7. REAL SOURCE. Actual creators / venues / editorial press. Never spammy, generic, or SEO-bait.

WHAT TO HUNT FOR (wide aperture — this is a culture feed, not a trade publication):
- Celebrity sightings & viral date nights; a restaurant everyone suddenly can't stop posting
  (viral TikTok spot, impossible res, secret menu, one-week collab/pop-up).
- Viral menu items and food collabs (a Sungold Sundae, a limited-drop pastry, a bar takeover).
- FOMO openings & activations (rooftop, hotel, luxury-brand café, fan villages, food festivals).
- POP CULTURE, even with NO restaurant tie — a big movie premiere, a show everyone's watching, a
  major NYC/London city moment (e.g. someone climbing a landmark to propose). If ResX could ride
  it in its social voice, it counts.
- BIG CULTURAL ANCHORS worth a whole carousel: the START of a major event (a tournament like the
  US Open, a festival, fashion week), a hyped movie release or TV season finale, a citywide
  moment. If one is landing this week, build the going-out/dining angle around it (e.g. "US Open
  starts — the honey-deuce + courtside-dining moment," "the [show] finale — watch-party spots").
- Timely lifestyle hooks: heatwave treats, marathon, Pride, holiday weekends, first day of patio szn.
- When a cultural moment or holiday is in play (Bastille Day, a premiere, a heatwave, a big match),
  surface the SPECIFIC venue doing something for it — a deal, a special, a themed menu, a collab —
  with the real post. A generic tie-in is weak (a plain croissant on Bastille Day); "X's Bastille
  Day free-Champagne hour" or "Y's French-themed pop-up today" is strong. If nothing specific is
  actually happening, skip the generic angle rather than forcing it.

ALWAYS CHECK THESE — ResX's key restaurants (both cities). Every run, look for anything genuinely
new worth posting at these specific spots (a new dish, a collab, a viral moment, big press today):
{tracked_str}
{food_day_str}

═══════════════════════════════════════════════════════════════════════════
HOW TO GET THE ACTUAL POST LINK (this is why you have web_fetch)
═══════════════════════════════════════════════════════════════════════════
The links ARE the product. Georgia wants the actual Reel/TikTok/post, not a website or a
profile page or an article she has to go hunt through.

Plain web_search usually can't return an Instagram/TikTok permalink directly — BUT fresh articles
usually EMBED the original post, and a focused search on a specific venue usually surfaces its real
account. Your workflow (do the drill-down — do NOT stop at a roundup):
  1. DISCOVER: web_search for what's blowing up today + fresh coverage of the tracked spots. A
     roundup/guide ("new openings", "top tables", "where to book") is fine ONLY to discover the
     specific venue or moment worth posting — it is NEVER an acceptable lead link itself.
  2. DRILL DOWN on each specific venue/moment you found: web_fetch the best SINGLE-SUBJECT article
     about it AND do a focused follow-up web_search for that exact venue/creator to (a) pull any
     embedded instagram.com/p/…, instagram.com/reel/…, or tiktok.com/@user/video/… permalink, and
     (b) confirm its REAL Instagram account (copy the exact profile URL you actually see — do not
     spell it out yourself).
  3. Prefer, in order: a real post permalink → a single-subject article PLUS the verified account.
     Always try to end with the venue's REAL, verified account attached so Georgia isn't left with
     only an article to hunt through. Better no account than a guessed one, but a quick follow-up
     search usually gets you the real one — do that search.

LINK ACCURACY IS CRITICAL — broken or wrong links are the #1 complaint. Only use a URL you
actually retrieved (from a search result or a fetched page). Never construct, guess, autocomplete,
"fix" spelling, or recall a URL from memory. This applies DOUBLY to account_url: use only the exact
profile URL you saw in the fetched article or a search result — do NOT build it from the venue's
name or handle, and do NOT correct a spelling yourself. A wrong account (a typo like an extra
letter, the wrong handle, or an account that doesn't exist — some brands aren't on Instagram at
all) is WORSE than no account. If you cannot verify the exact profile URL from a source, omit
account_url entirely. Confirm every link genuinely matches what you're describing; if unsure, drop it.

═══════════════════════════════════════════════════════════════════════════
GET THE REAL POST — an article "lead" is a LAST resort, not the default
═══════════════════════════════════════════════════════════════════════════
The actual Reel / TikTok / post IS the product. The CLEAR MAJORITY of what you return must be
real post permalinks (post_url) — not article links. If your list comes out mostly articles,
you have NOT drilled down hard enough: go back and find the actual posts.

For each opportunity, in order:
  1. REAL POST PERMALINK (post_url) — the goal every single time. To get it, do BOTH:
     • fetch the article and pull the embedded instagram.com/p/…, instagram.com/reel/…, or
       tiktok.com/@user/video/… link straight out of the page, AND
     • search the creator/venue DIRECTLY for the post — their handle + the moment, site:instagram.com,
       site:tiktok.com, the specific post an article is describing. Spend the effort here.
  2. LEAD (article_url + verified account_url) — ONLY after you've genuinely tried step 1 and still
     can't get the permalink. A lead must be a SPECIFIC single-subject editorial moment (one
     opening / collab / sighting — Time Out / Eater / Infatuation / Grub Street style, e.g. "the NYC
     hot dog king is giving out 500 free hot dogs outside the Met next week"), NEVER a
     "best restaurants" / "top tables" / "where to book" roundup or a marketing homepage. Attach the
     account only if you can extract its exact real profile URL (omit rather than guess).

BETTER to return 3 items that are REAL POSTS than 6 that are just leads. Prefer fewer, realer.
Keep leads to a small minority — if you can't get past mostly-articles, return fewer items.

═══════════════════════════════════════════════════════════════════════════
THE THREE BUCKETS (every item is exactly one)
═══════════════════════════════════════════════════════════════════════════
1. REPOST — one specific post ResX could reshare to Stories today.
   Good: Caffè Panna's Sungold Sundae reel (seasonal, craveable, of-the-moment).
   Good: casapiada's "how to kidnap me: [a van full of Aperol spritzes]" (funny, shareable).
   - post_url: the real permalink (preferred). If you truly can't get it, use the lead fallback
     (article_url + account_url) instead — still a valid repost lead.

2. POST_IDEA — a concrete thing ResX makes TODAY, ready to execute with no extra research.
   BAD (too vague): "World Cup Fan Village opened."
   GOOD: "Everyone's posting from Rockefeller Fan Village today — carousel the atmosphere + best
   spots to watch," backed by 4-6 real Reels.
   GOOD: "It's 95° in NYC — carousel the 5 ice-cream shops everyone's posting this week," backed
   by the 5 real posts.
   GOOD (pop culture, no restaurant tie): "Someone climbed the Empire State Building to propose —
   react to the NYC-summer-romance moment," backed by the real posts.
   - posts: entries of {{"post_url", "account_url", "why"}}. Aim for 2+ real permalinks; a couple
     rock-solid real posts beats five where three are guesses. If you can't get permalinks for a
     genuinely great idea, use the lead fallback (article_url + account_url) so it still ships.
   - "why" is a curation reason (e.g. "most-viewed of the bunch this morning"), NEVER a caption.

3. TRENDING_AUDIO — a song/sound worth using over a dining or going-out reel. First-class, not an
   afterthought. Just track + link, no explanation. Return 0-3, only if genuinely trending today.

YOU NEVER WRITE CAPTIONS, COMMENTS, OR SOCIAL COPY. You surface the opportunity and the real
link; the team writes their own words. The "headline" is a punchy one-line hook in ResX's voice
(insider, lowercase, like a friend texting a tip) — NOT a caption to post.

═══════════════════════════════════════════════════════════════════════════
NEVER EMPTY. There is ALWAYS something worth posting — a viral dish, a celeb sighting, a city
moment, a trending sound. Return the best 3-5 opportunities EVERY day (more on a huge day).
Returning nothing is a FAILURE, not a quiet day. Do not pad with junk either — but there is
always real, of-the-moment content out there; go find it.
═══════════════════════════════════════════════════════════════════════════

PINNED LEADS — HIGHEST PRIORITY (Georgia manually flagged these). Research each and build a real
opportunity (find the actual post behind a bare topic). Every pinned lead below MUST end up as an
opportunity with "origin":"pinned" and "pinned_input" set to its exact text, OR as a
"pinned_rejected" entry with that exact text and a reason ("duplicate" | "irrelevant" |
"no_content_found") — never silently omit one. Pinned leads bypass ranking, but still need at
least a lead-quality link (permalink, or article+account); if you can find no real content at
all, reject as "no_content_found".
Pinned leads to address:
{pinned_str}

DEDUP — never show Georgia the same thing twice.
- Do NOT reuse any URL already sent (exact-match list below).
- Do NOT repeat any MOMENT already featured (list below). A venue CAN come back, but only for a
  genuinely NEW, specifically-named development — never the same moment again.
Already-sent URLs:
{seen_str}
Already-featured moments — do NOT repeat these:
{moments_str}
Already-featured songs — do NOT reuse:
{songs_str}

═══════════════════════════════════════════════════════════════════════════
FRESHNESS — the POST itself must be recent, not just the article about it
═══════════════════════════════════════════════════════════════════════════
Georgia only wants FRESH content — roughly the last {FRESHNESS_CUTOFF_DAYS} days. Before including
any repost or post, determine the ACTUAL date the post/Reel was published (its own timestamp, the
"X days/weeks ago" it shows, or the date of the primary source you pulled it from). A post that
dropped weeks ago is NOT fresh even if you found it in a recent article — a month-old dirty-martini
soft-serve reel is exactly the stale content to reject. If it's older than ~{FRESHNESS_CUTOFF_DAYS}
days, drop it and find a fresher one. Report "posted_days_ago" for every researched repost/post_idea:
the real age in days of the post (for a post_idea backed by several, use the age of the OLDEST).
Never guess — if you genuinely can't tell, report 999 (it will be dropped). Pinned leads are exempt.

SCORING (researched items only; skip for pinned). Rate 1-5 on each axis — these decide RANK ORDER,
so be honest: momentum (on the way up vs. saturated), stop_scroll (does it stop the thumb),
desire_fit (FOMO + on-brand for ResX's audience), timeliness (a real reason it's today),
source_quality (real post/creator/editorial vs. weak source).

DIVERSITY — SPREAD ACROSS CREATORS AND VENUES. Never feature the same source account/creator more
than once in a day (don't surface two different posts both from, say, @ijustwant2eat — pick their
single best and move on). Set "source_account" to the handle the post/repost actually comes from
so this can be enforced. The mining tends to over-rely on whichever creator is most web-searchable,
so consciously vary the accounts you pull from — different creators, different venues, both cities.
Don't return two items about the same venue/moment/song either; give distinct ones different
"subject" and "moment" values.

Also list what you researched and chose NOT to include, as "considered_and_rejected"
(brief subject/url/reason each; best effort).

Return ONLY a valid JSON object, no markdown:
{{
  "opportunities": [
    {{"type": "repost", "origin": "researched|pinned", "pinned_input": "...", "headline": "...",
      "subject": "...", "moment": "...", "source_account": "handle_without_@", "city": "NYC|LDN|BOTH",
      "posted_days_ago": 0,
      "scores": {{"momentum": 1, "stop_scroll": 1, "desire_fit": 1, "timeliness": 1, "source_quality": 1}},
      "post_url": "...", "account_url": "...", "article_url": "..."}},
    {{"type": "post_idea", "origin": "researched|pinned", "pinned_input": "...", "headline": "...",
      "subject": "...", "moment": "...", "source_account": "handle_without_@", "city": "NYC|LDN|BOTH",
      "posted_days_ago": 0,
      "scores": {{"momentum": 1, "stop_scroll": 1, "desire_fit": 1, "timeliness": 1, "source_quality": 1}},
      "posts": [{{"post_url": "...", "account_url": "...", "why": "..."}}],
      "account_url": "...", "article_url": "..."}}
  ],
  "audio": [
    {{"song": "...", "artist": "...", "url": "..."}}
  ],
  "pinned_rejected": [
    {{"pinned_input": "...", "reason": "duplicate|irrelevant|no_content_found", "detail": "..."}}
  ],
  "considered_and_rejected": [
    {{"subject": "...", "url": "...", "reason": "...", "source_type": "researched"}}
  ]
}}
Field notes: "moment" is a short phrase naming the SPECIFIC thing (used for dedup — name the real
event, not the idea). "subject" is the venue/creator/topic (max 4 words). "source_account" is the
plain handle (no @) the post/repost comes from, used to avoid featuring one creator twice a day.
"posted_days_ago" is the verified age in days of the post (researched items only; 999 if unknown →
dropped). Include only the fields relevant to the item's type. Omit "scores"/"posted_days_ago" for
pinned items. Use "post_url"/"posts" when you have a real permalink; use "article_url" +
"account_url" as the lead fallback when you don't.
"""

    result = call_anthropic(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are the social media editor for ResX — a NYC and London restaurant reservation "
            "app for cool 25-35 year olds. You live on Instagram and TikTok and have incredible "
            "taste. You find what ResX should POST today, never summarize what happened. "
            "The links are the product: get the ACTUAL post. Plain web_search rarely returns an "
            "Instagram/TikTok permalink, but fresh articles embed them — so web_search for the "
            "moment, web_fetch the article, and pull the real permalink out of the page. "
            "If you truly can't get the permalink, fall back to the editorial article about the "
            "moment PLUS the account (a labeled lead) — never a marketing homepage or a listicle. "
            "Everything must be FRESH — verify each post's actual publish date and drop anything "
            "older than about two weeks (a weeks-old reel is stale even if the article is recent); "
            "report posted_days_ago honestly, 999 if you can't tell. "
            "There is ALWAYS something worth posting: an empty result is a failure, not a quiet "
            "day. Return the best 3-5 every day; don't pad with junk, but don't come back empty. "
            "You never write captions, comments, or copy — you surface the opportunity and the "
            "real link; the team writes their own words. "
            "Link accuracy is critical: only cite a URL you actually retrieved, never one you "
            "constructed or recalled, and never attach a link to content it doesn't match. "
            "Score honestly (scores set rank order). Never silently drop a pinned lead. "
            "Return only a valid JSON object, no markdown."
        ),
        max_tokens=8000,  # was 4000 — the JSON was truncated mid-string on richer days (unparseable
                          # → 0 opportunities → run failed). Streaming, so a high cap is safe.
    )

    empty = {"opportunities": [], "audio": [], "pinned_rejected": [], "considered_and_rejected": []}
    clean = re.sub(r"```[a-z]*", "", result).strip().strip("`").strip()
    try:
        start = clean.index("{")
        data, _ = json.JSONDecoder().raw_decode(clean, start)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
        return {
            "opportunities": data.get("opportunities", []) or [],
            "audio": data.get("audio", []) or [],
            "pinned_rejected": data.get("pinned_rejected", []) or [],
            "considered_and_rejected": data.get("considered_and_rejected", []) or [],
        }
    except Exception as e:
        # A truncated/malformed response used to zero out the whole day. Salvage the complete
        # opportunity/audio objects instead of failing the run entirely.
        print(f"Error parsing UGC results: {e}; attempting salvage")
        opps = _salvage_json_array(clean, "opportunities")
        audio = _salvage_json_array(clean, "audio")
        if opps or audio:
            print(f"Salvaged {len(opps)} opportunities, {len(audio)} audio from a truncated response")
            return {"opportunities": opps, "audio": audio,
                    "pinned_rejected": [], "considered_and_rejected": []}
        return empty


def urls_in_item(item: dict) -> list:
    """Every URL attached to an item — used for broken-link checks and logging."""
    urls = [item[k] for k in ("post_url", "account_url", "article_url") if item.get(k)]
    urls += [p[k] for p in item.get("posts", []) or [] for k in ("post_url", "account_url") if p.get(k)]
    return urls


def dedup_urls_in_item(item: dict) -> list:
    """Only the URLs we permanently block from reuse: post permalinks + the editorial article.
    Deliberately excludes account/profile URLs — accounts recur (a venue posts many times), so
    blocking them forever would retire the account. We block the exact post/article, not the account."""
    urls = [item[k] for k in ("post_url", "article_url") if item.get(k)]
    urls += [p["post_url"] for p in item.get("posts", []) or [] if p.get("post_url")]
    return urls


# A real post link points directly at a specific post (Instagram post/reel, TikTok video, X
# status, Threads post). This shape check is deterministic — we never trust the prompt alone to
# tell a permalink from a profile/article. It decides the link TIER, not whether an item lives
# or dies (see tier_and_label — a moment with no permalink still ships as a labeled lead).
POST_URL_PATTERNS = [
    re.compile(r"^https?://(www\.)?instagram\.com/p/[\w-]+"),
    re.compile(r"^https?://(www\.)?instagram\.com/reel/[\w-]+"),
    re.compile(r"^https?://(www\.)?tiktok\.com/@[\w.\-]+/video/\d+"),
    re.compile(r"^https?://(www\.)?(x|twitter)\.com/[\w]+/status/\d+"),
    re.compile(r"^https?://(www\.)?threads\.(net|com)/@[\w.\-]+/post/[\w]+"),
]


def is_valid_post_url(url: str) -> bool:
    """True only for a direct post-level URL (Instagram post/reel, TikTok video, X status,
    Threads post). False for a profile, website, article, newsletter, or city guide."""
    if not url:
        return False
    return any(p.match(url.strip()) for p in POST_URL_PATTERNS)


def is_probably_article(url: str) -> bool:
    """Lenient guard for the lead fallback: accept an editorial article URL (has a real path,
    e.g. timeout.com/newyork/news/...), reject a bare marketing homepage (path is just '/') and
    non-http URLs. We can't reliably separate 'editorial' from 'marketing' by URL alone — the
    prompt carries that rule; this only blocks the obvious homepage / no-path case."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    after = url.split("://", 1)[1]
    path = after[after.index("/"):] if "/" in after else ""
    return len(path.strip("/")) > 1


def tier_and_label(items: list, today_iso: str, source_type: str) -> tuple:
    """The link fallback ladder, enforced deterministically. Tags each kept item with `_tier`:
      - 'post' : a real permalink (repost) or >=1 valid backing post (post_idea)
      - 'lead' : no permalink, but a specific editorial article + an account (team grabs the post)
    Only items with neither are dropped and logged. 'Never empty' means strong moments ship as
    leads instead of getting discarded (the July-9 failure mode)."""
    kept = []
    skips = []
    for raw in items:
        item = dict(raw)
        # A lead needs a real editorial article. The account is a bonus, extracted-not-guessed
        # (see the prompt) — we allow an article-only lead so the model omits a shaky account
        # rather than attach a wrong one (wrong accounts were the #1 complaint).
        has_article_lead = is_probably_article(item.get("article_url", ""))

        if item.get("type") == "post_idea":
            valid_posts = [p for p in item.get("posts", []) or []
                           if is_valid_post_url(p.get("post_url", ""))]
            if valid_posts:
                item["posts"] = valid_posts
                item["_tier"] = "post"
                kept.append(item)
                continue
            if has_article_lead:
                item["posts"] = []
                item["_tier"] = "lead"
                kept.append(item)
                continue
        else:  # repost (default)
            if is_valid_post_url(item.get("post_url", "")):
                item["_tier"] = "post"
                kept.append(item)
                continue
            if has_article_lead:
                item["_tier"] = "lead"
                kept.append(item)
                continue

        skips.append({
            "date": today_iso, "subject": item.get("subject", ""),
            "url": (urls_in_item(item) or [""])[0], "reason": "no_usable_link",
            "detail": "no valid post permalink and no editorial-article+account lead",
            "source_type": source_type,
        })
    return kept, skips


def avg_score(item: dict) -> float:
    scores = item.get("scores") or {}
    vals = [scores.get(axis, 0) for axis in SCORE_AXES]
    return sum(vals) / len(vals) if vals else 0.0


def check_broken(url: str, timeout: int = 5) -> bool:
    """Best-effort dead-link check. Only returns True on a definitive 404/410 —
    network errors, timeouts, and blocks (common on Instagram/TikTok) are treated
    as inconclusive rather than broken, so we never punish a real link we can't verify."""
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


def resolve_pinned(pinned_leads: list, opportunities: list, pinned_rejected: list,
                    seen_urls: set, seen_moments: set, today_iso: str) -> tuple:
    """Cross-references the model's response against the pinned-leads queue so every
    pinned lead ends up either kept or logged as skipped — never silently dropped,
    even if the model forgot to mention one."""
    kept = []
    skips = []
    matched_inputs = set()

    for item in opportunities:
        if item.get("origin") != "pinned":
            continue
        pinned_input = item.get("pinned_input", "")
        matched_inputs.add(pinned_input)

        urls = urls_in_item(item)
        moment = (item.get("moment") or "").strip().lower()
        dup_url = next((u for u in dedup_urls_in_item(item) if u in seen_urls), None)
        if dup_url or (moment and moment in seen_moments):
            skips.append({
                "date": today_iso, "pinned_input": pinned_input, "subject": item.get("subject", ""),
                "url": dup_url or (urls[0] if urls else ""), "reason": "duplicate",
                "duplicate_match": dup_url or moment, "source_type": "pinned",
            })
            continue

        broken = next((u for u in urls if check_broken(u)), None)
        if broken:
            skips.append({
                "date": today_iso, "pinned_input": pinned_input, "subject": item.get("subject", ""),
                "url": broken, "reason": "broken_link", "source_type": "pinned",
            })
            continue

        kept.append(item)

    for rej in pinned_rejected:
        pinned_input = rej.get("pinned_input", "")
        matched_inputs.add(pinned_input)
        skips.append({
            "date": today_iso, "pinned_input": pinned_input, "subject": "",
            "url": "", "reason": rej.get("reason", "irrelevant"), "detail": rej.get("detail", ""),
            "source_type": "pinned",
        })

    for lead in pinned_leads:
        lead_input = lead.get("input", "")
        if lead_input not in matched_inputs:
            skips.append({
                "date": today_iso, "pinned_input": lead_input, "subject": "", "url": "",
                "reason": "not_addressed_by_model", "source_type": "pinned",
            })
            matched_inputs.add(lead_input)

    return kept, skips, matched_inputs


def account_identity(item: dict) -> str:
    """The creator/account an item comes from, normalized for dedup. Prefers the model-reported
    `source_account` handle; falls back to the handle inside account_url. Used to stop one
    web-searchable creator (e.g. @ijustwant2eat) from dominating a single digest."""
    acct = (item.get("source_account") or "").strip().lower().lstrip("@")
    if acct:
        return acct
    m = re.search(r"(?:instagram\.com|tiktok\.com)/@?([\w.\-]+)", item.get("account_url") or "")
    return m.group(1).lower() if m else ""


def validate_freshness(items: list, today_iso: str, cutoff: int = FRESHNESS_CUTOFF_DAYS) -> tuple:
    """Drop researched reposts/post_ideas whose post is older than `cutoff` days, or whose age is
    unknown/unparseable (an undated post is treated as not-fresh). Pinned items never reach here.
    This is a real deterministic gate — the prompt asks the model to verify dates, this enforces it."""
    kept, skips = [], []
    for item in items:
        raw = item.get("posted_days_ago")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = None
        if days is None or days > cutoff:
            skips.append({
                "date": today_iso, "subject": item.get("subject", ""),
                "url": (urls_in_item(item) or [""])[0], "reason": "stale_content",
                "detail": f"posted_days_ago={raw!r} — older than the {cutoff}-day freshness window (or undated)",
                "source_type": "researched",
            })
            continue
        kept.append(item)
    return kept, skips


def apply_diversity(ordered_items: list, today_iso: str) -> tuple:
    """Caps the digest at one item per subject (venue/topic) AND one per source account/creator,
    in the given priority order — pass pinned items first so they win any conflict. The account
    cap fights the mining's bias toward whichever creator is most web-searchable."""
    kept = []
    skips = []
    seen_subjects = set()
    seen_accounts = set()
    for item in ordered_items:
        subject = (item.get("subject") or "").strip().lower()
        account = account_identity(item)
        dup_reason = None
        if subject and subject in seen_subjects:
            dup_reason = f"already have an item for subject '{item.get('subject', '')}' in this digest"
        elif account and account in seen_accounts:
            dup_reason = f"already have an item from account '{account}' in this digest"
        if dup_reason:
            skips.append({
                "date": today_iso, "pinned_input": item.get("pinned_input", ""),
                "subject": item.get("subject", ""),
                "url": (urls_in_item(item) or [""])[0],
                "reason": "diversity_cap",
                "detail": dup_reason,
                "source_type": item.get("origin", "researched"),
            })
            continue
        if subject:
            seen_subjects.add(subject)
        if account:
            seen_accounts.add(account)
        kept.append(item)
    return kept, skips


def dedupe_audio(audio: list) -> list:
    seen = set()
    out = []
    for a in audio:
        key = f"{a.get('song', '').strip().lower()} - {a.get('artist', '').strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def format_ugc_item(item: dict) -> str:
    """Renders one opportunity: a punchy hook, then the link(s). Two buckets (repost / post_idea),
    each in one of two tiers set by tier_and_label: 'post' (real permalink) or 'lead' (no
    permalink — the editorial article + the account, so the team grabs the post themselves)."""
    item_type   = item.get("type", "repost")
    tier        = item.get("_tier", "post")
    headline    = item.get("headline", "")
    city        = (item.get("city") or "BOTH").upper()
    label       = "POST IDEA" if item_type == "post_idea" else "REPOST"
    tag         = f"→ {label}  ·  {city}" + ("  ·  LEAD" if tier == "lead" else "")
    header      = f"{tag}\n*{headline}*"
    account_url = item.get("account_url", "")
    article_url = item.get("article_url", "")

    if tier == "lead":
        # No permalink found — hand over the moment (article) + the account to grab the post from.
        parts = []
        if article_url:
            parts.append(safe_link(article_url, "article"))
        if account_url:
            parts.append(safe_link(account_url, "account"))
        links = "  ·  ".join(parts)
        return f"{header}\n  {links}"

    if item_type == "post_idea":
        lines = [header]
        for post in item.get("posts", []) or []:
            post_url = post.get("post_url", "")
            acct     = post.get("account_url", "")
            why      = post.get("why", "")
            if not post_url:
                continue
            line = f"  •  {safe_link(post_url, 'post')}"
            if acct:
                line += f"  ·  {safe_link(acct, 'account')}"
            if why:
                line += f"  — {why}"
            lines.append(line)
        return "\n".join(lines)

    # repost, post tier
    url      = item.get("post_url", "")
    link_str = f"  {safe_link(url, 'post')}" if url else ""
    acct_str = f"  ·  {safe_link(account_url, 'account')}" if account_url else ""
    return f"{header}{link_str}{acct_str}"


def format_audio_item(item: dict) -> str:
    song   = item.get("song", "")
    artist = item.get("artist", "")
    url    = item.get("url", "")
    label  = f"{song} — {artist}" if artist else song
    link_str = f"  {safe_link(url, 'listen')}" if url else ""
    return f"*{label}*{link_str}"


def build_slack_blocks(date_str: str, items: list, audio: list, forced: bool = False) -> list:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Social Opportunities  ·  {date_str}"},
        }
    ]

    if forced:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "⚠️ *forced/manual re-run* — a post already went out today, this is a manual override (FORCE_POST=1)",
            }],
        })

    # Big cultural moments up top so nobody misses them (food days, events, releases).
    today = datetime.date.today()
    moments = upcoming_calendar_moments(today)
    if moments:
        def _when(du):
            if du == 0:
                return "today"
            if du == 1:
                return "tomorrow"
            return (today + datetime.timedelta(days=du)).strftime("%a")
        radar = "   ·   ".join(f"*{m['name']}* _{_when(m['days_until'])}_" for m in moments if m["name"])
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"📅  *On the radar this week*\n{radar}"},
        })

    if items:
        blocks.append({"type": "divider"})
        for item in items:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_ugc_item(item)},
            })

    if not items:
        # There is ALWAYS something to post — an empty digest means the pipeline failed, not a
        # quiet day. Make it loud so a bad run gets noticed instead of reading as "nothing today."
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "⚠️ *Pipeline came back empty — this shouldn't happen.* "
                             "There's always something to post; check the run logs and "
                             "`data/social_skipped_log.json`."},
        })

    if audio:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Trending Audio*"},
        })
        for item in audio:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_audio_item(item)},
            })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "ResX Social Bot  ·  Powered by Claude  ·  Daily"}],
    })

    return blocks


def main():
    today = datetime.date.today()
    today_str = today.strftime("%B %d, %Y")
    today_iso = today.isoformat()
    dry_run = os.environ.get("DRY_RUN") == "1"
    force_post = os.environ.get("FORCE_POST") == "1"
    print(f"Running ResX Social Bot — {today_str}" + (" [DRY RUN]" if dry_run else ""))

    last_post = load_json(LAST_POST_FILE, {})
    already_posted_today = last_post.get("date") == today_iso
    forced_rerun = already_posted_today and force_post

    if already_posted_today and not force_post and not dry_run:
        print(
            f"Already posted today ({today_iso}) at {last_post.get('posted_at', 'unknown time')} "
            f"— skipping to avoid a duplicate post. Set FORCE_POST=1 to override."
        )
        return

    # Load permanent dedup state ("never repeat anything"): the exact post URL and exact song are
    # blocked forever; a venue can return only for a genuinely NEW moment, so we track the moments
    # we've featured (not the venue name). Retention is effectively forever.
    seen_raw = load_json(SEEN_UGC_FILE, [])
    cutoff = (today - datetime.timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    recent = [e for e in seen_raw if e.get("date", "") >= cutoff]
    seen_urls = {e["url"] for e in recent if e.get("url")}
    seen_moments = {e["moment"].strip().lower() for e in recent if e.get("moment")}
    seen_songs = {e["song"] for e in recent if e.get("song")}
    print(
        f"Loaded {len(seen_urls)} seen URLs, {len(seen_moments)} seen moments, "
        f"{len(seen_songs)} seen songs for dedup"
    )

    pinned_leads = load_json(PINNED_LEADS_FILE, [])
    skipped_log = load_json(SKIPPED_LOG_FILE, [])
    tracked = load_json(TRACKED_RESTAURANTS_FILE, [])
    print(f"Loaded {len(pinned_leads)} pinned lead(s), {len(tracked)} tracked restaurant(s)")

    print("Researching...")
    result = research_ugc(seen_urls, seen_moments, seen_songs, pinned_leads, tracked)
    opportunities = result["opportunities"]
    print(f"Model returned {len(opportunities)} candidate opportunities")

    # Trending audio: dedupe within the run, then drop anything already featured before.
    audio = [
        a for a in dedupe_audio(result["audio"])
        if f"{a.get('song', '')} - {a.get('artist', '')}" not in seen_songs
    ]

    # Pinned leads first — every one ends up kept or logged, never silently dropped.
    pinned_kept, pinned_skips, _ = resolve_pinned(
        pinned_leads, opportunities, result["pinned_rejected"], seen_urls, seen_moments, today_iso
    )
    researched = [o for o in opportunities if o.get("origin") != "pinned"]

    # Link fallback ladder (permalink → editorial-article+account lead → drop) for both sources.
    pinned_kept, pinned_link_skips = tier_and_label(pinned_kept, today_iso, source_type="pinned")
    researched, researched_link_skips = tier_and_label(researched, today_iso, source_type="researched")

    # Freshness gate (researched only — pinned leads are Georgia's own picks, exempt): drop any
    # post older than the freshness window or with an unknown date. Kills stale content like a
    # 5-week-old reel slipping in.
    researched, freshness_skips = validate_freshness(researched, today_iso)

    # Rank researched by the taste rubric (scores drive rank order only) and take the best few.
    researched.sort(key=avg_score, reverse=True)
    researched_top = researched[:DAILY_TARGET_N]

    # Model's own self-reported misses (transparency in the skipped log).
    considered_rejected_log = [
        {
            "date": today_iso, "subject": c.get("subject", ""), "url": c.get("url", ""),
            "reason": c.get("reason", "not specified"), "source_type": c.get("source_type", "researched"),
        }
        for c in result["considered_and_rejected"]
    ]

    # Diversity cap: one item per subject per digest, pinned wins any conflict.
    final_items, diversity_skips = apply_diversity(pinned_kept + researched_top, today_iso)

    new_skip_entries = (
        pinned_skips + pinned_link_skips + researched_link_skips + freshness_skips
        + considered_rejected_log + diversity_skips
    )
    print(
        f"Publishing {len(final_items)} opportunities "
        f"({len(pinned_kept)} pinned, {len(final_items) - len(pinned_kept)} researched); "
        f"{len(new_skip_entries)} skipped this run"
    )
    blocks = build_slack_blocks(today_str, final_items, audio, forced=forced_rerun)

    if dry_run:
        print("[DRY RUN] Final Slack payload (not posted, no state written):")
        print(json.dumps({"blocks": blocks}, indent=2))
        return

    # HARD RULE: never post an empty digest to the team channel. If research came back empty,
    # fail the run loudly (non-zero exit → the GitHub job shows failed, no state is written, and
    # the same-day guard isn't consumed so a re-trigger can still succeed) instead of posting a
    # warning to #social. An empty result is an ops problem to fix, never a team-facing message.
    if not final_items:
        print("⚠️  Zero opportunities to publish — REFUSING to post an empty digest to #social. "
              "Failing this run so it's visible in Actions. Check the log and social_skipped_log.json.")
        raise SystemExit(1)

    post_to_slack(blocks)
    print("Posted to #social" + (" [forced re-run]" if forced_rerun else ""))

    # Persist dedup state — only for what actually posted, retained effectively forever so nothing
    # ever repeats. We store the blockable URLs (post permalinks + editorial article, never the
    # account) plus the moment, so a venue can return only for a genuinely new moment.
    new_entries = [
        {"url": url, "date": today_iso, "subject": item.get("subject", ""),
         "moment": item.get("moment", "")}
        for item in final_items
        for url in dedup_urls_in_item(item)
    ]
    # Safety net: record the moment even for the (rare) item that posted with no blockable URL.
    new_entries += [
        {"date": today_iso, "subject": item.get("subject", ""), "moment": item.get("moment", "")}
        for item in final_items
        if item.get("moment") and not dedup_urls_in_item(item)
    ]
    new_entries += [
        {
            "url": item["url"],
            "date": today_iso,
            "song": f"{item.get('song', '')} - {item.get('artist', '')}",
        }
        for item in audio
        if item.get("url")
    ]
    keep_cutoff = (today - datetime.timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    all_entries = [e for e in seen_raw if e.get("date", "") >= keep_cutoff] + new_entries
    save_json(SEEN_UGC_FILE, all_entries)
    save_json(LAST_POST_FILE, {
        "date": today_iso,
        "posted_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    # Pinned-leads queue is consumed each run — every lead was resolved above
    save_json(PINNED_LEADS_FILE, [])

    # Skipped log: append this run's rejections, capped to a 30-day rolling window
    skip_keep_cutoff = (today - datetime.timedelta(days=30)).isoformat()
    all_skips = [e for e in skipped_log if e.get("date", "") >= skip_keep_cutoff] + new_skip_entries
    save_json(SKIPPED_LOG_FILE, all_skips)

    print("Done ✓")


if __name__ == "__main__":
    main()
