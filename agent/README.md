# agent — the Admiry marketing agent (Muse-first connector)

FastAPI process with a stateless FastMCP endpoint at `/mcp`, its own Postgres database
(`AGENT_DATABASE_URL`, default `postgresql:///bidx_agent`), and the owner-facing pages.
Spec: `docs/superpowers/specs/2026-09-24-admiry-marketing-agent-design.md` (§4, §5, §9 A1).

## Run locally
```bash
PY=.venv/bin/python      # the repo's venv: `make sync` installs agent/requirements.txt
createdb bidx_agent 2>/dev/null || true
$PY -m agent          # http://localhost:8097/health, /mcp, /connect/docs
```

## Tests
```bash
$PY -m pytest agent/tests -q     # dedicated DB: AGENT_TEST_DB_URL (default postgresql:///bidx_agent_test)
```

## Operate
```bash
$PY -m agent.keys mint --class platform --label ops                      # prints a platform key once
$PY -m agent.keys mint --class business --business biz_x --scopes read,manage,leads --label muse
AGENT_KEY=ak_... bash agent/smoke.sh http://localhost:8097               # health, docs, the skill, the public tools, get_marketing_state
$PY -m agent.probe https://agent.dev.adx.admiry.ai --key-file F --walk   # what an assistant sees, as dated rows: 401 + metadata,
                                              # initialize/tools/prompts/resources, get_marketing_state, the dental walk; --oauth signs in
                                              # through the browser (DCR + PKCE, paste-back); --oauth --rotation-check spends
                                              # the refresh token twice; --handoff-file H continues a website session (runbook docs/ops/muse-connector-validation.md)
$PY -m agent.tick proposal-expire            # jobs: proposal-expire, ads-link-sync, spend-guard, consult-expire,
                                              # ads-launch, ads-sync, page-check, email-retry, support-mail,
                                              # conversion-upload (hourly), claim-expire, session-expire, oauth-prune,
                                              # email-purge (daily), wallet-settle (daily at 14:27 UTC),
                                              # hosting-minimum (daily at 14:47 UTC); --shard i/N
$PY -m agent.ops verify biz_x                 # early access: mark a business verified and make its setup key a lasting
                                              # business key (a linked Google Ads account, or a prepaid business's first payment, verifies by itself);
                                              # audited as ops:cli; remains the early-access verification alongside Google sign-in
$PY -m agent.ops proposals biz_x              # open proposals: id kind class expires_at summary
$PY -m agent.ops issue-code pr_x              # print a fresh approval code for a spend proposal (read it to the owner);
                                              # now a fallback — the owner normally sees the code on /plan/{id} after sign-in
$PY -m agent.ops ads-link biz_x 123-456-7890 --status active   # manual link mode: after sending the invitation from
                                                               # Admiry's manager account and seeing it accepted;
                                                               # active verifies (pending | refused record only)
$PY -m agent.ops refund biz_x 250 --note "closing"   # refund unspent balance to the cards that paid it (most recent
                                                     # payment first; refused above the balance); a refund Stripe made
                                                     # but not yet recorded is recorded first, and then nothing new is
                                                     # refunded; needs STRIPE_SECRET_KEY
$PY -m agent.ops adjust biz_x -25.50 --note "late Google credit"   # prepaid businesses: a signed correction the owner reads
$PY -m agent.ops email-reply et_x --text-file reply.txt   # answer an e-mail thread handed to a person, as Admiry;
                                                          # a ticket's thread: the ticket takes the reply
                                                          # and closes once sent
$PY -m agent.ops google-ads-auth              # once: sign in with Admiry's Google Ads login, paste back the address,
                                              # the refresh token is printed (store it as GOOGLE_ADS_REFRESH_TOKEN)
$PY -m agent.ops ads-probe 123-456-7890 --validate-campaign   # operator pre-flight on an account Admiry's manager
                                                              # account manages: prints seven account fields
                                                              # (currency, time zone, tracking id, call reporting,
                                                              # …); --validate-campaign also sends one validateOnly
                                                              # mutate (a paused budget + Search campaign, the
                                                              # launch's own shape) and prints "validate: ok"; no
                                                              # database, no audit row, not gated by the switch
$PY -m agent.ops nb-probe [--create-account NAME [--company C] [--category CAT]]   # M1: the ad accounts of Admiry's
                                              # NewsBreak Organization; --create-account makes one (LEADGEN-LOCAL, the
                                              # trade's industry: CAT, default fallback; companyName C, default NAME)
                                              # and prints NewsBreak's answer -- an account NewsBreak cannot delete; no
                                              # database, not gated by the switch, never the token
$PY -m agent.ops nb-reconcile biz_x 2026-10-01   # a closed day of the account's calendar: NewsBreak's HOURLY report,
                                              # metric_daily and the balance's spend entry in whole cents, exit 0 only
                                              # when the three agree; a fourth line, the lifetime cross-check, is
                                              # informational
$PY -m agent.ops nb-account-ready biz_x       # once an account's trouble is fixed at NewsBreak: clears its wait, and a
                                              # spend check (NewsBreak's total recorded as the last cap when above it)
$PY -m agent.ops geo-refresh                  # rebuild agent/data's two ZIP catalogs from their public sources
$PY -m agent.ops hosting biz_x                # A3p-3: the hosting minimum as the daily job judges the business today
$PY -m agent.ops hosting-exempt biz_x --reason "the pilot"   # hosting stays free whatever its ads spend (P18);
                                              # a running countdown is cleared without an e-mail; --clear ends it
$PY -m agent.ops stock-load --files /tmp/stock-files   # A3p-6: the stock photo library into storage, once per box
$PY -m agent.ops site-preview biz_x --sample roofer --style solid [--accent #1D4ED8] [--words model] [--days 7]
                                              # A3p-6: one sample in one style as the business's bundle preview (needs
                                              # AGENT_PAGES_DOMAIN); prints the address and token, "loads N (sizes),
                                              # partial yes|no" (with the pages not opened), the speed scores; exit 1
                                              # with the problems on a template fault or the renderer down
$PY -m agent.ops site-gallery biz_x [--days 7]   # A3p-6: the six (roofer: solid, clean, bold; dentist: calm, warm,
                                              # boutique), then one index bundle with their screenshots and the six
                                              # under /<sample>-<style>/; prints "gallery <address>" last;
                                              # --samples hvac,plumber,electrician builds a trade's own (2026-10-08)
$PY -m agent.ops site-looks biz_x --out agent/static/sites/looks   # A3p-6 batch 2: the gallery's six phone screenshots as the looks
                                              # an owner picks a site by (390 px wide JPEGs); all six or none, exit 1 without a
                                              # live gallery; then commit the six files
AGENT_LLM_LIVE=1 $PY -m pytest agent/tests/test_llm_live.py -q   # two Muse Spark calls; cost unmeasured — check dev.meta.ai
```

