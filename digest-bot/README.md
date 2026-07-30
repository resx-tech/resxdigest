# ResX Digest Bot (`bot.py`)

> This folder contains documentation only. The actual code lives at the repo root: [`../bot.py`](../bot.py), state files in [`../data/`](../data/), workflows in [`../.github/workflows/`](../.github/workflows/).
>
> For the operating philosophy, architectural decisions, and "don't touch this without thinking" guidance, see [`CLAUDE.md`](./CLAUDE.md) in this folder.

## Purpose

Posts a curated restaurant-industry Slack digest twice a week (Mon/Fri) for the ResX team — a last-minute restaurant reservation marketplace for 25-35 year olds in NYC and London. It is explicitly framed as an **executive briefing, not a news summary**: every story is expected to answer "why should the ResX team care?" (strategic, operational, cultural, competitive, or product-relevant), and content that's merely interesting but wouldn't change what the team discusses that week is filtered out.

> **2026-07-13 update:** UGC covers and Instagram links on openings are now **correct-or-omit** — a cover must be a verified Instagram *photo* post (`instagram.com/p/…`, never a video/reel or profile) of the right restaurant, else it's omitted (deterministic gate `sanitize_opening_links` + `is_photo_post_url`, plus a `web_fetch`-based confirm step in the prompt). A "coming soon" venue that surfaced as a New Opening now gets reclassified to Watching (`looks_not_yet_open`). The editorializing "→ why care" (`so_what`) line was **cut** from openings/Industry/Culture (kept as a factual `why_it_matters` only in AI & Product); blurbs/details are factual, no hype. See `CLAUDE.md` → "2026-07-13 update".

Five sections, in order:
1. **New Openings** (NYC + London) — restaurants/hotels/bakeries/bars/members clubs that have officially opened
2. **Watching** — announced-but-not-open venues, tracked week to week until they graduate to New Openings
3. **Industry & Competitor Watch** — includes a dedicated "🎯 Competitor Watch" sub-section that mandatorily checks a named list of competitors (Dorsia, Resy, SevenRooms, Tock, etc.) for launches/expansions/funding/acquisitions/partnerships/executive hires/strategy changes
4. **City & Culture** — celebrity sightings, viral brand collabs, chef gossip, specials
5. **AI & Product** — AI/dev-tool news, each item required to answer whether it lowers costs, is worth experimenting with, or improves the team's workflow

## How it works end-to-end

`bot.py`'s `main()` runs this pipeline every invocation:

