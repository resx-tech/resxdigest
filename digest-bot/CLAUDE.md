# CLAUDE.md — ResX Digest Bot (`bot.py`)

This is the operating manual for `bot.py`. Read this before making *any* change to it — a lot of what looks like unnecessary complexity here is a direct fix for a real, previously-observed bug. See [`README.md`](./README.md) in this folder for the operational/how-to-run side of things.

## Mission

Give the ResX team an **executive briefing**, not a news summary, on the restaurant/hospitality industry, twice a week. Every story exists to answer one question: *why should the ResX team care?* If a story is merely interesting but wouldn't change what the team would actually discuss internally that week, it doesn't belong — even if it's true and well-written.

## 2026-07-13 update — read this first

Direct feedback (Georgia) on wrong opening links + slop drove these changes:

- **UGC covers & Instagram links are now correct-or-omit, enforced deterministically.** `cover_image_post` and `instagram_url` are URLs the LLM returns, rendered as Slack links that Slack unfurls into the preview image — so a wrong-but-live URL showed the wrong restaurant, and `verify_url`'s bare HTTP-200 check couldn't catch it (nor Instagram's soft-404s, nor a video). New: `is_photo_post_url` (a cover must be `instagram.com/p/…`, a **photo**, never `/reel/` `/tv/` video, never a profile) and `sanitize_opening_links` (clears any cover that isn't a live photo-post; IG profile links use lenient `check_broken`, not strict `verify_url`, which 403-clears live IG *and* 200-passes dead handles; website/source_url keep strict `verify_url`). Applied at ingestion in `research_openings` **and** as a final pass on every `new_opening`/`watching` item after `edit_and_rank`. The prompt now says: only include a cover you've **web_fetch-confirmed** shows THIS restaurant's food, else omit — a missing cover is fine, a wrong one is unacceptable. Correctness ("right restaurant") can't be HTTP-verified, so it's prompt + omit-when-unsure; the deterministic layer guarantees no video/profile/dead cover ever renders.
- **`web_fetch` added** to `call_anthropic` (basic `web_search_20250305` + `web_fetch_20250910`, `anthropic-beta: web-fetch-2025-09-10`) so the model can open a source to confirm a link belongs to a restaurant. Basic variants deliberately (the `_20260209` "dynamic filtering" ones run code execution → pause_turn/container pain — learned in `social_bot.py`). Also added a socket timeout + graceful `""` return so a hung call degrades one section to empty instead of hanging the run (the digest previously had no timeout).
- **Categorization backstop `looks_not_yet_open`** reclassifies a `just_opened` item whose date/blurb says "coming soon"/"opens [future]" into Watching (a coming-soon place had rendered as a New Opening). The prompt's "JUST OPENED" bar is also tightened to *verifiably open NOW*.
- **The displayed `so_what` "→ why care" line was CUT** from openings, Industry, and City & Culture (per direct feedback — it produced AI-slop like *"exactly the collab-of-the-month energy the audience is already discussing"*). `format_opening_item`/`format_news_items` no longer render it; `blurb`/`detail` are tightened to factual-no-hype. **This reverses the earlier decision that added `so_what`** (see feedback history) — but only the *displayed editorializing line* is gone; `edit_and_rank`'s executive-relevance *selection filter* (what to include) stays, and **AI & Product keeps its functional `why_it_matters`** ("does it lower costs / worth testing"), which is not slop.

Where the sections below conflict with this, this note wins.

## What success looks like

- Nobody on the team ever says "wait... how did we miss that?" (a real, repeatedly-stated bar from the user).
- No restaurant, competitor move, or story ever appears twice in one digest, or across two consecutive digests, without a genuinely new development.
- A manually pinned lead (a link, a name, a description Georgia hands the bot directly) **always** ends up in the digest or is explained in `skipped_items_log.json` — never silently dropped.
- The bot runs automatically Mon/Fri without manual triggering, and never posts twice for the same scheduled window.
- Content reads like it came from someone chronically online and plugged into the industry — specific names, numbers, and insider details, never generic trend commentary.