## Layout
- `app.py` FastAPI process: `/health`, `pages.py`'s `/` (the homepage) and `/plan/{id}`, `home_api.py`'s router
  (`/static/*`, `POST /api/turn`, `/api/claim`, `/api/handoff`, `/api/csrf`), the `connect.py` router
  (`/connect/{docs,llms.txt,tools.json}`, rendered from the public surface's registry, and `/connect/skill.md` and
  `/connect/references/<file>`, the skill's files), then the FastMCP app mounted last
  at `/mcp` (stateless streamable HTTP, JSON responses; `Authorization: Bearer ak_…` or `X-API-Key`).
- `home_api.py` the homepage (`agent.admiry.ai/`) as an in-process client of the registered commands
  (`auth.with_headers`, no loopback HTTP): one action per `POST /api/turn` (`start`, `answer`, `say`,
  `recommend`, `adjust`, `state`), the visitor's draft key in the `admiry_visit` cookie, the sign-in wall
  (`AGENT_ANON_TURNS`) and the hand-off token to Muse/Claude/ChatGPT; every home request, once the page knows its
  business, names it (`X-Admiry-Business`); a write for another business is refused with a reload sentence (B0
  follow-up R1);
  `templates/home.html` + `static/home.js`
  + `static/home.css` (with the shared header, `base.css` and `site.js`, below) are the whole page (no build step,
  no framework); `static/channels.js`, loaded before `home.js`, draws the channels for it: the plan card's rows, a
  budget level's split and the channels card (M0), and the picture a channel shows with the ad (M1: `ads_preview`'s
  `image` and `image_note`, which `plans.cards()` sets for a plan funding NewsBreak; `/plan/{id}` and the MCP plan view
  show it too); `static/sites.js`, loaded after `channels.js`, draws an industry site's card (A3p-6 batch 2, under `sites/`
  below).
- `config.py` environment settings; `db.py` autocommit connections + idempotent `schema.sql` apply.
- `mcp_app.py` command registry (`agent_tool(cls=, scope=, approval=)`) and the audited, idempotent `run()` every command uses.
- `envelope.py` the `{data, say, next}` shape every tool answers in; `consult.py` + `consult_trees/` the consultation
  question trees and session state; `tools_consult.py` `start`'s follow-on commands `answer` / `recommend` / `adjust`;
  `assistant_skills/admiry-marketing/` the one guide assistants read, served by `assistant_skill.py` (at
  `/connect/skill.md`, `/connect/references/<file>` and as `skill://` resources).
- `surface/` the public MCP surface (spec 2026-10-01): `gate.py` (what assistants see, the retired names, the
  version), `registry.py` (the public tools and the commands behind them), `where.py` (what a public tool reads about
  the business before it picks its command, without writing a row), `actions.py` (turns the tool names in a command's
  `next` into public names), the five tool modules (`context`, `plan`, `run`, `outcomes`, `supporting`), `told.py`
  (prompts, resources), `words.py` (the instructions), `contract.py` (the golden contract). Every other `tools_*.py`
  holds commands: registered, audited, called in process by the homepage and by the surface, hidden from assistants
  except three that are public as they are (`update_lead`, `add_lead`, `contact_support`).