1. **Claim the run slot** (`claim_todays_run`) — writes a `{"status": "running", ...}` claim to `data/last_post.json` and commits+pushes it *before* doing any research. Git's fast-forward-only push is the atomic arbiter: if two runs (a delayed native schedule, a watchdog retrigger, a manual click) race, only one wins the push; the loser re-fetches, sees the winner's claim, and exits without ever calling the Anthropic API or Slack. See `CLAUDE.md` for why this exists.
2. **Load state**: `seen_openings.json` (permanent), `watching.json`, `pinned_inputs.json`, `skipped_items_log.json`, `seen_stories.json` (last 14 days used for prompts), `competitors.json`.
3. **Holiday + cultural-moment context** (`get_holiday_context` + `upcoming_calendar_moments`) — `CALENDAR` (holidays only, may set a special Slack header, e.g. "❤️ Valentine's Edition") plus the shared `data/calendar_moments.json` (food/drink days + one-off events like the US Open) inject prompt hints when within range, and the moments render a "📅 Coming up" callout atop City & Culture.
4. **`refresh_competitors()`** — one small Claude call asks "any new reservation/dining-membership competitors launched in the last 2 weeks?" and appends genuinely new names to `competitors.json`.
5. **`research_openings("nyc" | "london", ...)`** — two Claude+web_search calls. Each returns `just_opened` (verifiably open) and `coming_soon` items, tagged with a stable `id` (see `normalize_identity`) and `category` right at ingestion.
6. **Deterministic graduation** — restaurants in `watching.json` whose `id` matches a `just_opened` item this run are dropped from Watching (they graduated); everything else in `watching.json` is carried forward untouched. Only genuinely *new* `coming_soon` candidates (not already tracked) proceed to the next step.
7. **`research_competitor_watch()`** — a dedicated, mandatory per-name check across the tracked competitor list (see `SEED_COMPETITORS`), explicitly instructed that completeness beats brevity here.
8. **`research_industry()`** — broader industry/regulatory/business news not tied to a specific named competitor (that's Competitor Watch's job).
9. **`research_culture()`** — city culture, celebrity sightings, brand collabs, chef gossip, specials.
10. **`research_ai_product()`** — AI/dev-tool news with a mandatory `why_it_matters` field.
11. **`normalize_stories()`** — flattens all of the above into one pool of story dicts sharing common `id`/`category` fields.
12. **`process_pinned_inputs()`** — resolves anything manually queued in `pinned_inputs.json` (a raw link/description, or an already-fully-written entry) via `research_pinned_inputs()`, then applies deterministic broken-link/duplicate checks. **Every entry is guaranteed to end up either added to the pool or logged to `skipped_items_log.json` with a reason** — see `CLAUDE.md` for why this guarantee is load-bearing.
13. **`edit_and_rank()`** — the single "executive editor" LLM pass. Given the *entire* pool at once, it merges duplicate entities (a chef's opening surfaced independently as both a `new_opening` and an `industry` story becomes one), assigns each story's *final* category, drops non-pinned stories that fail the "would the team discuss this" bar, and ranks importance within category (pinned items always rank above non-pinned ones).
14. **`build_slack_blocks()`** — renders the five sections from the one final list.
15. **`post_to_slack()`**.
16. **Persist everything** (`watching.json`, `seen_openings.json`, `seen_stories.json`, `pinned_inputs.json` cleared, `skipped_items_log.json`, `last_post.json` marked `completed`) in one git commit+push, then `log_run_event()`.

If anything raises inside the `try:` block, the `except` handler marks `last_post.json` as `status: "failed"` (so a retry doesn't have to wait out the full staleness window) and re-raises, so the CI job shows red.

## Folder structure

```
bot.py                              # the entire bot — one file, no other modules
data/
  seen_openings.json                 # permanent list of featured restaurant names
  watching.json                      # tracked "coming soon" restaurants, each with a stable id
  seen_stories.json                  # 14-day dedup window / 30-day retention for news-style stories
  pinned_inputs.json                 # manually-submitted leads queue (cleared each run)
  skipped_items_log.json             # 30-day log of why a pinned input wasn't included
  competitors.json                   # grows via refresh_competitors()
  last_post.json                     # same-day claim/completion state (the anti-duplicate-post guard)
  run_log.json                       # 30-day rolling history of every run attempt
.github/workflows/
  news_bot.yml                       # the actual scheduled/manual trigger
  watchdog.yml                       # polls every 30 min, retriggers news_bot.yml if a scheduled run was missed
.claude/skills/
  run-bot/SKILL.md                   # `/run-bot` — manual workflow_dispatch trigger
  reset-openings/SKILL.md            # `/reset-openings` — clears seen_openings.json for a new season
```

## Environment variables

| Variable | Required | Set by | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | GitHub Secret | Calls `claude-sonnet-4-6` via the raw Messages API (no SDK) |
| `SLACK_WEBHOOK_URL` | Yes | GitHub Secret | Incoming Webhook URL for the digest's Slack channel (`#news`) |
| `FORCE_POST` | No | `news_bot.yml`'s `force_post` workflow_dispatch input, defaults `"0"` | `"1"` bypasses the same-day "already completed" guard (still subject to the atomic claim race) |
| `TRIGGER_TYPE` | No | Set automatically by `news_bot.yml` from `github.event_name` | `"scheduled"` or `"manual"`; becomes `"forced"` internally whenever `FORCE_POST=1` |
| `GITHUB_ACTIONS` | No | Set automatically by GitHub Actions | Gates ALL git operations (`IN_CI`) — bot.py never runs git commands unless this is `"true"`, so local/manual runs never touch a real working tree |
| `GITHUB_RUN_ID` | No | Set automatically by GitHub Actions | Used as the claim's `run_id` for traceability; falls back to `local-<timestamp>` outside CI |

## How scheduling works

- **Cron**: `15 12 * * 1,5` in `news_bot.yml` — Monday and Friday at 12:15 UTC (8:15am ET). The 15-minute offset avoids top-of-hour GitHub Actions congestion.
- **GitHub silently deprioritizes low-frequency schedules.** Confirmed directly in this repo's Actions history: both observed native `schedule` firings were over 2 and 3.5 hours late. This is *why* `watchdog.yml` exists.
- **`watchdog.yml`** runs every 30 minutes, every day (also subject to some deprioritization, but much less severe at that frequency). On a Mon/Fri, it fetches `data/last_post.json` directly from `main` via the GitHub API and checks: is there a `completed` entry for today? If not, and there's no fresh `running` claim either, it fires `gh workflow run news_bot.yml`. It deliberately reads the bot's own state file rather than GitHub Actions run status — a run can post successfully and still show "failure" in the Actions UI (e.g. a later step errors), which used to cause incorrect retriggers.
- **Manual trigger**: `workflow_dispatch` with an optional `force_post` boolean input.

## How to run locally

Set `ANTHROPIC_API_KEY` and `SLACK_WEBHOOK_URL`, then `python bot.py`. Since `GITHUB_ACTIONS` won't be set locally, `IN_CI` is `False` and every git operation (`git_commit_and_push`, the claim itself) silently no-ops and returns success — so a local run **will actually call the real Anthropic API and post to the real Slack channel**, it just won't try to commit anything back to git. There is no local dry-run flag for this bot (unlike `social_bot.py`'s `DRY_RUN=1`) — be careful running it locally against production Slack.

Per project convention, real end-to-end verification happens via GitHub Actions, not locally. Locally, the productive thing to do is sanity-test the many **pure functions** with fake data and no API key — `normalize_identity`, `build_slack_blocks`, `_finalize_pinned_story`, `_is_pre_resolved_pin`, `normalize_stories`, `_parse_editor_response`'s fallback path, etc. — by importing `bot` and monkey-patching `call_anthropic`/`post_to_slack`/`refresh_competitors` with fakes, exactly as was done throughout this bot's development.

## How to manually trigger it

- `/run-bot` (Claude Code skill) — runs `gh workflow run news_bot.yml` and shares the run URL.
- Or directly: `gh workflow run news_bot.yml` (optionally `-f force_post=true` to override the same-day guard).
- Or the Actions tab → **ResX News Bot** → **Run workflow**.

## How to deploy

There's no build step. "Deploying" is just pushing to `main` — the next scheduled or manual run picks up the new code automatically. Required one-time setup: add `ANTHROPIC_API_KEY` and `SLACK_WEBHOOK_URL` as repository secrets (Settings → Secrets and variables → Actions).

## Common failure modes

- **A research call's JSON fails to parse.** Every `research_*` function catches parse errors and returns an empty list for that section — a bad LLM response degrades that section's content for one run, it doesn't crash the bot.
- **`edit_and_rank`'s response fails to parse.** Falls back to a deterministic pass-through (`_parse_editor_response`): keep each item's already-assigned category, rank by original research order. The digest still posts, just without merge/relevance-filtering/re-ranking for that run.
- **A git push loses the race.** `git_commit_and_push` retries up to 5 times with backoff, re-fetching and resetting to `origin/main` between attempts. If it still fails after 5 tries, a `WARNING` is printed but the run is **not** marked failed (the Slack post already succeeded) — the next run may not see this run's state changes, though.
- **The Anthropic API errors** (rate limit, timeout, etc.) — uncaught inside `call_anthropic`, propagates up through whichever `research_*` call was in flight, caught by `main()`'s outer `except`, which marks the claim `status: "failed"` and re-raises (CI job shows red, a retry doesn't have to wait the full 15-minute staleness window).
- **Slack webhook errors** — `post_to_slack` catches `HTTPError`, prints the response body, and re-raises (same failure path as above).
- **Zero content in a section on a given run.** Intended, not a bug — the executive-relevance filter in `edit_and_rank` and each research prompt's own "leave it out" instructions mean some sections can legitimately be empty.

## Troubleshooting

- **`data/run_log.json`** — 30-day rolling structured history of every run attempt: `{date, timestamp, trigger, outcome, detail}` where outcome is `skipped`/`completed`/`failed`. Start here to see what actually happened.
- **`data/last_post.json`** — current claim state. `status: "running"` with a stale `started_at` (>15 min) means a run crashed without reaching the `except` handler's cleanup (rare, but possible on e.g. an OOM-killed process); the next run will treat it as abandoned and reclaim.
- **`data/skipped_items_log.json`** — every pinned input that didn't make a digest, with a machine-readable `reason` (`broken_link` / `duplicate` / `clearly_irrelevant` / `not_addressed_by_model`) and a human `detail`.
- **GitHub Actions run logs** — `main()` prints a step-by-step trace (`"Researching NYC openings..."`, `"Editing & ranking N candidate stories..."`, per-block text previews before posting, etc.) — copious by design so a failed run's logs are self-explanatory without needing to reproduce locally.
- **A restaurant appears in the wrong place, or twice** — check `data/watching.json` for its `id` (computed by `normalize_identity`); if two entries differ only by an unexpected city suffix or punctuation, that's the identity-matching logic to inspect first.

## How state / deduplication works

- **`seen_openings.json`** — a flat, **permanent** list of restaurant names ever featured as a New Opening. Never expires. To reset for a new season, use `/reset-openings` or clear it to `[]`.
- **`watching.json`** — coming-soon restaurants. Each entry has a stable `id` (`normalize_identity(name, city)` — lowercased, punctuation-stripped, common city suffixes removed, but parenthetical qualifiers like "(Williamsburg)" deliberately preserved). Graduation to New Openings is matched by `id`, not by fuzzy name equality — this is the fix for the bug where a restaurant appeared in both sections because "Dishoom" didn't match "Dishoom NYC."
- **`seen_stories.json`** — news-style stories (industry/culture/ai_product only — openings have their own permanent/tracked dedup and don't also live here). Last 14 days are fed into research prompts as "already covered — don't just re-report these unless there's a materially new development" (a funding rumor becoming an official round, a confirmed date after a teaser, etc.). 30-day retention on disk.
- **`pinned_inputs.json`** — the manually-submitted leads queue. Consumed (cleared to `[]`) every run, because `process_pinned_inputs`'s safety net guarantees every entry is resolved-or-logged before that happens.
- **`skipped_items_log.json`** — 30-day rolling log of pinned-input rejections with reasons.
- **`competitors.json`** — seeded from `SEED_COMPETITORS` in `bot.py`, grows via `refresh_competitors()`'s LLM discovery. Never shrinks or dedupes fuzzy variants.
- **`last_post.json`** — the anti-duplicate-post mechanism. See `CLAUDE.md` for the full "why."

## Future improvements / TODOs

*(Noted, not fixed, per the documentation-only scope of this pass.)*

- **`social_bot.py` doesn't have this bot's claim-based git-race locking.** It has an equivalent same-day guard (`data/last_social_post.json`), but the git commit happens in `social_bot.yml`'s shell step with no retry-on-race logic — the same duplicate-post bug class fixed here (see `CLAUDE.md`) is still latent there.
- **No automated test suite.** Verification has been manual: local sanity-checks of pure functions with fake data, then one real run via `/run-bot`. A lightweight `pytest` suite for `normalize_identity`, `_finalize_pinned_story`, `build_slack_blocks`, `_parse_editor_response`'s fallback, etc. would catch regressions faster.
- **`normalize_identity`'s parenthetical-qualifier handling is a deliberate compromise**, documented in its own docstring — it can't distinguish "a dropped disambiguator" from "a genuinely distinct second location of an expanding chain" on its own; it relies entirely on `edit_and_rank`'s semantic judgment for that nuance.
- **Inconsistent link-verification strictness**: `research_openings`'s own links use the strict `verify_url` (any status ≥ 400 fails, including a 403 from a bot-blocking site); pinned-input links use the more lenient `check_broken` (only a confirmed 404/410 fails). This asymmetry is intentional (documented in `check_broken`'s docstring) but could be confusing to a future reader; worth a comment cross-reference or eventual consolidation.
- **`competitors.json` only grows.** No mechanism to dedupe near-duplicate names (e.g., a seeded "OpenTable" plus a later-discovered "Open Table Inc.").
- **`CALENDAR` in `bot.py` is hand-maintained** and will need periodic review/extension for new observances; the July 4th special-header year math is hardcoded relative to 1776.