## What the bot should optimize for

1. **Signal over completeness.** It is completely fine — expected, even — for a section to be thin or empty on a quiet week. Padding with weak content to "fill out" a section is worse than an empty section.
2. **Primary sources over secondary coverage.** A restaurant/brand/creator account's own post is a valid, citable source on its own; the bot is explicitly instructed not to wait for editorial write-ups to confirm something is real (see "Prompting philosophy" below).
3. **Specificity over genericity.** Every prompt bans vague trend language and demands named people/places/numbers.
4. **Deterministic guarantees over LLM good behavior, wherever a hard requirement exists.** Anywhere the product requirement is "this must never happen" (duplicate posts, a silently-dropped pinned lead, a restaurant stuck in two sections), there's a non-LLM check enforcing it, not just a prompt instruction.

## Important architectural decisions and why they were made

### One flat pool + a single "editor" pass, instead of N independently-researched sections
Earlier, each of ~7 sections was researched by a separate, mutually-blind Claude call. A restaurant opening could surface independently via the openings research *and* via an industry/hospitality angle, with zero cross-awareness — this was the direct cause of restaurants appearing in both New Openings and Watching. The fix: every research call (`research_openings`, `research_competitor_watch`, `research_industry`, `research_culture`, `research_ai_product`, plus pinned inputs) feeds into one flat pool (`normalize_stories`), and a single `edit_and_rank` call sees the *entire* pool at once — it's the only place a cross-domain duplicate can actually be caught and merged, because it's the only place with full visibility.

### Claim-before-work locking, not "check date, do work, then save"
The naive guard (check `last_post.json`'s date, do all the research, post, *then* save the guard) has a fatal flaw: the guard's own write only survives if the final `git push` succeeds, and that push races against every other concurrent run. This was **confirmed directly in this repo's Actions logs**: two separate days had real duplicate Slack posts, both traced to the guard-write's git push losing a race against another run that started around the same time (a delayed native schedule + a watchdog retrigger + a manual click, in various combinations). The fix is `claim_todays_run`: commit and push a `{"status": "running"}` claim **before** any research begins, using git's fast-forward-only push as the atomic race arbiter. The loser re-fetches, sees the winner's claim, and exits before ever calling the Anthropic API or Slack. **Do not revert this sequencing** — reopening "research first, claim later" reopens the exact bug that motivated this.

### Deterministic graduation (`normalize_identity`) underneath the LLM's own judgment
`edit_and_rank` is asked to recognize when a candidate is the same restaurant as something in `watching.json`, even if renamed. But that's a *soft* guarantee — an LLM can get it wrong on a given run. So there's also a **deterministic** check in `main()`: every `watching.json` entry has a stable `id`; any `just_opened` item whose `id` matches gets deterministically removed from Watching regardless of what the LLM does. This two-layer approach (LLM for the fuzzy cases, deterministic ID match for the clean case) is why the "must disappear from Watching" requirement is actually guaranteed, not just likely.

### Two different link-verification strictness levels, on purpose
`verify_url` (strict: any status ≥ 400 fails) is used for the bot's *own* researched openings links. `check_broken` (lenient: only a confirmed 404/410 fails) is used for *manually pinned* links. This is deliberate, not an oversight: Instagram/TikTok routinely return 403 to scripted HEAD requests even for perfectly real, live content (confirmed directly — a real Bake Magazine article once 403'd a plain `curl` request). A link Georgia is personally vouching for shouldn't be rejected because of a bot-blocking heuristic on the other end.