- `tools_state.py`, `profile.py`, `funnel.py` the reads the surface is built on: where the business stands, its
  profile (services with ids, one location, offers), the funnel (`setup.checklist`, `changes` and `next` from the
  list's first blocking item since spec 2026-10-11).
- `keys.py` / `auth.py` keys (class × scopes, SHA-256, 24 h drafts) and caller resolution with rate limits.
- `audit.py` append-only `agent_action`; `tick.py` + `jobs.py` scheduled jobs with the `job_run` ledger.
- `benchmark.py` Appendix C seed (and, since M0, the channels spec's Appendix B rows per channel) and the
  deterministic lead estimate; `schema.sql` the spec §4.4 tables (and, appended, every later spec's).
- `models.py` + `llm.py` Business Knowledge / Plan shapes and the generator on Meta's Model API (Chat Completions +
  `response_format` json_schema over httpx; `AGENT_LLM_MODEL`, default `muse-spark-1.3-contributor` — the Contributor
  tier, on which Meta may train on prompts and completions: user decision 2026-09-25, disclosed in the privacy policy).
- `crawl.py` website reader (public hosts, ≤20 pages); `places.py` Google Maps lookup (`GOOGLE_PLACES_API_KEY`, `AGENT_PLACES_DAILY_BUDGET`).
- `business.py`, `plans.py`, `proposals.py` data; `tools_*.py` the commands (`start`, Business Agent, Plan, proposals, audit log); `ops.py` operator CLI; `pages.py` `/plan/{id}`.
- `google_auth.py` / `sessions.py` / `claims.py` / `web.py` Google sign-in, cookie sessions, claim links and the owner pages (`/connect`, keys shown once, `/plan/{id}` approval, connected assistants with revoke, starting a new business).
- `oauth.py` / `oauth_web.py` MCP OAuth 2.1 on `/mcp` (`AdmiryOAuthProvider`, DCR, PKCE, refresh rotation; an access token is an `api_key` row) and its consent page (`/oauth/consent`: which business, scopes in owner words, Allow/Deny).
- `legal.py` / `agreements.py` / `google_ads_link.py` / `tools_ads.py` the Google Ads Management Agreement (`legal/management-agreement-v1.md` since spec 2026-10-11 §5.2 -- §2, the changes Admiry makes to keep the ads running; v0 stays in the directory for the record; `/legal/management-agreement`, click-wrap rows), the link of the owner's own Google Ads account to Admiry's manager account (`api` over httpx or `manual` for an operator; verification on ACTIVE; every write mirrored into Google Search's `channel_account` row, `google_ads_link.mirror`) and `connect_google_ads`, the alias of `tools_channels.connect_channel` (below).
- `ads_mode.py` / `wallet.py` / `stripe_client.py` / `pay.py` / `tools_wallet.py` prepaid ads (`managed`): the chooser's rules, the Prepayment Addendum (`legal/prepayment-addendum-v0.md`, `/legal/prepayment-addendum`), the balance (integer cents, append-only entries, payment-as-verification), Stripe over httpx (Checkout, refunds, webhook signatures), `POST /hooks/stripe` (`/pay/{id}` only redirects old links to `/?setup={id}`), and the balance commands.
- `setup.py` / `setup_api.py` / `static/setup.js` ads setup in the home conversation (`/?setup={id}`, `POST /api/setup`): choose the way, one Accept, link Google Ads or add funds (Stripe Checkout), Change; the steps are `claim → choose → accept → pay | link → done` (`state`; L1, 2026-10-10: the `review` step and B0's `blocker` / `onBlocker` are gone). The plan card's own yes approves (`approve` → `proposals.approve_on_web`, launching with `ads_run.launch_now` only when approved), or Agree and pay's webhook does (`approve_paid`, once its Checkout's credit has committed); what stops a launch is the plan card's own checklist (`launch_check`). `/connect` is Settings: read-only status (way, balance, payments, link, accepted texts) plus keys and connected assistants.
- `launch_review.py` a first launch's review after the owner's yes (L1, spec §6): one row a version while `AGENT_LAUNCH_REVIEW` is on (`queued → creative → configured → checked → passed | held | returned | rejected | withdrawn`), opened by the yes itself (`submit`, inside its approval's own transaction) and withdrawn by a newer version (`withdraw_open`, `plans.create_version`); `hold` and `decide` commit atomically, a concurrent admin's own decision never overwritten (rulings R20, R22); `returned_before` holds a version for a person again when an admin returned an earlier one (ruling R17); `note_for` is the admin's word a returned or rejected version carries back to the owner's list (`launch_check.items`). `resolution` (spec 2026-10-11 §5) is the fix a return the Reviewer makes names on the owner's list (`phone`, `check_page`; an admin's return names none); `validations` counts the `validated` steps -- the validateOnly calls Google answered -- over every review of a plan version (at most `reviewer.VALIDATIONS_PER_VERSION`, 3). The Reviewer (`roles/reviewer.py`) and the admin's decisions (`ops_review.py`) both act on it.
- `plan_review.py` the second look before the owner confirms (S0): the launch's check as a read (`proposals.launch_refusal`) and what it asks of the owner (`ask_for`: a question or an action, each with its public tool — MarketingState's `next` for a refused launch, `tools_state._ask_next`), the money, one model call (`llm.review_plan`, `models.PlanReview`, advisory) stored once per plan version in `plan_review`; the receipt e-mail's next step (`next_step_words`); B0 (2026-10-08) adds `RESOLUTIONS` / `resolution_for` beside `ASKS` / `ask_for` -- what Admiry offers on its own surfaces for each refusal, read by `blockers.py` below -- and `ask_id_for`, the blocker's ask id with a leading place's name taken off (the home's reading; `ask_for` keeps the MCP side's exact pre-B0 one).
- `blockers.py` what stops a launch, said in the conversation (B0, spec `2026-10-08-admiry-blockers-in-conversation-design.md`): the latest draft's refusal (`proposals.launch_refusal`) with what resolves it (`plan_review.RESOLUTIONS`: a button, one question, choices), the budget choices that pass, Admiry's own page's headline fix, the strict reading of a typed answer; `home_api`'s `POST /api/blocker` runs the fixes (each an existing command). Since L1, the home carries no `blocker` of its own: the plan card's checklist reads `GET /api/launch_check` instead (`launch_check.py`, below), whose `items` calls this module's own `describe` for each refusal -- its resolution (what `launch_check.py` turns into the suggestion under the problem), its question and its one-tap action(s).
- `launch_check.py` the launch checklist (L1, spec `2026-10-10-admiry-launch-review-design.md` §4): every reason the latest draft cannot launch yet, read at once from the launch's own rules as lists (`ads_run.plan_refusals` / `page_refusals` / `place_and_phone`, the Google adapter's `refusals`) and B0's resolutions (`blockers.describe`) -- Google Search first, so a refusal two places share is its item (ruling R9), an `ask` item's id its bare resolution whatever place raised it (ruling R10); the admin's return or reject note, when there is one, leads the list, blocking only once rejected (plan D8, ruling R15c). The money and the account are no items, left to the step after the list; a place other than Google Search never blocks the others (channels spec D77). `check` (`GET /api/launch_check`), `blocked` (the yes's own refusal) and `first_ask` (typed words' target) are its readers. `public` (spec 2026-10-11 §3) is the list as an assistant reads it (`get_marketing_state`'s `setup.checklist`): each item's problem, suggestion, place, whether it blocks, and `fix` -- the one public tool and the arguments that fix it (`fix_for`: `<…>` where only the owner knows the value, with `ask_owner`; `choices` for a budget and for the page); a returned note with a resolution is the ask or the fix item it names.
- `operations.py` the three kinds (spec 2026-10-08 §2.1, §2.2): every registered command dangerous or safe, and the never column; a leaf the blockers' `RUNS` reads today and K6 will.
- `harness/` the home's thin harness (capability layer K6 slice 1, spec `2026-10-10-agent-k6-harness-consultation-design.md`): free text while a consultation is open or ready goes through `fast.py` (the code paths that spend no call: the start-over phrases whole, "rather not say" at a text question, the questions answered by word rules), else `context.py` assembles the material (the key's own business and goal, the questions with their answers, the open one with its options and `why`, `may_go_to`, the free-text turns since the latest answer (at most five); within 3,000 tokens) and `turn.py` makes the one `Turn` call (`models.Turn`, `llm.read_turn`, the stage skill `skills/before_plan.md`) under the key's limits and the day's cap, then `dispatch.py` runs its one dispatch -- a reroute into `answer` (the reading handed through `consult.with_reading`, so `answer` makes no second call), `recommend` at ready, or `update_business` for "not my listing"; a start over (`business.retire_draft` for a draft and the cookies gone, `consult.supersede_open` for a claimed business); the owner's budget on the goal; or Admiry's words and the question again (`explain`, `ask`, `smalltalk`, `unclear`, a skip without an option, support off). One `web.turn` audit row a turn (`counted` for a model turn, unless the reroute hands words to `answer`, whose own row counts that call). `AGENT_HARNESS=off|on`; the golden turns `agent/eval/turns.json`, `python -m agent.eval turns` live on dev.
- `roles/` the capability layer's roles (spec `2026-10-07-admiry-capability-layer-design.md`): `base.py` (contract, grades clamped, score and verdict computed), `contracts.py` (the Critic's plan rubric, the roles' contracts), `critic.py` (the judge: the plan rubric on the look's one model call), `queue.py` (`agent_job`: one waiting job per business, role and kind; claimed by shard), `runner.py` (`research-run`, hourly: the Critic's look at each new plan version and, since K4, the Researcher's runs; 24 background calls a business a day), `analyst.py` (K2: `analyst-run`, hourly at `:02` — each business's daily look, one `insight` row per business and local day: figures, flags and candidates computed by code, the model's words checked by `guard.py` (length, links, numbers the material does not hold, jargon), graded by the Critic, written again at most once; owners read it only through `owner_view`, with `AGENT_ROLES=on` and the Critic's deliver; nothing in it is ever applied), `researcher.py` and `research_material.py` (K4, the next bullet). `AGENT_ROLES=off|validate|on`.
- `roles/researcher.py` (K4, the Researcher): `research-run`'s `("researcher", "business")` jobs — queued by the hourly sweep after a signed-in business's first plan, and again 30 days later if its plan changed (the 30 days run from research that stands for them: a run done that the switch can stand for — with `AGENT_ROLES=on` only one built on — or one that failed for its material; a run that failed otherwise is tried again after `RETRY_DAYS` (3) days, until `RETRY_LIMIT` (3) runs have failed in the 30 days -- the first and two retries; a business never researched comes first), and by an owner's or the operator's ask — one `research_run` row per job, the confidence loop of at most two rounds with every sentence and search checked by code (a source the material holds, every number in its sources' words, no business nearby named by any form of its name or its page's title, no mention of Admiry) before the Critic grades it (its serious `unnamed` check sends naming back), and `owner_view`, the one way an owner or a plan version reads a run: `AGENT_ROLES=on`, a run built while it was on, the Critic's deliver; nothing in it is ever applied. `roles/research_material.py` is what it reads: one Places call an attempt, ≤ 10 pages (never one Admiry hosts), K3's guides with `AGENT_KNOWLEDGE` on.
- `roles/reviewer.py` the Reviewer (L1, role `reviewer`, kind `launch`, spec §6.2): a first launch's review, run as a `research-run` job whatever `AGENT_ROLES` says and whatever `AGENT_LAUNCH_REVIEW` says *now* (ruling R16 -- the switch gates only whether a yes goes to review) -- the creative (the Critic's look, the copy linter, the page checked afresh), the configuration (the campaign built and sent to Google with `validateOnly`), the check (the configured ads against the agreed plan, in code and by one model call), then the pass (the approval itself and the launch) or a hold for a person, never unseen (ruling R19: an unexpected error, a plan with no funded Google Search entry, or a run that died each hold it too, as does a version a person returned before, ruling R17). At most two model calls a version (`job.reviewer`, counted toward `ROLE_RUNS_PER_DAY`; the pass itself, `job.review_pass`, is not). Google's refusals (spec 2026-10-11 §5): a refused validateOnly is read in detail (`client.RefusalDetail` on `PlatformError.details`) and classified (`channels/google_search/refusals.py`); ad text and search phrases Google refuses are dropped (`ads_refusal`) and the ads sent once more in the same run, the owner e-mailed once a plan version, at the pass (`templates/mail_review_changed.txt.j2`); the owner's own page or a fact about the business goes back to the owner with its fix; Admiry's own page, a second refusal, a count of `VALIDATIONS_PER_VERSION` (3) answered calls, or anything Admiry cannot place holds for a person.
- `knowledge/` Admiry's curated knowledge (capability layer K3, spec `2026-10-07-admiry-capability-layer-design.md` §6.1): `<vertical>/<topic>.md` files (YAML frontmatter, Key points, Details, Numbers, Do not say), reviewed by the founder, and `INDEX.md` (the table of files, written by `python -m agent.knowledge_index index`, and the research taxonomy mapped to the code's categories); `knowledge_index.py` loads and checks them, previews what a draft reads (`preview`) and retrieves by tree, channel and topic within 1,500 tokens; `roles/strategist.py` picks what a plan draft reads (the trade comes from `trades.py`, the one table of trades the industry sites share), keeps the ids the draft names that it was given (`sections.rationale.knowledge_ids`) and picks what the answerer reads (the files the plan cites); the resources `admiry://knowledge` and `admiry://knowledge/{vertical}/{topic}`. `AGENT_KNOWLEDGE=off|on`; the operator's `agent.ops knowledge`. A file still waiting for review says `reviewed_by: pending` and is never read by a model or served.
- `tools_research.py` the Researcher's two commands (K4; public as `get_research` and `request_research`, contract `2026-10-09`): `request_research` queues a run (an owner's ask at most once a day; nothing spends and nothing in the plan changes), `get_research` reads the latest run `researcher.owner_view` lets an owner read, each finding with its sources; the resource `admiry://marketing/research`; the operator's `agent.ops research|research-show|research-due`.
- `research.py` `python -m agent.research vertical <tree>` / `promote <tree>/<topic>` (K4, spec §6.1): knowledge drafts in K3's format, written outside `agent/` (`docs/research/knowledge-drafts/`) for the founder's review; `promote` stamps a reviewed one, moves it into `agent/knowledge/` and rewrites INDEX.md's table. Offline: needs `META_MODEL_API_KEY` and a checkout, no database.
- the daily digest (K2; spec `2026-09-30-admiry-daily-digest-design.md`): `digest.py` the job (`digest`, hourly at `:12`: claim, build with the Analyst's look, render, send; one row per business and day, re-checked before every send) and its reads; `digest_zone.py` (the business's time zone, the reported day, the shared daily lock), `digest_audience.py` (who gets which edition, the sync gate), `digest_numbers.py` (every number, one source each), `digest_rules.py` (the "needs you" items), `digest_body.py` (the stored body and its owner sentences), `digest_links.py` (signed links), `digest_mail.py` (rendering, `templates/mail_digest.html`), `digest_web.py` (the unsubscribe page and its RFC 8058 POST, the Reports card, `/api/report`, Resend's bounce and complaint events at `/hooks/resend-events`); `tools_insights.py` `get_insights` and the insights resource (what the owner's assistant reads of the daily look); `copy.py` the owner-word checks; replies to a report are `mail_dispatch._digest_reply`. `AGENT_DIGEST=off|operator|on`; the operator's commands are `agent.ops digest-preview|digest|email-status|digest-seed|insight|insight-rebuild`.
- `eval/` the golden set and its tools (spec `2026-10-07-admiry-capability-layer-design.md` §7, K1 and K5): `cases/` (twenty cases, each with its frozen plan and the founder's grades) and `flawed/` (six flawed copies, `flaws.py`); `python -m agent.eval run` (plans built fresh, judged, the 0.3 fall rule against `baseline.json`), `freeze` (the plans the founder grades, built once), `judge` (the judge's alignment with the founder and the flaws caught, `align.py`), `export` (the grades into the case files, `bundle.py`), `case-from` (a dev business anonymized, `anonymize.py`); `online.py` behind `agent.ops eval-online`. `eval_web.py` is the founder's grading page (`/ops/eval`, dev only, `AGENT_EVAL_GRADERS`; the grades in `eval_grade`, `eval/grades.py`).
- `ops_review.py` the admin's reads and decisions on a first launch in review (L1, spec §7, §8), shared by the admin page and the admin MCP: the queue and one review in full (`queue_view`, `detail`), approve and launch (the Reviewer's own pass, in the admin's name; a refusal from the approval's own check holds the review and is said, never silently, ruling R24), return to the owner or reject with a note (`send_back`: the decision and its audit row commit together, the owner e-mailed only after, ruling R25). Every decision is an audit row `admin.review_approve` / `admin.review_return` / `admin.review_reject`, actor `admin:<email>`.
- `ops_web.py` `/ops/review` and `/ops/review/{id}` (L1, spec §7), and `/ops/tasks` and `/ops/tasks/{id}` (the Help button, spec 2026-10-12 §4: the support tickets by tab — Open, Waiting on the owner, Closed — one ticket with its events and a Help report's context, reply by e-mail, send and close, close as; the same gate), and `/ops/rules` (Part B phase 1: the refusal rules -- proposed, active, add one, the daily summaries; the same gate): the admin's queue and one review's three decisions, behind Google sign-in and `AGENT_ADMIN_EMAILS` on every box (unlike `/ops/eval`, prod serves this one: the review queue is prod's too) -- everyone else, signed in or not, and the whole page while the list is empty, gets a 404. A refusal from `ops_review`'s own check is said on the page (409) and changes nothing.
- `ops_mcp.py` `python -m agent.ops_mcp --as <admin e-mail>`: the admin MCP (L1, spec §8), stdio only -- no port, no Caddy route, no OAuth -- started by the admin's own client over SSH into the box's agent container; `--as` names the admin for every decision's audit row and must be one of `AGENT_ADMIN_EMAILS`, checked once at start (one process per SSH session), or the server refuses to start (ruling R27). Tools: `review_queue`, `review_get`, `review_approve`, `review_return`, `review_reject`, `review_run` (the Reviewer on one review now), and `ticket_queue`, `ticket_get`, `ticket_reply`, `ticket_close` (spec 2026-10-12 §6, `ops_tasks`'s own functions) -- `ops_review`'s own functions, and `rules_list`, `rule_decide` (Part B phase 1, `ops_rules`'s own functions); nothing of the public MCP's registry is mounted here.
- `ops_tasks.py` the admin's reads and actions on support tickets (the Help button, spec 2026-10-12 §4), shared by `/ops/tasks` and the admin MCP: the list by tab with each ticket's origin (the Help button and its page, the owner's assistant, e-mail, a 👎, the website card, the conversation), one ticket in full (`detail`: the business and its latest plan and review, the contact verified or not, every event with a report's context under `data.help`, the opening snapshot's last turns), `reply` (`support.reply`; the caller flushes the outbox after the commit) and `close` (`support.set_status`), each with its audit row `admin.ticket_reply` / `admin.ticket_close`, actor `admin:<email>`.
- `local_rules.py` the refusal rules (Part B phase 1, spec `2026-10-12-admiry-google-refusal-knowledge-design.md` §2.2–§2.3): `refusal_rule` rows -- a whole word or phrase, or one of seven pattern shapes by id (`SHAPES`; never a regular expression from input) -- on headlines, descriptions, search phrases or any; one pure `check(lines, rules)` (a line's first `block` rule, else its first `warn`), `load` (the active rules while `AGENT_REFUSAL_RULES` is on, nothing off), `stamp` (the Reviewer's: the rule a refused line matched, on `ads_refusal.rule_id`), `with_rules` (the block rules onto `build.Dropped`), `validate` (the one value check for the model, the form and the MCP).
- `refusal_summary.py` the daily `refusal-summary` job (spec §2.4; the daily DAG, prod `bidx-agent-refusal-summary.timer`): yesterday's `ads_refusal` rows and the live disapprovals ads-sync keeps into one `refusal_summary` row (counts, the lines, the rules' hits and misses) and up to five proposed rules from one `llm.propose_rules` call under the Analyst's day (`analyst.used_today`), each re-verified in code (`_accept`: the value, the evidence the rule matches, ≥2 businesses or ≥3 lines, a trademark warn only, a key never twice); a `warn` rule active at once, a `block` rule proposed. No model: the counts alone.
- `ops_rules.py` the admin's reads and decisions on the rules (spec §2.5), shared by `/ops/rules` and the admin MCP: `page_view` (proposed with evidence, active, the last summaries), `decide` (approve as warn/block, reject, retire, level) and `add` (a rule by hand, active at once), each with its audit row `admin.rule_decide` / `admin.rule_add`, actor `admin:<email>`.
- `ads_refusal.py` every refusal Google answers a validateOnly or a launch with, recorded (spec 2026-10-11 §6): one row per refusal detail with its class and what Admiry did (dropped, returned, held); `dropped` is what every Google build of the business leaves out -- a line Google refused, by its exact text, whichever version it was refused in (ruling L2; `build.Dropped`, read by `adapter.build` and `move_page` -- the plan's json is never edited); `changes`, `line` and `say` are the owner's words for what Admiry removed, the drops in effect for a version -- its own, and an earlier version's whose line it still carries (the plan page, the home card, Your ads, `get_marketing_state`'s `changes`, the e-mail at the pass); `for_review` the admin page's rows; `dropped(…, settings=)` carries the active block rules too (`build.Dropped.rules`); `carried`, `flagged`, `why_for` and `avoid_words` are what the checklist, the home's rewrite and the plan writer read (Part B phase 1); the daily summary (`refusal_summary.py`) reads the table by day.
- Settings (`/connect`, spec `2026-09-29-admiry-settings-redesign-design.md`): `templates/connect.html` +
  `static/settings.css`, a left section nav (Overview, Ads and billing, Assistants, E-mail when `email_inbound`,
  Advanced = keys folded away, Account) over a column of cards; the one primary button is the next step to
  `/?setup={id}`. One business at a time: `?b={id}` when it is the owner's, else the most recent (a switcher when
  there are several); per-business POSTs redirect to `/connect?b={id}#<section>`; `POST /connect/signout` → `/`.
  Times through `timefmt.when` (relative, exact UTC in `title`); `web.CONNECT_CSP` (self only, no inline script or
  style). The header (`templates/_site_header.html`, the account menu: e-mail, Settings, Sign out), `static/base.css`
  and `static/site.js` are shared with the home.
- `landing.py` / `tools_pages.py` Admiry Pages (spec `2026-09-28-admiry-pages-design.md`): the kit (`get_page_kit`) built from the plan and a vertical recipe (`page_recipes/{dentists,home_services,attorneys,fallback}.json`), the page-record helpers and the 14-rule `check_landing_page` (`landing.BLOCKING` gates `ok`; the last two rules, `terms` and `contact` — NewsBreak asks both of a landing page — are warnings for Google and blocking for a channel whose `page_rules` name them, judged by `landing.passed`; `get_page_kit` adds their two footer lines to `rules.required_lines` only when a funded channel asks); the commands themselves — `get_page_kit`, `set_landing_page`, `check_landing_page`, `get_page`.
- `hosted.py` hosting for Admiry Pages (A3p-2): the pages-domain slug and its uniqueness, a page's linear
  version history and unpublished previews, the HTML sanitiser and the CSP they both serve under, and serving
  by `Host` (`/`, `/privacy`, `/terms`, `/photos/{id}`, `/robots.txt`, and `/pages/ask` for Caddy's on-demand TLS).
  The two notices (`templates/hosted_privacy.html`, `hosted_terms.html`) are plain pages built from one context (the
  terms page sends questions to the business's phone, else through the page's form — never to an e-mail address), and
  every hosted page's footer links both and shows the business's phone (with none on file, no contact line: the owner's
  sign-in address is the owner's to publish, never Admiry's to guess).
  `tools_hosting.py` the commands — `preview_page`, `publish_page`, `unpublish_page` (proposals the owner confirms)
  and `create_page` / `update_page` / `revise_page` (a page Admiry writes itself, for an owner with no
  assistant; with `AGENT_SITES` on, a home-services business or a dental practice with no such page gets its site
  from `create_page` instead -- `tools_sites`' first step -- and `revise_page` / `update_page` refuse on a site page
  until batch 3: `NO_CHANGES_YET`). `page_builder.py` (+ the four vertical templates and `_base.html`/`hosted.css` under
  `templates/pages/`) turns the kit, a template and the model's copy into one rendered page, its linter reusing
  Google Search's own banned-word, phone and shouting checks (`channels/google_search/build.py`). `uploads.py` the owner's photo upload
  (`GET`/`POST /u/{token}`, no MCP call needed): sniffed by magic bytes, resized and re-encoded, stored in
  `page_photo`; an industry site's page (A3p-6 batch 2) asks what each batch shows (the family's kinds, `page_photo.kind`),
  caps each kind, keeps a logo's transparency as a PNG, shows its own thumbnails (`GET /u/{token}/photo/{id}`) and
  builds the site's preview again without the model and without the render check (`sites/photos.rebuild`).