### Pinned inputs bypass the scoring/relevance filter and always outrank organic content
"The bot must respect manually provided source links — that should never happen [being ignored]" is a direct, hard product requirement. `edit_and_rank`'s executive-relevance filter (which *can* drop a non-pinned story) explicitly exempts anything with `origin: "pinned"`, and pinned items are explicitly ranked above all non-pinned items in their category. This is enforced via prompt instruction (the LLM is told this rule) *and* the pinned pipeline (`process_pinned_inputs`) never routes a pinned item through the relevance filter in the first place — it has its own accept/reject logic (broken/duplicate/clearly-irrelevant only).

### Tiered Competitor Watch (core every run + rotating long tail)
Georgia's full competitor list is ~89 names (`data/competitors.json`, kept in sync with `SEED_COMPETITORS`) — far too many for the model to meaningfully "check EACH by name" in one call. So `competitor_watch_list(all, today)` builds the per-run watch list from two tiers: `CORE_COMPETITORS` (the most direct threats — reservation platforms + top secondary-market/exchange players + Ambl) checked **every run**, plus a rotating chunk of the long tail (`TAIL_CHUNK_SIZE`, ~15). The rotation is **stateless** — derived from the ISO week + Mon/Fri, advancing one chunk per run — so the whole tail is covered every ~5 runs with no state file. To add a competitor: put it in `data/competitors.json` (and `SEED_COMPETITORS`); to make one high-priority, add it to `CORE_COMPETITORS`.

## Things that should never be changed without careful consideration

- **The order of operations in `main()`**: claim → research (including deterministic graduation, computed *before* the pool is built) → process pinned inputs → build pool → `edit_and_rank` → render → post → persist state → commit. Moving the claim later, or persisting state before `post_to_slack` succeeds, reopens duplicate-post risk.
- **`process_pinned_inputs`'s safety net** (every `pinned_inputs.json` entry ends up in `resolved_stories` or `skip_entries`, matched via `_pin_input_key`, cross-checked against `matched_inputs`). This is the literal fix for "the bot ignored a link I gave it." Don't refactor this into something that could silently drop an entry if the LLM's response is malformed or incomplete.
- **`normalize_identity`'s decision not to strip parenthetical qualifiers.** It would be tempting to "fix" this to more aggressively normalize names, but doing so without also strengthening `edit_and_rank`'s semantic check risks silently merging two genuinely distinct locations of an expanding restaurant group.
- **The `IN_CI` gate around every git operation.** Without it, running `bot.py` locally (or in some other automated context) would attempt real `git commit`/`git push` calls against whatever happens to be checked out.
- **`CLAIM_STALE_MINUTES = 15`.** Shorter reopens race conditions between a legitimately still-running job and a "stale" reclaim; longer means a genuinely crashed run blocks all retries for that long.
- **The executive-relevance filter's pinned-item exemption in `edit_and_rank`'s prompt.** Removing it would silently reintroduce "pinned content gets dropped," just via a different mechanism than the one already fixed.

## Known limitations

- No automated test suite — verification is manual (local pure-function sanity checks + one real `/run-bot` run).
- `edit_and_rank` and every research call rely entirely on LLM discipline plus a lightweight HTTP status check for link accuracy — there's no way to actually render/verify Instagram or TikTok content server-side.
- `competitors.json` only grows; no dedupe of near-duplicate names.
- `CALENDAR` (holiday/food-day awareness) is hand-maintained and will silently go stale for new observances.
- `social_bot.py` has an architecturally identical same-day-guard pattern to what this bot used to have, *before* the claim-based locking fix — it has not received the same fix (see that bot's own `CLAUDE.md`). Don't assume a fix here automatically applies there.

## Common user feedback we've received

These are paraphrased from actual product-owner (Georgia) feedback that directly shaped the current design — useful context for *why* things are the way they are, not just *what* they are:

- *"It only runs when I manually trigger it, even though it's scheduled, and sometimes it posts twice."* → Root-caused to GitHub's low-frequency-schedule deprioritization plus a git-push race in the same-day guard. Led to `watchdog.yml` (already existed) being fixed to check actual post state instead of Actions run status, and to the claim-before-work locking rewrite in `bot.py`.
- *"We've overcomplicated the sections... restaurants are appearing under both New Openings and Watching. That should never happen."* → Led to the full research → pool → `edit_and_rank` → render redesign, plus `normalize_identity`.
- *"I explicitly provided this [Instagram] post as a new opening. The digest ignored it. That is a failure."* → The prior pinned-content mechanism (`pinned_stories.json`) only accepted fully hand-written entries — a bare link had no path to ever be processed. Led to `pinned_inputs.json` + `research_pinned_inputs` + `process_pinned_inputs`'s safety net.
- *"I want the digest to feel less like a news summary and more like an executive briefing. Every story should answer: why should the ResX team care?"* → Led directly to the `_executive_relevance_instruction()` block, the exclusion logic in `edit_and_rank`, and the addition of a `so_what` field to openings/watching (which previously had none).
- *"We're missing things that are already everywhere in the hospitality ecosystem"* (with concrete named examples: an RH London opening, a Myka x Losers froyo collab, Levain's Cookie Milk Latte, a Dorsia city expansion) → Led to the source-priority reordering (restaurant/creator accounts *before* newsletters/publications) and the dedicated `research_competitor_watch`.
- *"I want the bot to become much more responsive to manual guidance... pinned inputs should override normal discovery and ranking."* → Led to pinned items' relevance-filter exemption and top-of-category ranking in `edit_and_rank`.

## Prompting philosophy

- **State the bar explicitly, as literal criteria** — not "be selective," but a numbered checklist (e.g. the executive-relevance instruction, the tier-priority rules in `research_competitor_watch`). Vague quality instructions produce vague filtering; explicit criteria produce consistent filtering.
- **Worked examples, including a "bad" one.** Several prompts include an explicit bad-vs-good pair (e.g. `research_pinned_inputs`' link-preservation instruction, the social bot's carousel example). This measurably improves adherence versus describing the rule abstractly.
- **Link accuracy over coverage, repeated at every layer.** "Only cite a URL you actually retrieved, never construct or recall one from memory, when in doubt drop it" appears in `research_openings`, `research_pinned_inputs`, and (in the social bot) `research_ugc`. This is deliberately redundant rather than stated once — link fabrication is treated as the single worst failure mode across both bots.
- **Long, explicit prompts, not terse ones.** The prompts in this file are large and repetitive by design. This isn't unpolished — shorter, vaguer prompts were tried first (see git history) and produced worse editorial judgment.

## Editorial philosophy

- Quality over quantity, enforced structurally where possible (the executive-relevance drop in `edit_and_rank`), not just requested in prose.
- A story's category is about **where a team member would most usefully look for it**, not where it was discovered — a chef's new restaurant surfaced via an industry-angle search still belongs in New Openings if it's actually opened.
- Manual/pinned input is *never* second-guessed on relevance — only on being broken, an exact duplicate, or (for raw leads only) clearly unrelated to the business.
- Preserve specificity relentlessly. Merging duplicate stories in `edit_and_rank` explicitly instructs "combine distinct facts from both if each adds something the other lacks" rather than picking one and discarding detail.

## How to safely make changes

1. **Read the surrounding prompt language before editing a prompt.** Match its voice and rigor — these prompts are intentionally verbose and example-heavy; a terse replacement will likely regress output quality even if it "looks cleaner."
2. **If you add or rename a category**, update all of: the category list inside `edit_and_rank`'s prompt (the assignment rules, priority order), the filtering logic in `build_slack_blocks`, and `_story_entries`' category filter (currently only `industry`/`culture`/`ai_product` persist to `seen_stories.json` — openings have their own separate dedup).
3. **If you touch `main()`'s sequencing**, preserve: claim before research; deterministic graduation computed before the pool is built; pinned inputs processed and folded into the pool before `edit_and_rank`; all state persisted only *after* `post_to_slack` succeeds.
4. **Never remove a safety-net loop** (`process_pinned_inputs`'s unmatched-input check, `claim_todays_run`'s retry loop) to "simplify" — they exist because a previous version of this exact logic silently failed in that exact way.
5. **If you change scoring/filtering criteria that affects what gets included**, consider whether pinned items need an explicit exemption restated — it's easy to add a new filter and forget the exemption that took real feedback to add.

## How to test changes

Per project convention, real end-to-end testing happens via GitHub Actions, not locally (`/run-bot` skill, or `gh workflow run news_bot.yml -f force_post=true`). Before that:

- **Sanity-test pure functions locally** with fake data and no API key: `normalize_identity`, `_finalize_pinned_story`, `_is_pre_resolved_pin`, `_pin_input_key`, `normalize_stories`, `build_slack_blocks`, `_parse_editor_response`'s fallback path. Import `bot`, monkey-patch `call_anthropic`/`post_to_slack`/`refresh_competitors`/individual `research_*` functions with fakes, and run `bot.main()` end-to-end against fake responses — this was the actual development/verification pattern used throughout this bot's history and catches wiring bugs (e.g. a watching-list entry being silently dropped) without spending API calls.
- **Always back up `data/*.json` before a local test run** that calls `bot.main()` — even with `IN_CI` gating git operations, `save_json` still writes real local files, and a test run will overwrite your real `data/watching.json` etc. with test content unless you restore from backup afterward.
- **Test the specific bug you're fixing directly**, not just "does it run." E.g. when the graduation fix was built, the actual test seeded `watching.json` with an entry missing an `id` field (simulating a pre-migration file) and confirmed a differently-named `just_opened` item still correctly graduated it.

## Files that are important to understand before editing

- **`bot.py`** — everything lives here, no other modules.
- **`data/watching.json`** and **`data/pinned_inputs.json`** — read these to understand the *real* shape of state, not just the schema in a docstring.
- **`.github/workflows/news_bot.yml`** and **`watchdog.yml`** — the two-workflow relationship (watchdog reads the bot's own claim file, not Actions run status) is easy to break by editing one without the other.

## Assumptions future Claude instances should know

- **"Georgia" is a real name baked directly into prompt text** (e.g. `research_pinned_inputs`: "Georgia has manually submitted these leads"), not a placeholder. If the primary stakeholder changes, these references are worth revisiting, but their presence is intentional, not a bug.
- **ResX = a last-minute restaurant reservation marketplace for 25-35 year olds in NYC and London.** Every prompt's audience/relevance calibration assumes this specific business and demo.
- **"Zero external dependencies" is a deliberate, load-bearing constraint** — stdlib only (`os`, `json`, `re`, `urllib.request`, `subprocess`, `datetime`, `pathlib`). Don't introduce a pip package or the Anthropic SDK without raising it explicitly; this has been a consistent constraint across this bot's entire history.
- **`bot.py` and `social_bot.py` are fully independent** — no shared imports, no shared code. They've accumulated some inconsistent conventions over time (see Known Limitations). Never assume a fix made in one automatically applies to, or is even compatible with, the other — check its own `CLAUDE.md`.
- **This bot runs Mon/Fri; the social bot runs Tue/Thu** (both 2x/week as of 2026-07-21). GitHub's schedule deprioritization is worse for infrequent schedules — this bot has `watchdog.yml`, the social bot still doesn't.
- **Cultural moments live in `data/calendar_moments.json`, shared with `social_bot.py`** (`upcoming_calendar_moments`) — `fixed`/`movable` recurring food/drink days plus one-off dated `moments` (events/releases like the US Open, EEEEEATSCON, a premiere; year-specific, update as needed). Add one once and BOTH bots pick it up. `CALENDAR` in `bot.py` is now holidays/observances ONLY (the ones that can set a `special_header`) — do **not** add food days there, they'd be ignored. The digest renders a **"📅 Coming up"** callout at the top of the City & Culture section (shows even when there are no culture stories). Shared as *data*, not code; the bots stay independent modules.