- `hosting_minimum.py` the hosting minimum (spec `2026-10-03-admiry-pages-a3p-3-design.md` P12–P20, plan
  `2026-10-04-agent-a3p-3-1-hosting-minimum-and-muse.md`): a hosted site is free while the business's ads through
  Admiry run on $200 a month across channels or spent $200 in the last 30 days (`coverage`, both modes; an operator's
  `exempt`); short of that, a 30-day countdown on the page row (`countdown_ends`, `countdown_mail`) started by the
  daily job (an e-mail) or by a publish (the request names the date), the e-mails 7 days and 1 day before, and at the
  end the takedown -- the ads that carry the site's address paused first, then A3p-2's unpublish with the reason
  `hosting_minimum` -- the files kept 90 days (`purge_after`), then deleted and the address freed. Coming back
  (`precheck_publish`) needs the ads ready at $200 a month (`ready`). `jobs.hosting_minimum` runs it daily;
  `get_page`'s `hosting` block, `get_page_source` (behind `get_landing_page(view="source")`), the download
  (`/connect/site?b=`, behind sign-in) and the homepage card say it; the countdown's e-mails are
  `templates/mail_hosting_{start,reminder,kept,down}.txt.j2`.
- `sites/` industry sites (A3p-6): `spec.py` the site's versioned document, `families/` the families and their styles
  as design tokens, `samples.py` the sample roofer and dentist and a sample per other home-services trade (hvac,
  plumber, electrician: 2026-10-08, the looks pictured by the business's trade, `looks.sample_for`), `stock.py` the
  stock photo library in storage
  (`stock.load` from the manifest and a local copy of its files, `pick` / `variants`; `python -m agent.ops stock-load`,
  `data/stock/README.md` "On a box"), `images.py` the photo slots filled (the owner's, then the library, seeded by
  the business), `render.py` the site rendered with `templates/sites/` into a bundle's files (`base` for a prefix),
  `words.py` the model's words and their linter, `build.py` the site as the business's bundle preview through the
  upload's checks, the render check at three sizes and Google's speed test (`checks="static"` for a photo change;
  `publish_gate`: a site goes live once its logo and key photo are in; `live`: what goes live is the same document
  drawn again without the preview's labels and stock-only sections, with canonical links, at publish),
  `intake.py` (batch 2) the family and trade a business's knowledge says (`family_for`), the site's facts filled from
  the profile, the confirmed listing and the consultation with the required ones still missing named (`facts`), a patch
  of facts checked (`check`) and written where it belongs (`merge`: the profile's own facts on the profile, the extras
  in `page.site_draft`, the owner's working copy between builds), `photos.py` (batch 2) the photo kinds of each family
  (`page_photo.kind`), the slots the owner's photos fill, the checklist and the sentences that say what keeps a site
  from going live, `gallery.py` the six looks on one preview (`python -m agent.ops site-preview` / `site-gallery`),
  `looks.py` (batch 2) the looks an owner picks a site by -- each family's three styles with a name, a line and its
  sample's phone screenshot, `static/sites/looks/<sample>-<style>.jpg` (refreshed from a gallery with `python -m
  agent.ops site-looks <business_id> --out agent/static/sites/looks`);
  the stock files and manifest live in `data/stock/`, the sites' data (fonts, samples) in `data/sites/`.
  `tools_sites.py` (batch 2) the commands, behind `AGENT_SITES` (off: they say the sites are not on yet) --
  `build_site` (the look asked first, then each missing fact, one at a time, then the site built into the bundle
  preview with its checklist and the phone upload link; the model writes the words only when what it reads
  changed, `words.fingerprint`),
  `get_site` (where the site stands: `site_view`) and `get_site_templates` (the looks). `BuildCall` is the one call
  that may build -- `build_site` and `create_page` for a family business -- whose `after` writes the owner one e-mail
  once the first build has committed (`templates/mail_site_ready.txt.j2`: the preview, the hosting minimum, the photo
  checklist and the phone link; the draft's `mailed_ready` is set only once it was sent).
  `static/sites.js` (batch 2, loaded after `channels.js` and before `home.js`) draws the homepage's site card -- the
  looks as `static/sites/looks/<sample>-<style>.jpg`, the missing facts on one card, the site at three sizes (`GET
  /api/page/shot?size=…&preview=1`, the live site preview's screenshots), the checklist and phone link, the wordmark,
  the industry's own facts, another look, Publish held until the logo and the key photo are in -- from the state
  `POST /api/page/state` answers for a family business (`home_api._site_state`); `POST /api/site/build` and `POST
  /api/site/extras` are its two writes. The batch's two columns are `page.site_draft` (the owner's working copy between
  builds) and `page_photo.kind`; `AGENT_SITES` (`on` or `off`, default off: every business gets the one-page starter
  page) is the switch (docs/ops/config-reference.md "Industry sites (A3p-6 batch 2)").
- `bundles.py` / `site_upload.py` site bundles (spec `2026-10-03-admiry-pages-a3p-3-design.md` P30–P37 §6, plan
  `2026-10-04-agent-a3p-3-3-site-bundles.md`): a whole site an assistant built (a Next.js static export, Vite, Astro,
  Hugo) uploaded as one zip of its build output. `request_site_upload` (behind `update_landing_page(action="upload_site")`)
  mints a one-time link, `PUT /s/{token}` on the agent host (30 minutes, one upload, 411 without a length, 413 past
  25 MiB counted as it streams); `bundles.judge` unpacks it counting real bytes and refuses the path tricks, a bomb and
  a project, then checks the contract (index.html at the top, the limits, the file types) and every HTML page with the
  sanitiser (`hosted.problems`, which names each rule) and for Admiry's script tag, answering every problem as
  `{file, rule, message, fix}`. What passes is kept once per content (`site_blob`, by SHA-256) as a bundle preview
  (`page_preview.kind`, `page_preview_file`) on the business's own preview host (`preview--<hex>.<pages domain>`,
  `page.preview_host`); `publish_page(token)` makes it a bundle version (`page_version.kind`, `page_version_file`)
  served by path on the tenant host (`bundles.respond`: the ETag is the SHA-256, HTML 5 minutes, files an hour,
  hashed names a year, an SVG under a policy that runs nothing). `get_page_source` answers a bundle's files and a
  one-hour zip link (`GET /s/zip/{token}`, `site_zip`); `get_page`'s `upload` is the last upload's result; the hourly
  page-check prunes the links and collects the files nothing names.
- `render_check.py` the render check from the agent's side (A3p-3 batch 4, plan `2026-10-04-agent-a3p-3-4-render-check.md`):
  with `AGENT_RENDER_URL`, a site bundle's upload is opened by the renderer service (`renderer/`, its own container: a
  headless Chromium) on its preview host before the preview is handed out -- its problems (a request that fails or
  answers 404, what the hosted page's policy refused, a script error, a blank page, a redirect away, a form that does
  not post to Admiry, no visit counted) refuse the upload in the static checks' shape and drop the preview -- and its
  screenshots stay with the preview, then the version (`shot_phone`, `shot_tablet`, `shot_desktop`): the homepage card shows the
  phone's (`GET /api/page/shot`), the report e-mail after a publish attaches all three. The hourly page-check opens each live
  bundle once a day (`page.render_json`, `get_page`'s `render`) and e-mails the owner when it goes from no problems to
  some (`templates/mail_render.txt.j2`) -- a site Admiry built itself is opened too, but its problems are Admiry's
  template's: they go to the log at ERROR, never to the owner.
- `track.py` / `leads.py` / `mail.py` the page-side script (`static/admiry.js`) and its endpoints (`GET /t/admiry.js`, `POST /t/event`, `POST /t/form/{page_id}`), leads (stored and e-mailed by `leads.py`; their commands `list_leads` / `update_lead` / `add_lead` live in `tools_pages.py`) and owner e-mail through Resend (`send`, `render`, `configured`, `MailError`; `templates/mail_kit.txt.j2`, `mail_lead.txt.j2`, `mail_check.txt.j2`).
  Since A3p-3 batch 2 (plan `2026-10-04-agent-a3p-3-2-measured-contacts.md`): the script keeps what brought the visitor
  (the click id, the link's UTM tags, the referring host) 90 days in the site's `localStorage` (`admiry_visit`) and
  sends it with every beacon and form; a form's `data-admiry-intent` (consult, appointment, quote) lands on
  `lead.intent` and the form's answer names its one conversion; Calendly's `calendly.event_scheduled` is a
  `booked_online` event and a `booking` lead with no contact; with the account's `call_site` action the snippet
  carries `data-phone` and the tag swaps the number for Google's forwarding number. `attribution.py` names a
  contact's source (`came_from`: the click's channel, `utm:<source>`, search, social, other, direct), which
  `get_campaign_performance` splits leads by (`by_source`).
- `transcript.py` / `support.py` / `support_mail.py` / `feedback.py` / `support_api.py` / `tools_support.py` support
  tickets and plan feedback (spec `2026-09-30-admiry-support-and-feedback-design.md`): the conversation-and-plan snapshot,
  one open ticket per business (homepage reads, the Help button, `contact_support`, e-mail hand-offs), the ticket
  mail's outbox and the hourly `support-mail` job, votes per plan version; `static/support.js` + `support.css` the
  thumbs on the homepage and `/plan/{id}`. The Help button (spec `2026-10-12-admiry-help-button-design.md`; it
  replaced the dock's "Talk to a person"): `templates/_help.html` with `static/help.js` + `help.css`, last in `<body>`
  on every page an owner or a visitor reads while `AGENT_SUPPORT` is on (`web.page()` and `pages.py` put `help` in the
  context), posting to `POST /api/help` (`support_api.py`): the words plus the page, the plan, signed in or not, the
  browser and the last turns on the ticket's `owner_words` event; a visitor's typed address is kept on
  `support_ticket.contact_verified = false` — no acknowledgement until the claim or a signed-in ask verifies it
  (`support._verify`), and `support.reports_today` bounds an address's reports a day. Environment: `AGENT_SUPPORT`
  (`off` default), `AGENT_SUPPORT_REPLY_TO`, `AGENT_FEEDBACK_PLAN_PAGE_RATE` (docs/ops/config-reference.md).
- `channels/` the channel framework (spec `2026-09-29-admiry-channels-design.md`, M0; its "As built (M0)"): every
  place the ads run is one adapter behind one protocol, so a new channel is a directory, not a code path.
  `channels/base.py` the shared types (`Capabilities`, `Account`, `AccountStatus`, `Step`, `Build`, `Resources`,
  `Serving`, `Words`, `MetricDay`, `Issue`, `PlatformError`, `Unsupported`, …) and the `Adapter` protocol;
  `channels/registry.py` the channel table — the owner's names (`LABELS`, D76), the card order (`ORDER`), the
  click-id kinds (`CLICK_ID_KINDS`), the switch per channel (`exec_mode`: `AGENT_EXEC_<CHANNEL>`), a business's
  account on each (`account` / `save_account` / `managed_channels` over `channel_account`); each adapter registers
  itself when `agent.channels` imports it.
  - `channels/google_search/` the first adapter, A3 batch 1's Google Search moved with its code unchanged:
    `client.py` the Google Ads REST client (v25: token exchange, `search` (GAQL, paged by page token; the body is the
    query alone), `mutate` with `validateOnly`, `AdsError` → an owner sentence plus Google's own error code, `AdsError.details`, one
    `RefusalDetail` per error and policy topic of a refused mutate (what the path points at, the topic, the sent
    line), `suggest_geo_targets`) that `google_ads_link.py` and the modules beside it call; `refusals.py` the pure
    classifier (ad_text, keyword, destination, business, other); `build.py` pure — a plan
    version to mutate operations, the diff between two plan versions (P10), the budget and CPC ceiling pair
    (`money`), and the copy linter behind `plans.cards()`'s `ads_preview` card; `conversions.py` the account's
    `Admiry · <kind>` conversion actions and call reporting (`ensure`, `labels`); `adapter.py` the protocol over
    them — the link check, the name lookup before any create, everything created paused then enabled, an update's
    money first, the pause, the sync's reads, the metered client and its `ops_sent`, and the page the ads carry
    (`follows_page`: `landing_url` adds `src=gads`, `carried_url` is `resources.final_url`, `move_page` is A3p-2's
    URL-only update — M0 Task 9).
  - `channels/newsbreak/` the second adapter and the first Admiry runs itself (M1, plan
    `2026-09-29-agent-m1-newsbreak.md`; the spec's "As built (M1)"), from the business's balance, in an ad account
    per business under Admiry's NewsBreak Organization: `client.py` NewsBreak's Business API (the `Access-Token`
    header, the `code`/`errMsg`/`data` envelope and its owner sentences, paging, the spending cap, the HOURLY report,
    the asset upload, the conversion postback by click id, NewsBreak's facts pinned from its docs, a metered client);
    `build.py` pure — the campaign, the ad set (CPC, a daily budget of at least $10, the whole targeting with the ZIP
    codes' location ids), up to three image ads with the plan's linted words, the click URL (`src=nb` and the click-id
    macro) and the diff between two versions; `creative.py` Admiry's licensed picture set (`static/creative/
    manifest.json`: at least 1200 × 627, JPEG or PNG, at most 5 MB; `pick` by trade and service, once per plan version,
    which pins it -- a trade without one gets no NewsBreak in its plans; `find` the pinned one by its file; read once
    until a file changes); `adapter.py` the protocol over them — every create OFF, then ON once all exist; the
    lifetime spending cap from a recorded base plus the month's amount; changed words as new ads beside the old ones
    until NewsBreak's review passes them; the page follow; the hourly read (metrics by day of the account's
    America/Los_Angeles calendar, NewsBreak's review as `channel_issue` rows, the new month's cap, the stops on a held
    day); one request a second per business; the account's own trouble and the hand-made lead event as operator mails.
  - `google_ads.py` / `ads_launch.py` / `ads_conversions.py` import shims of `channels/google_search/client.py` /
    `build.py` / `conversions.py`, kept one release for outside importers: no module of the service imports them,
    and `tests/test_google_search_adapter.py` pins that they hand out the moved modules' own objects.
- `geo.py` the radius → ZIP codes → NewsBreak location ids (M1: every ZIP code whose centroid is within the radius, at
  least the nearest, at most 300; `nearest`'s distance is how NewsBreak's precheck refuses a point far from any US ZIP
  code) over `data/zcta_centroids.csv` (the Census Gazetteer) and `data/newsbreak_locations.csv` (NewsBreak's location
  sheet), rebuilt by `agent.ops geo-refresh` (`data/README.md`).
- `ads_run.py` the channel-agnostic lifecycle over `channel_campaign` (one row per business and channel) and
  `metric_daily`: `precheck` / `launch` / `launch_now` / `update` / `sync`, `pause_channels` / `resume_channels`
  (the owner's pause, `resources.owner_paused`, beside Admiry's `paused_reason` -- money's reasons above the page's
  `page_down` / `unreachable`), `daily_budgets`, `all_paused`, the account's calendar (`account_zone`,
  `account_today`), the owner sentences and the monthly statement per channel; every platform step goes through
  `registry.adapter(channel)`. A3p-2's page follow, for a channel whose ads follow the page (M0 Task 9):
  `flag_moved_page` / `retry_refused_address` / `mark_page_down` (business-wide, inside a proposal's transaction),
  the move (`url_stale` → a URL-only `update` or `refresh_url`), a refused address (`resources.refused_url`), the
  page-down pause (`pause_page_down`, and the enable step's guard), `resume(only_if=ResumeIf(...))` for page-check.
  `jobs.page_check` is the hourly page-check over those channels.
- `allocate.py` the initial split of a plan's amount across channels by band (spec §6.2), pure; `plans_channels.py`
  fetches what it reads (capabilities, benchmark rows, the tree's fit flags, the goal's caps) and writes the split
  into the plan (`sections.ads.channels[]`, the split per budget level, the owner's pinned amounts).
- `state.py` `MarketingState` v1, the one read model (D102): `build` reads the plan's cards, a `ChannelRow` per
  channel and the platforms' open issues, and every reader renders it — the homepage's `state` action,
  `GET /api/state`, the channel commands and the MCP channels view (`templates/channels_view.html`,
  `ui://admiry/channels`); `connections` is the account side (Settings' "Where your ads run", the setup steps,
  `get_connection_status`).
- `tools_channels.py` `connect_channel` (W; `tools_ads.connect_google_ads` is its alias) and the reading commands
  `get_connection_status`, `get_channel_status`, `get_status_issues`, `get_platform_statement`, each a slice of
  `MarketingState`; `tools_ads_run.py` keeps `get_report` and `pause` / `resume` (a channel, or "all").
- `migrate.py` the one-off data moves `db.apply_schema` runs once each after the DDL, recorded in `schema_migration`
  (`channel_tables_from_ads`: A3's `ads_campaign` / `ads_day` rows into `channel_account` / `channel_campaign` /
  `metric_daily`); `secretbox.py` seals owner-side tokens in `channel_account.secret_enc` under
  `AGENT_KEY_ENCRYPTION_KEY`.
- Environment: `AGENT_DATABASE_URL`, `AGENT_PUBLIC_BASE_URL` (`/mcp`, the OAuth issuer, `/connect/docs` and the paste
  prompts), `AGENT_WEB_BASE_URL` (spec 2026-09-29 §3: every browser page, link and cookie — Google sign-in, claim /
  plan / setup (`/?setup=`) / e-mail `/m/` links, Stripe success/cancel, `connect_url`s, the MCP consent page; unset = `AGENT_PUBLIC_BASE_URL`), `PORT`, `BIDX_BUILD_SHA`, `AGENT_ENV` (`local` | `dev` | `prod`),
  `AGENT_ANON_TURNS` (the homepage's sign-in wall, D39: `0` (default) = the wall waits for a `ready` consultation;
  `N > 0` = the wall appears after the anonymous visitor has answered `N` questions; negative or non-integer refuses to boot),
  `META_MODEL_API_KEY` (part 2; unset = the LLM tools answer with a readable error), `AGENT_LLM_EFFORT` (optional
  `reasoning_effort` `minimal`|`low`|`medium`|`high`|`xhigh`; unset = `low` — the model's own default made a `start` take
  76 s on dev, `low` 27 s; anything else refuses to boot),
  `AGENT_LLM_BASE_URL` (default `https://api.meta.ai/v1`), `AGENT_START_DAILY_BUDGET` (`start` calls per day, keyed
  and anonymous, all addresses; default 500), `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` (`/connect` sign-in; unset =
  the page says sign-in is not configured; the OAuth client must list `<AGENT_WEB_BASE_URL>/connect/google/callback`
  as an authorised redirect URI), `GOOGLE_ADS_MCC_ID` / `GOOGLE_ADS_DEVELOPER_TOKEN` / `GOOGLE_ADS_REFRESH_TOKEN`
  (Admiry's Google Ads manager account: its Customer ID, its API Center developer token, and the refresh token
  `agent.ops google-ads-auth` prints; the OAuth client above exchanges it), `GOOGLE_ADS_LINK_MODE` (`api` | `manual`;
  unset = `api` when those three and the OAuth client are set, else `manual`; `api` with one missing refuses to boot),
  `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` (prepaid top-ups and refunds; either unset = `/pay` and the balance
  tools say payments are not set up and `/hooks/stripe` answers 503; the Stripe endpoint is
  `<AGENT_PUBLIC_BASE_URL>/hooks/stripe` with events `checkout.session.completed`,
  `checkout.session.async_payment_succeeded`, `checkout.session.expired`), `AGENT_TOPUP_MIN_USD` / `AGENT_TOPUP_MAX_USD`
  (one top-up's bounds in whole dollars, default 200 / 5000; `0 < min ≤ max` or the service refuses to boot),
  `AGENT_EXEC_<CHANNEL>` (M0: `off` | `validate` | `live` per channel — `AGENT_EXEC_GOOGLE_SEARCH`,
  `AGENT_EXEC_NEWSBREAK`, …; unset = `off`, anything else refuses to boot; while `AGENT_EXEC_GOOGLE_SEARCH` is unset,
  A3's `AGENT_ADS_EXECUTION` answers for Google Search alone, one release), `AGENT_LAUNCH_ALLOWLIST` (owners' e-mails,
  comma-separated: set, only their businesses launch while a switch is on and every other one runs as under `off`;
  empty = no list), `ADS_OPS_DAILY_BUDGET` (Google Search's
  operations a day, default 2300), `NEWSBREAK_ACCESS_TOKEN` / `NEWSBREAK_ORG_ID` (M1: Admiry's NewsBreak token, an Org
  Admin's, and its Organization's id; either unset = NewsBreak is not configured and nothing calls it; the token never
  leaves `settings`), `AGENT_KEY_ENCRYPTION_KEY` (seals owner-side tokens in
  `channel_account.secret_enc`; unset or empty = the service boots and a token cannot be stored; set, a hex string of
  at least 32 bytes, else the service refuses to boot; no M0 channel stores one, and the compose files do not pass it
  yet). `docs/ops/config-reference.md` §2.6 is the key inventory.
  The image excludes `agent/doc/` and `agent/tests/` (root `.dockerignore`).
