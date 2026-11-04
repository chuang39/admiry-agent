-- agent service schema (spec 2026-09-24 §4.4). Idempotent: every statement is IF NOT EXISTS.
-- Applied by agent.db.apply_schema() at process start and by the test fixture.

CREATE TABLE IF NOT EXISTS business (
  id                    TEXT PRIMARY KEY,
  slug                  TEXT UNIQUE,                       -- NULL while claim_state = 'draft'
  owner_email           TEXT,                              -- Google sign-in subject (part 2 / A1b)
  locale                TEXT NOT NULL DEFAULT 'en-US',
  status                TEXT NOT NULL DEFAULT 'active',    -- active | expired
  claim_state           TEXT NOT NULL DEFAULT 'draft',     -- draft | claimed | verified
  verified_via          TEXT,                              -- gbp | phone_otp
  verified_at           TIMESTAMPTZ,
  claim_expires_at      TIMESTAMPTZ,                       -- claimed-but-unverified deadline (14 d)
  ai_crawlers           BOOLEAN NOT NULL DEFAULT TRUE,
  lead_retention_months INTEGER NOT NULL DEFAULT 24,       -- D19
  notify_email_status   TEXT NOT NULL DEFAULT 'ok',        -- ok | bounced | unsubscribed
  operator_flags        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge (
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  json        JSONB NOT NULL,
  source      TEXT NOT NULL,                               -- extract | owner | gbp | handoff | migration | check (A3p-5 R1-c)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (business_id, version)
);

CREATE TABLE IF NOT EXISTS api_key (
  id           TEXT PRIMARY KEY,
  key_hash     TEXT NOT NULL UNIQUE,                       -- sha256 of the plaintext; plaintext shown once
  business_id  TEXT REFERENCES business(id) ON DELETE CASCADE,   -- NULL for platform keys
  label        TEXT NOT NULL,                              -- muse | claude | chatgpt | web | draft | ops
  class        TEXT NOT NULL,                              -- draft | business | platform
  scopes       TEXT[] NOT NULL,                            -- read | manage | leads | account
  expires_at   TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS goal (
  id                 TEXT PRIMARY KEY,
  business_id        TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  goal_type          TEXT NOT NULL DEFAULT 'customers',
  target             INTEGER,
  monthly_limit_usd  NUMERIC(10,2),
  channel_caps       JSONB NOT NULL DEFAULT '{}'::jsonb,
  focus_services     TEXT[] NOT NULL DEFAULT '{}',
  autopilot          TEXT NOT NULL DEFAULT 'safe',         -- off | safe | all
  network_share_cap  NUMERIC(4,3) NOT NULL DEFAULT 0,      -- D4 default 0, D20 hard max 0.2
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan (
  id             TEXT PRIMARY KEY,
  business_id    TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  goal_id        TEXT REFERENCES goal(id) ON DELETE SET NULL,
  version        INTEGER NOT NULL,
  json           JSONB NOT NULL,                           -- sections, spec §4.1
  status         TEXT NOT NULL DEFAULT 'draft',            -- draft | in_review | approved | superseded
  estimate_basis JSONB NOT NULL DEFAULT '{}'::jsonb,
  diff_from      TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (business_id, version)
);

CREATE TABLE IF NOT EXISTS proposal (
  id           TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  class        TEXT NOT NULL,                              -- spend | nonspend
  kind         TEXT NOT NULL,                              -- approve_plan | set_budget | ...
  payload      JSONB NOT NULL,
  summary      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'proposed',           -- proposed | confirmed | declined | expired
  created_by   TEXT NOT NULL DEFAULT 'assistant',          -- assistant | web
  expires_at   TIMESTAMPTZ NOT NULL,
  resolved_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approval_code (
  proposal_id TEXT NOT NULL REFERENCES proposal(id) ON DELETE CASCADE,
  code_hash   TEXT NOT NULL,
  issued_via  TEXT NOT NULL,                               -- plan_page | ops (email retired: D58, approval codes are never e-mailed)
  used_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (proposal_id, code_hash)
);

-- Append-only audit (spec §5.5). Also the ledger rate limits and idempotent replay read from.
CREATE TABLE IF NOT EXISTS agent_action (
  id              BIGSERIAL PRIMARY KEY,
  business_id     TEXT,
  actor           TEXT NOT NULL,                           -- key:<api_key.id> | ip:<addr> | job:<name> | web:<email>
  actor_class     TEXT,                                    -- draft | business | platform | anonymous | job
  api_key_id      TEXT,
  tool            TEXT NOT NULL,
  args_redacted   JSONB NOT NULL DEFAULT '{}'::jsonb,
  args_hash       TEXT NOT NULL,
  result          JSONB,
  ok              BOOLEAN NOT NULL DEFAULT TRUE,
  approval_kind   TEXT,                                    -- none | assistant | code
  approval_ref    TEXT,
  idempotency_key TEXT,
  ip              TEXT,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_action_key_ts ON agent_action (api_key_id, ts);
CREATE INDEX IF NOT EXISTS agent_action_ip_tool_ts ON agent_action (ip, tool, ts);
CREATE INDEX IF NOT EXISTS agent_action_business_ts ON agent_action (business_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS agent_action_idem
  ON agent_action (business_id, tool, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_run (
  id          BIGSERIAL PRIMARY KEY,
  job         TEXT NOT NULL,
  shard       TEXT NOT NULL DEFAULT '0/1',
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  ok          BOOLEAN,
  details     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS job_run_job_started ON job_run (job, started_at DESC);

CREATE TABLE IF NOT EXISTS benchmark (
  id         TEXT PRIMARY KEY,
  category   TEXT NOT NULL,
  geo        TEXT NOT NULL DEFAULT 'US',
  channel    TEXT NOT NULL,                                -- google_search | meta | lsa
  cpl_low    NUMERIC(10,2) NOT NULL,
  cpl_high   NUMERIC(10,2) NOT NULL,
  cpl_avg    NUMERIC(10,2) NOT NULL,
  source_url TEXT NOT NULL,
  as_of      INTEGER NOT NULL,
  confidence TEXT NOT NULL DEFAULT 'low',
  UNIQUE (category, geo, channel)
);

-- A1b part 1 (2026-09-25): sign-in and claims. Tokens are stored hashed (sha256 hex); plaintexts travel once.

-- The connect link `start` (and request_connect_link) hands out: a 24-hour capability to claim the draft
-- business after Google sign-in. Consumed by the claim, not by being opened.
CREATE TABLE IF NOT EXISTS claim_link (
  token_hash  TEXT PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_link_business ON claim_link (business_id);

-- A Google sign-in in flight: OAuth state + PKCE verifier, single use, LOGIN_TTL; the rows also count the
-- per-address sign-in limit (spec §5.5: OAuth 20/min/IP), so no audit row is written for a click.
-- login_hash binds the attempt to the browser that started it (the admiry_login cookie's nonce, hashed).
CREATE TABLE IF NOT EXISTS login_attempt (
  state         TEXT PRIMARY KEY,
  code_verifier TEXT NOT NULL,
  login_hash    TEXT NOT NULL,
  next_path     TEXT NOT NULL DEFAULT '/connect',
  ip            TEXT NOT NULL,
  used_at       TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempt_ip_created ON login_attempt (ip, created_at);

-- The owner's browser session: the cookie carries the token, the row its hash; csrf guards every POST.
CREATE TABLE IF NOT EXISTS web_session (
  token_hash   TEXT PRIMARY KEY,
  email        TEXT NOT NULL,                              -- lower-cased, verified by Google
  google_sub   TEXT NOT NULL,
  csrf         TEXT NOT NULL,
  ip           TEXT,
  expires_at   TIMESTAMPTZ NOT NULL,
  revoked_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS web_session_email ON web_session (email);

-- A1b part 2 (2026-09-25, spec 2026-09-25 rev. 2 §4): the Google Ads Management Agreement and the link of the owner's
-- own Google Ads account to Admiry's manager account.

-- Click-wrap acceptances, append-only: "accepted" = a row for the business with the version of the legal file that
-- ships (agent/legal/management-agreement-v*.md front matter).
CREATE TABLE IF NOT EXISTS agreement_acceptance (
  id          BIGSERIAL PRIMARY KEY,
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  email       TEXT NOT NULL,
  version     TEXT NOT NULL,
  ip          TEXT,
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agreement_acceptance_business_version ON agreement_acceptance (business_id, version);

-- One link per business, one business per Customer ID (D26). status 'manual' = registered by an operator from the
-- manager account UI (agent.ops ads-link --status active); treated as active. resource_name is Google's
-- customers/{mcc}/customerClientLinks/{client}~{manager_link_id}. last_error is an owner-readable sentence.
CREATE TABLE IF NOT EXISTS ads_link (
  business_id   TEXT PRIMARY KEY REFERENCES business(id) ON DELETE CASCADE,
  customer_id   TEXT NOT NULL UNIQUE CHECK (customer_id ~ '^[0-9]{10}$'),
  status        TEXT NOT NULL CHECK (status IN ('pending', 'active', 'refused', 'cancelled', 'inactive', 'manual')),
  resource_name TEXT,
  invited_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  checked_at    TIMESTAMPTZ,
  last_error    TEXT
);

-- own_account (the owner's Google Ads account, linked) | managed (an Admiry-owned client account, prepaid; A1b-3).
-- business.verified_via (TEXT, no CHECK) gains 'ads_link' here and 'payment' in A1b-3: no DDL needed.
ALTER TABLE business ADD COLUMN IF NOT EXISTS ads_mode TEXT NOT NULL DEFAULT 'own_account';

-- A1b part 3 (2026-09-26): the prepaid balance of managed businesses and payment-as-verification (spec 2026-09-25
-- rev. 2 D29, D30, §4, §5; the wallet design of rev. 1 §3, commit 202668c). Money is integer cents. wallet_entry rows
-- are never updated or deleted: balance = SUM(amount_cents). ref is the idempotency key, unique per business: the
-- Checkout Session id (topup), "{channel}:{day}" (spend, A3's wallet-settle), the Stripe refund id (refund),
-- ops:<ns>:<hex> (adjustment). No ON DELETE CASCADE: money rows never disappear with a business.
CREATE TABLE IF NOT EXISTS wallet_entry (
  id           BIGSERIAL PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES business(id),
  kind         TEXT NOT NULL CHECK (kind IN ('topup', 'spend', 'refund', 'adjustment')),
  amount_cents BIGINT NOT NULL CHECK (amount_cents <> 0),
  ref          TEXT NOT NULL,
  note         TEXT,
  created_by   TEXT NOT NULL CHECK (created_by IN ('stripe', 'job', 'ops')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (business_id, ref),
  CHECK ((kind = 'topup' AND amount_cents > 0) OR (kind IN ('spend', 'refund') AND amount_cents < 0)
         OR kind = 'adjustment')
);

-- One row per Stripe Checkout Session this server created; the webhook credits only these (a session another
-- server created on the same Stripe account is ignored). payment_intent (set by the webhook) and refunded_cents
-- let agent.ops refund return money to the card that paid it.
CREATE TABLE IF NOT EXISTS payment_session (
  id             TEXT PRIMARY KEY,                           -- Stripe Checkout Session id (cs_…)
  business_id    TEXT NOT NULL REFERENCES business(id),
  amount_cents   BIGINT NOT NULL CHECK (amount_cents > 0),
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paid', 'expired')),
  payment_intent TEXT,
  refunded_cents BIGINT NOT NULL DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  paid_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS payment_session_business_created ON payment_session (business_id, created_at DESC);

-- 'no_funds' (set by spend-guard, cleared by a top-up that brings the balance above zero) or NULL;
-- A3 adds 'operator'. verified_via now also takes 'payment' (TEXT, no CHECK: no DDL needed).
ALTER TABLE business ADD COLUMN IF NOT EXISTS paused_reason TEXT;

-- Which legal text a click-wrap row accepts: 'management-agreement' (every row A1b-2 wrote) or 'prepayment-addendum'
-- (managed businesses, D28). "Accepted" = a row for the business and document with that document's shipping version.
ALTER TABLE agreement_acceptance ADD COLUMN IF NOT EXISTS document TEXT NOT NULL DEFAULT 'management-agreement';

-- Channels spec rev. 4 D92 (plan 2026-10-04-agent-m1m-1): the payment cost is paid at the door. amount_cents is
-- what Stripe charges (the credit plus the cost); credited_cents is what the balance gets; processing_cents is the
-- cost Stripe keeps; rail is 'card' or 'ach' (wallet.RAILS). Rows from before: NULL, read as v0's terms.
ALTER TABLE payment_session ADD COLUMN IF NOT EXISTS rail TEXT;
ALTER TABLE payment_session ADD COLUMN IF NOT EXISTS credited_cents BIGINT;
ALTER TABLE payment_session ADD COLUMN IF NOT EXISTS processing_cents BIGINT;
-- L1 (spec 2026-10-10 §5): Agree and pay -- the plan version a Checkout was started for, which its credit approves
-- while AGENT_LAUNCH_REVIEW is on (setup.approve_paid); NULL for a top-up (Add funds)
ALTER TABLE payment_session ADD COLUMN IF NOT EXISTS plan_id TEXT;
-- A bank debit completes days before it is paid: 'processing' until Stripe says paid or 'failed'
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'payment_session_status_check' AND pg_get_constraintdef(oid) LIKE '%processing%'
  ) THEN
    ALTER TABLE payment_session DROP CONSTRAINT IF EXISTS payment_session_status_check;
    ALTER TABLE payment_session ADD CONSTRAINT payment_session_status_check
      CHECK (status IN ('open', 'processing', 'paid', 'failed', 'expired'));
  END IF;
END $$;

-- A1b-4a (spec 2026-09-26 §8): one consultation per plan; answers keyed by question id.
CREATE TABLE IF NOT EXISTS consult_session (
  id            TEXT PRIMARY KEY,
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  tree          TEXT NOT NULL,
  locale        TEXT NOT NULL DEFAULT 'en-US',
  opening_text  TEXT NOT NULL,
  known_facts   JSONB NOT NULL DEFAULT '{}'::jsonb,
  answers       JSONB NOT NULL DEFAULT '{}'::jsonb,
  options       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- frozen labels per question id
  step          INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'open',          -- open | ready | planned | superseded | expired
  plan_id       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS consult_session_one_open ON consult_session (business_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS consult_session_business ON consult_session (business_id, created_at DESC);

-- A1b-4c Task 3 (spec §3 D41): the website's hand-off token to an assistant. Only the sha256 is stored;
-- take_handoff clears both columns on its one use.
ALTER TABLE consult_session ADD COLUMN IF NOT EXISTS handoff_hash TEXT;
ALTER TABLE consult_session ADD COLUMN IF NOT EXISTS handoff_expires_at TIMESTAMPTZ;

-- A1b-4a (spec 2026-09-26 §7): funnel factors per category; null = read fallback's.
ALTER TABLE benchmark ADD COLUMN IF NOT EXISTS lead_to_booking NUMERIC(4,3);
ALTER TABLE benchmark ADD COLUMN IF NOT EXISTS booking_to_customer NUMERIC(4,3);

-- A1b-4a final review C1: who set the goal's monthly limit. 'owner' (set_goal, start's stated budget) is the
-- owner's budget; 'system' is the ceiling recommend wrote when the owner named none (its aggressive level), which
-- adjust enforces but never reads back as a stated budget.
ALTER TABLE goal ADD COLUMN IF NOT EXISTS limit_source TEXT NOT NULL DEFAULT 'owner';

-- A1b-4b (spec 2026-09-26 §6, D35): MCP OAuth 2.1 -- FastMCP's authorization server on Postgres. An access
-- token is an api_key row (oa_...); refresh tokens rotate and a reused one revokes its grant.
CREATE TABLE IF NOT EXISTS oauth_client (
  id           TEXT PRIMARY KEY,
  client_name  TEXT,
  redirect_uris JSONB NOT NULL,
  info         JSONB NOT NULL,                              -- the full OAuthClientInformationFull, as JSON
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  disabled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_txn (
  id          TEXT PRIMARY KEY,
  client_id   TEXT NOT NULL REFERENCES oauth_client(id),
  params      JSONB NOT NULL,                                -- AuthorizationParams, as JSON
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_grant (
  id            TEXT PRIMARY KEY,
  client_id     TEXT NOT NULL REFERENCES oauth_client(id),
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  owner_email   TEXT NOT NULL,
  scopes        TEXT[] NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS oauth_grant_business ON oauth_grant (business_id, created_at DESC);

CREATE TABLE IF NOT EXISTS oauth_code (
  code_hash     TEXT PRIMARY KEY,
  grant_id      TEXT NOT NULL REFERENCES oauth_grant(id) ON DELETE CASCADE,
  client_id     TEXT NOT NULL,
  scopes        TEXT[] NOT NULL,
  code_challenge TEXT NOT NULL,
  redirect_uri  TEXT,
  redirect_uri_provided_explicitly BOOLEAN NOT NULL DEFAULT TRUE,
  resource      TEXT,
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_token (
  id                 TEXT PRIMARY KEY,
  grant_id           TEXT NOT NULL REFERENCES oauth_grant(id) ON DELETE CASCADE,
  api_key_id         TEXT NOT NULL REFERENCES api_key(id) ON DELETE CASCADE,
  refresh_hash       TEXT NOT NULL UNIQUE,
  refresh_expires_at TIMESTAMPTZ NOT NULL,
  rotated_from       TEXT,
  used_at            TIMESTAMPTZ,
  revoked_at         TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- P8: a crude per-address rate limit on the OAuth endpoints (register/authorize/token).
CREATE TABLE IF NOT EXISTS oauth_hit (
  ip TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oauth_hit_ip_ts ON oauth_hit (ip, ts);

-- An OAuth access token is an api_key row: source distinguishes it from an ak_ key minted by /connect or
-- `start`, and grant_id ties it back to the oauth_grant it belongs to (NULL for ordinary keys).
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'key';
ALTER TABLE api_key ADD COLUMN IF NOT EXISTS grant_id TEXT;

-- Final review I4: per-grant rate limits look up every access token of a grant; revoke_grant sweeps both.
CREATE INDEX IF NOT EXISTS api_key_grant ON api_key (grant_id);
CREATE INDEX IF NOT EXISTS oauth_token_grant ON oauth_token (grant_id);

-- A3p-1 (2026-09-28): Admiry Pages. One page per business is live at a time (page_one_live); an owner's own URL
-- is a page row too (source 'owner'), so the check and the events have somewhere to hang. Lead contact fields
-- live only in lead.fields (D19 retention applies; audit rows never carry them: audit.SECRET_FIELDS "contact").
CREATE TABLE IF NOT EXISTS page (
  id            TEXT PRIMARY KEY,                 -- pg_…, also the snippet's token
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  source        TEXT NOT NULL CHECK (source IN ('owner', 'hosted', 'admiry')),
  url           TEXT NOT NULL,                    -- the page the ads use
  hostname      TEXT,                             -- hosted/admiry: <slug>.business.admiry.ai (A3p-2)
  version       INT NOT NULL DEFAULT 1,
  html          TEXT,                             -- hosted/admiry (A3p-2)
  sections      JSONB,                            -- admiry-built: the structured content (A3p-2)
  status        TEXT NOT NULL CHECK (status IN ('preview', 'published', 'unpublished')),
  check_json    JSONB, checked_at TIMESTAMPTZ,
  kit_plan_id   TEXT, kit_sent_at TIMESTAMPTZ,     -- the plan version the kit was last e-mailed for
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS page_one_live ON page (business_id) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS page_business ON page (business_id, created_at DESC);
CREATE TABLE IF NOT EXISTS page_event (
  id BIGSERIAL PRIMARY KEY, page_id TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('visit','call','mail','book','form','booked_online')),
  click_id TEXT, click_id_kind TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS page_event_page_ts ON page_event (page_id, ts DESC);
CREATE TABLE IF NOT EXISTS lead (
  id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  page_id TEXT REFERENCES page(id) ON DELETE SET NULL, kind TEXT NOT NULL CHECK (kind IN ('form','call','other','booking')),
  fields JSONB NOT NULL DEFAULT '{}', click_id TEXT, click_id_kind TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','contacted','booked','won','lost','spam')),
  note TEXT, notified_at TIMESTAMPTZ, uploaded_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_business_created ON lead (business_id, created_at DESC);
CREATE TABLE IF NOT EXISTS conversion_action (
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, resource_name TEXT NOT NULL, label TEXT, PRIMARY KEY (business_id, kind)
);

-- A3 batch 1 (2026-09-29, spec 2026-09-28 admiry-google-search-execution-design §9): one row per business for its
-- Google Search ads (agent/ads_run.py: the state, the plan version running or queued, Google's resource names, the
-- last error as an owner sentence), and its daily numbers (ads-sync, A3 Task 5). `state` gains 'validated' (plan P5:
-- the `validate` switch sends every mutate validateOnly, so nothing is built).
CREATE TABLE IF NOT EXISTS ads_campaign (
  business_id   TEXT PRIMARY KEY REFERENCES business(id) ON DELETE CASCADE,
  customer_id   TEXT NOT NULL,                    -- the owner's account (ads_link.customer_id)
  plan_id       TEXT NOT NULL,                    -- the version running (or queued)
  state         TEXT NOT NULL CHECK (state IN ('queued','launching','validated','live','paused','limited','failed')),
  resources     JSONB NOT NULL DEFAULT '{}',      -- budget, campaign, ad groups, ads, assets, conversion action
  last_error    TEXT,
  validated_at  TIMESTAMPTZ, launched_at TIMESTAMPTZ, paused_at TIMESTAMPTZ, synced_at TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ads_day (
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  day           DATE NOT NULL,
  cost_micros   BIGINT NOT NULL, clicks INT NOT NULL, impressions INT NOT NULL,
  phone_calls   INT NOT NULL, conversions NUMERIC NOT NULL,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (business_id, day)
);

-- A1b-5 e-mail channel (spec 2026-09-28 §7; plan P2: the thread token is plaintext, it grants nothing).
CREATE TABLE IF NOT EXISTS email_thread (
  id           TEXT PRIMARY KEY,                           -- et_…
  business_id  TEXT REFERENCES business(id) ON DELETE CASCADE,  -- NULL until a known sender picks one, or start creates one
  session_id   TEXT,                                       -- the consult_session this thread drives, when one exists
  counterpart  TEXT NOT NULL,                              -- the one address this thread talks to (lower-cased)
  token        TEXT NOT NULL UNIQUE,                       -- the Reply-To plus-address token (locates; never authorizes)
  subject      TEXT,
  status       TEXT NOT NULL DEFAULT 'open',               -- open | operator | closed
  last_change  JSONB,                                      -- what "undo" puts back: {"tool", "plan_before", "plan_after"}
  unclear      INTEGER NOT NULL DEFAULT 0,                 -- consecutive messages the reader could not place
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_in_at   TIMESTAMPTZ,
  last_out_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS email_thread_business ON email_thread (business_id, created_at DESC);
CREATE INDEX IF NOT EXISTS email_thread_counterpart ON email_thread (counterpart, created_at DESC);

CREATE TABLE IF NOT EXISTS email_message (
  id           TEXT PRIMARY KEY,                           -- em_…
  thread_id    TEXT REFERENCES email_thread(id) ON DELETE SET NULL,
  direction    TEXT NOT NULL,                              -- in | out
  message_id   TEXT NOT NULL UNIQUE,                       -- RFC 5322 Message-ID: the dedupe key and the tool call's idempotency key
  provider_id  TEXT,                                       -- Resend's id (in: to fetch the content; out: what the send returned)
  from_addr    TEXT NOT NULL,
  to_addrs     TEXT[] NOT NULL DEFAULT '{}',
  subject      TEXT,
  new_text     TEXT,                                       -- in: the sender's own new words; out: the body sent; NULL once purged
  intent       JSONB,                                      -- what the state or the reader made of it
  outcome      TEXT NOT NULL,                              -- received | answered | queued | failed | sent | dropped:<rule> | refused:<rule>
  sent_by      TEXT,                                       -- out: admiry | ops
  key_id       TEXT,                                       -- in: the per-message api_key row the tools ran under (plan P4)
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  purged_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS email_message_thread ON email_message (thread_id, created_at);
CREATE INDEX IF NOT EXISTS email_message_from_ts ON email_message (from_addr, created_at);
CREATE INDEX IF NOT EXISTS email_message_outcome_ts ON email_message (outcome, created_at);

CREATE TABLE IF NOT EXISTS email_grant (
  id           TEXT PRIMARY KEY,                           -- eg_…
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  address      TEXT NOT NULL,                              -- lower-cased
  scopes       TEXT[] NOT NULL,                            -- read | manage | leads (never account: D57)
  via          TEXT NOT NULL,                              -- web_step | connect
  created_by   TEXT NOT NULL,                              -- the signer's e-mail
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS email_grant_live ON email_grant (business_id, address) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS email_link (
  token_hash   TEXT PRIMARY KEY,                           -- sha256 of the /m/{token} capability (7 days, one use)
  thread_id    TEXT NOT NULL REFERENCES email_thread(id) ON DELETE CASCADE,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  expires_at   TIMESTAMPTZ NOT NULL,
  used_at      TIMESTAMPTZ
);

-- A3p-2 Task 1 (2026-09-30, spec 2026-09-28-admiry-pages-design.md §9, plan
-- 2026-09-30-agent-a3p-2-hosted-pages-and-templates.md H2/H7/H8/H13/H16): a page's own linear version
-- history, an unpublished preview (a token good for hosted.PREVIEW_TTL; embed_origin lets Task 7's homepage
-- card frame it), and the owner's uploaded photos -- all hang off A3p-1's page row by page.id. page.hostname
-- (A3p-1) gets its one-hostname-per-business index here (H2): a ('hosted', 'admiry') source page is the one
-- that ever sets it.
CREATE TABLE IF NOT EXISTS page_version (
  page_id     TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  version     INT NOT NULL, html TEXT NOT NULL, title TEXT,
  source      TEXT NOT NULL CHECK (source IN ('hosted', 'admiry')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (page_id, version)
);
CREATE TABLE IF NOT EXISTS page_preview (
  token        TEXT PRIMARY KEY,                   -- pv_…, 24 h, unguessable (db.new_id)
  page_id      TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  html         TEXT NOT NULL, title TEXT, sections JSONB,
  embed_origin TEXT,                                -- H16: NULL until Task 7 sets it to the homepage's origin
  expires_at   TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS page_photo (
  id          TEXT PRIMARY KEY,                    -- ph_…
  page_id     TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  bytes       BYTEA NOT NULL, content_type TEXT NOT NULL, width INT NOT NULL, height INT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A3p-2 Task 3 round 2 (H25): before this fix, an owner's own page could carry its own external domain in
-- page.hostname (set_owner_url wrote it) -- a column every reader now trusts as Admiry's *hosted* hostname
-- only (H22) and, since round 2, only for a ('hosted', 'admiry')-source row (hosted.serve, pages_ask,
-- landing.final_host). Round 3 (H27, S1b): this runs at every boot, so it must not also null a slug H25
-- *legitimately* keeps on a row that went hosted then owner (that row has `page_version` rows from when it
-- really was hosted; a pre-H22 legacy row never does, since page_version is hosted-content-only) -- without
-- the NOT EXISTS guard, a restart freed a kept slug for any other business to mint, while this one's own live
-- ads kept pointing at it (a cross-tenant hostname collision, not merely a stale value). Round 4 (H28): it
-- sits here, after page_version's CREATE -- the file runs as one query, so a statement naming a table the
-- file creates further down fails on an empty database (CI, a first boot) -- and before page_hostname_one,
-- whose unique build an A3p-1 database (two owner pages on one external host, both in hostname) would refuse.
UPDATE page SET hostname = NULL WHERE source = 'owner' AND hostname IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM page_version v WHERE v.page_id = page.id);
CREATE UNIQUE INDEX IF NOT EXISTS page_hostname_one ON page (hostname) WHERE hostname IS NOT NULL;
CREATE INDEX IF NOT EXISTS page_photo_page ON page_photo (page_id);

-- A3p-2 Task 6 (2026-09-30, plan H13, ledger H35/H37): the owner's own upload link -- create_page mints one
-- (agent/uploads.py: GET renders the form, POST stores what was sent), update_page/revise_page just read the
-- newest still-valid one back. Shaped like page_preview: a bare token good for a fixed TTL, unguessable,
-- nothing else identifies it.
CREATE TABLE IF NOT EXISTS upload_token (
  token       TEXT PRIMARY KEY,                    -- up_…, 24 h, unguessable (db.new_id)
  page_id     TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS upload_token_page ON upload_token (page_id, created_at DESC);

-- Support tickets and plan feedback (spec 2026-09-30 support-and-feedback §3, D130–D148). A business has at most one
-- ticket that is not closed; ticket_event is append-only and is also the ticket mail's outbox (mail_state, D142).
CREATE TABLE IF NOT EXISTS support_ticket (
  id              TEXT PRIMARY KEY,                                   -- tk_…
  business_id     TEXT REFERENCES business(id) ON DELETE CASCADE,     -- NULL: an e-mail thread with no business
  source          TEXT NOT NULL,          -- home | mcp | email | feedback
  reason          TEXT NOT NULL,          -- help | human | dissatisfied | other | website (A3p-5 batch 2)
  status          TEXT NOT NULL,          -- awaiting_contact | open | answered | closed
  resolution      TEXT,                   -- on close: resolved | not_support | duplicate | no_reply | no_contact
  contact_email   TEXT,                   -- verified (D133), except an address an owner typed on the website card
                                          -- (A3p-5 batch 2, R3-b) or a visitor typed on the Help button (spec
                                          -- 2026-10-12: contact_verified false); NULL while awaiting_contact
  summary         TEXT NOT NULL,          -- one line, <= 200 characters
  email_thread_id TEXT REFERENCES email_thread(id) ON DELETE SET NULL,
  snapshot        JSONB NOT NULL,         -- transcript.build when the ticket opened (§7)
  first_reply_at  TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS support_ticket_one_open
  ON support_ticket (business_id) WHERE status <> 'closed' AND business_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS support_ticket_thread_open
  ON support_ticket (email_thread_id) WHERE status <> 'closed' AND email_thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS support_ticket_queue ON support_ticket (status, updated_at);

-- The Help button (spec 2026-10-12 §3.3, §3.5): a visitor who is not signed in types an address nobody verified.
-- False for such an address; true for every row from before (every contact was Google's or the e-mail channel's) and
-- for every signed-in one. Admiry sends an unverified address nothing on its own (no acknowledgement); a person who
-- replies from /ops/tasks writes to it knowingly. The claim, or a signed-in ask, makes it the verified one
-- (support._verify) and the acknowledgement goes then.
ALTER TABLE support_ticket ADD COLUMN IF NOT EXISTS contact_verified BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS ticket_event (
  id               TEXT PRIMARY KEY,      -- te_…
  ticket_id        TEXT NOT NULL REFERENCES support_ticket(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,         -- created | owner_words | ack | notice | reply_out | reply_in | note | status | thread | contact
  actor            TEXT NOT NULL,         -- web:<email> | key:<id> | admin:<email> | email:<addr> | visitor:<addr> | ops:cli | admiry | job:…
  via              TEXT,                  -- home | mcp | email | feedback | ops
  body             TEXT,                  -- the words, in full, <= 4000 characters
  data             JSONB NOT NULL DEFAULT '{}'::jsonb,
  mail_state       TEXT,                  -- the outbox (ack / notice / reply_out): pending | sent | failed
  mail_attempts    INTEGER NOT NULL DEFAULT 0,
  email_message_id TEXT,                  -- the out row once sent
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_event_ticket ON ticket_event (ticket_id, created_at);
CREATE INDEX IF NOT EXISTS ticket_event_outbox ON ticket_event (created_at) WHERE mail_state IN ('pending', 'failed');

CREATE TABLE IF NOT EXISTS plan_feedback (
  id          TEXT PRIMARY KEY,           -- fb_…
  plan_id     TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  surface     TEXT NOT NULL,              -- home | plan_page
  rating      TEXT NOT NULL,              -- up | down
  reasons     TEXT[] NOT NULL DEFAULT '{}',   -- budget | area | copy | business | other (down only)
  comment     TEXT,                       -- <= 1000 characters (down only)
  actor       TEXT NOT NULL,              -- web:<email>
  ticket_id   TEXT REFERENCES support_ticket(id) ON DELETE SET NULL,
  snapshot    JSONB NOT NULL,             -- transcript.build(plan_id=…) at the vote (§7)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (plan_id, actor)
);
CREATE INDEX IF NOT EXISTS plan_feedback_time ON plan_feedback (created_at);

-- M0 channel framework (spec 2026-09-29-admiry-channels-design §8; plan 2026-09-29-agent-m0-channel-framework Task 1).
-- One-off data moves (A3's ads_campaign / ads_day rows into the tables below, P6's channel_account seed) are Python
-- functions in agent/migrate.py, run once each after this file and recorded here.
CREATE TABLE IF NOT EXISTS schema_migration (
  name         TEXT PRIMARY KEY,
  applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS channel_account (
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,                       -- google_search | microsoft_search | meta | nextdoor | lsa | newsbreak | reddit | chatgpt_ads
  mode         TEXT NOT NULL CHECK (mode IN ('own_account', 'managed')),
  status       TEXT NOT NULL,                       -- none | pending | active | refused | inactive | needs_attention
  external     JSONB NOT NULL DEFAULT '{}',         -- customer_id, account_number, ad_account_id, page_id, advertiser_id, org/account ids; newsbreak: ad_account_id, event_id (the lead event)
  secret_enc   BYTEA,                               -- owner-side tokens sealed by agent/secretbox.py under AGENT_KEY_ENCRYPTION_KEY; never in rows' JSON, logs or audit
  checked_at   TIMESTAMPTZ, last_error TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (business_id, channel)
);
-- ads_link stays as the Google link's mechanics (resource_name, invited_at); the google_search adapter reads it and
-- mirrors its status into channel_account. business.ads_mode is read only by the migration that seeds the
-- google_search row, then ignored.
-- as built (M0): google_ads_link.mirror writes that row (mode from business.ads_mode); ads_mode.current falls back to the column and the prepaid paths read it until M1m
CREATE TABLE IF NOT EXISTS channel_campaign (
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  plan_id      TEXT NOT NULL,                       -- the version running (or queued)
  state        TEXT NOT NULL CHECK (state IN ('waiting_account','waiting_creative','queued','launching','live','paused','limited','failed','operator')),
  paused_reason TEXT,                               -- owner | month_cap | no_funds | page_down | unreachable | no_leads | dropped | stop_requested | dispute
                                                    -- as built (M0): Admiry's reasons only, never 'owner'; the owner's pause is resources.owner_paused
  resources    JSONB NOT NULL DEFAULT '{}',         -- platform ids: budget, campaign, groups, ads, assets, forms, conversion actions
  last_error   TEXT,
  validated_at TIMESTAMPTZ, launched_at TIMESTAMPTZ, paused_at TIMESTAMPTZ, synced_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (business_id, channel)
);
CREATE TABLE IF NOT EXISTS metric_daily (
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  day          DATE NOT NULL,
  cost_micros  BIGINT NOT NULL, impressions INT NOT NULL, clicks INT NOT NULL,
  calls        INT NOT NULL, leads INT NOT NULL, conversions NUMERIC NOT NULL, booked INT NOT NULL DEFAULT 0,
  source       TEXT NOT NULL CHECK (source IN ('api', 'operator')),
  synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (business_id, channel, day)
);
CREATE TABLE IF NOT EXISTS channel_issue (
  id           BIGSERIAL PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  kind         TEXT NOT NULL,                       -- disapproval | billing | verification | account | suspended
  platform_text TEXT NOT NULL,                      -- the platform's own sentence, quoted verbatim
  since        TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS allocation (
  id           TEXT PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  plan_id      TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('initial', 'rebalance', 'stop_loss')),
  split        JSONB NOT NULL,                      -- {channel: monthly_usd}
  basis        JSONB NOT NULL,                      -- per channel: cost per booked/lead, window, sample sizes, benchmark row ids
  proposal_id  TEXT,                                -- the bundle it produced, when any
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE lead ADD COLUMN IF NOT EXISTS channel TEXT;   -- derived from click_id_kind at insert (registry.channel_for_click)
                                                          -- or set by lead-pull; null = organic

-- G1 (spec 2026-10-01-admiry-mcp-gateway-design §3, §10): the public tool an assistant called, beside the command that
-- ran (`tool`). NULL for the homepage, mail, jobs and a command called directly.
ALTER TABLE agent_action ADD COLUMN IF NOT EXISTS surface_tool TEXT;

-- G1 (spec 2026-10-01 §5, D163): what a lead became. value_usd is what the owner says a won lead was worth; reason
-- says why one was lost or is not a lead; source says where the row came from (a page's form or beacon, a platform's
-- own lead or call record, or the owner's word). kind gains 'other' (a walk-in the owner reports): in CREATE TABLE
-- above for a new database, by the migration lead_kind_other (agent/migrate.py) for one that has the table.
ALTER TABLE lead ADD COLUMN IF NOT EXISTS value_usd NUMERIC(10,2);
ALTER TABLE lead ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE lead ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'page' CHECK (source IN ('page', 'platform', 'owner'));

-- G1 (spec 2026-10-01 §5, D162): the most a new customer may cost, when the owner states it.
ALTER TABLE goal ADD COLUMN IF NOT EXISTS max_cost_per_customer_usd NUMERIC(10,2);

-- G1 Task 18 (spec 2026-10-01 §11): the identity of a public call -- a hash of its tool and what the assistant sent,
-- as it means it (an approval code blanked, a null argument left out, a whole number as one: audit.call_hash) -- on
-- the row of the command it ran. The same call sent again with the same idempotency key within a day is found by it
-- and answered with its first answer, whatever the command behind it would be given now, while that answer still
-- stands. A change that made a plan version stands while that version is the latest (a turn that made none always
-- stands); after that the tool refuses the key as already used and runs no command. The owner's yes (a launch with
-- its code, a page's publish or take-down) is answered again only when nothing waits for it, and stands while the
-- plan it approved is the one asked about and still approved, the page is up at the version it published, or down;
-- otherwise the tool says that nothing waits.
ALTER TABLE agent_action ADD COLUMN IF NOT EXISTS surface_call_hash TEXT;
CREATE INDEX IF NOT EXISTS agent_action_surface_idem ON agent_action (business_id, surface_tool, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND surface_tool IS NOT NULL;

-- plan 2026-10-03-agent-listing-confirm: the places a lookup found close to the business, waiting for the owner's
-- pick (the consultation's first question, update_business_profile's google_listing, Settings). One row per
-- business, replaced by each lookup, closed by a pick or "None of these", read for 24 hours (listings.OFFER_TTL).
-- Nothing here is the business's listing: the knowledge holds only a place the owner confirmed.
CREATE TABLE IF NOT EXISTS listing_offer (
  business_id TEXT PRIMARY KEY REFERENCES business(id) ON DELETE CASCADE,
  candidates  JSONB NOT NULL,                              -- [{id, place_id, name, address, lat, lng}], at most 3
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A3c-1 (2026-10-03; plan 2026-10-03-agent-a3c-1-managed-google; founder decision 2026-10-03, amending spec
-- 2026-09-25 D26): the Google Ads client accounts Admiry runs prepaid (managed) businesses' Google Search ads in --
-- a pool filed by industry, not one account per business. `scope` is `root` (any business), a family (L1,
-- benchmark.CATEGORIES) or `<family>/<trade>` (L2, spec 2026-10-01 §7.6), and names the account in Google Ads
-- (`account_<scope>`, the slash an underscore). `capacity`: the live businesses it takes at most (NULL: no limit; 1 is
-- Google's third-party policy, a separate account for each end-advertiser); `closed` takes no new business. Opened in
-- the manager account's UI (or by API once Basic access allows it), billed to Admiry's payments profile, and added
-- with `agent.ops google-account add`, which reads it from Google first. A Customer ID an owner linked (ads_link) or
-- the manager account itself is never one (agent/google_managed.py checks both).
CREATE TABLE IF NOT EXISTS google_ads_pool (
  customer_id      TEXT PRIMARY KEY CHECK (customer_id ~ '^[0-9]{10}$'),
  scope            TEXT NOT NULL,                     -- root | <family> | <family>/<trade>
  capacity         INTEGER CHECK (capacity IS NULL OR capacity > 0),
  status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  descriptive_name TEXT,                              -- Google's, read when it was added
  time_zone        TEXT,                              -- Google's, read when it was added
  added_by         TEXT NOT NULL,                     -- ops:cli
  added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  note             TEXT
);
CREATE INDEX IF NOT EXISTS google_ads_pool_scope ON google_ads_pool (scope, status);

-- Which pool account each prepaid business's Google Search runs in: one live row per business (the partial unique
-- index -- a business is bound to exactly one account at a time), every earlier one kept with when it was released.
-- `family`/`trade` are the business's industry when it was bound (what the allocator matched); `bound_by` is
-- `job:allocator` (the launch's own pick) or `ops:cli`.
CREATE TABLE IF NOT EXISTS google_ads_binding (
  id           BIGSERIAL PRIMARY KEY,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  customer_id  TEXT NOT NULL REFERENCES google_ads_pool(customer_id),
  family       TEXT NOT NULL,
  trade        TEXT,
  bound_by     TEXT NOT NULL,
  bound_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  released_at  TIMESTAMPTZ,
  note         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS google_ads_binding_live ON google_ads_binding (business_id)
  WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS google_ads_binding_account ON google_ads_binding (customer_id) WHERE released_at IS NULL;

-- A3p-3 batch 1 (2026-10-04; spec 2026-10-03-admiry-pages-a3p-3-design P12-P20, plan
-- 2026-10-04-agent-a3p-3-1-hosting-minimum-and-muse Q4, Q6, Q7, Q16): hosting is free while the business's ads through
-- Admiry run at $200 a month or spend $200 in 30 days (agent/hosting_minimum.py). An operator's exemption on the
-- business (the reason; NULL = the minimum applies); on the page, the countdown (the day the site comes down unless the
-- business is covered again, and which of the countdown's e-mails went), why and when it was taken down, and the day
-- its files go (90 days after a takedown for the minimum; an owner's own unpublish keeps them).
ALTER TABLE business ADD COLUMN IF NOT EXISTS hosting_exempt TEXT;
ALTER TABLE business ADD COLUMN IF NOT EXISTS hosting_exempt_by TEXT;
ALTER TABLE business ADD COLUMN IF NOT EXISTS hosting_exempt_at TIMESTAMPTZ;
ALTER TABLE page ADD COLUMN IF NOT EXISTS countdown_ends DATE;
ALTER TABLE page ADD COLUMN IF NOT EXISTS countdown_mail JSONB;
ALTER TABLE page ADD COLUMN IF NOT EXISTS taken_down_reason TEXT
  CHECK (taken_down_reason IN ('owner', 'hosting_minimum'));
ALTER TABLE page ADD COLUMN IF NOT EXISTS taken_down_at TIMESTAMPTZ;
ALTER TABLE page ADD COLUMN IF NOT EXISTS purge_after DATE;

-- A3p-3 batch 2 (2026-10-04; spec 2026-10-03-admiry-pages-a3p-3-design P24-P30, plan
-- 2026-10-04-agent-a3p-3-2-measured-contacts C1, C4, C5): a form's intent (consult, appointment, quote); where a
-- contact came from (`came_from`: the click's channel, `utm:<source>`, the referrer's kind -- search, social, other --
-- or direct) with the link's UTM tags and the referring host; a booking completed on the page (lead kind 'booking',
-- event kind 'booked_online': in CREATE TABLE above for a new database, by the migrations lead_kind_booking and
-- page_event_booked_online for one that has the tables).
ALTER TABLE lead ADD COLUMN IF NOT EXISTS intent TEXT CHECK (intent IN ('consult', 'appointment', 'quote'));
ALTER TABLE lead ADD COLUMN IF NOT EXISTS came_from TEXT;
ALTER TABLE lead ADD COLUMN IF NOT EXISTS utm JSONB;
ALTER TABLE lead ADD COLUMN IF NOT EXISTS referrer TEXT;
ALTER TABLE page_event ADD COLUMN IF NOT EXISTS came_from TEXT;

-- A3p-3 batch 3 (2026-10-04; spec 2026-10-03-admiry-pages-a3p-3-design P31-P37 §6.7, plan
-- 2026-10-04-agent-a3p-3-3-site-bundles B1, B2, B7, B8, B11): site bundles (agent/bundles.py, agent/site_upload.py).
-- Every distinct file once, by its SHA-256 (`touched_at`: an upload naming a blob touches it, and the hourly
-- collection deletes only blobs nothing names and nothing touched for a day); a version or a preview is `html` (one
-- page, as before) or `bundle` (its files are rows naming blobs); the one-time upload link (single use, 30 minutes)
-- with its check's result; the one-hour zip link of a bundle's source; the business's preview host
-- (`preview--<hex>.<pages domain>`, minted at its first bundle and kept).
CREATE TABLE IF NOT EXISTS site_blob (
  sha256      TEXT PRIMARY KEY,
  bytes       BYTEA NOT NULL,
  size        INT NOT NULL,
  touched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE page_version ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'html'
  CHECK (kind IN ('html', 'bundle'));
ALTER TABLE page_preview ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'html'
  CHECK (kind IN ('html', 'bundle'));
CREATE TABLE IF NOT EXISTS page_version_file (
  page_id  TEXT NOT NULL,
  version  INT NOT NULL,
  path     TEXT NOT NULL,
  sha256   TEXT NOT NULL REFERENCES site_blob(sha256),
  PRIMARY KEY (page_id, version, path),
  FOREIGN KEY (page_id, version) REFERENCES page_version (page_id, version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS page_version_file_blob ON page_version_file (sha256);
CREATE TABLE IF NOT EXISTS page_preview_file (
  token   TEXT NOT NULL REFERENCES page_preview(token) ON DELETE CASCADE,
  path    TEXT NOT NULL,
  sha256  TEXT NOT NULL REFERENCES site_blob(sha256),
  PRIMARY KEY (token, path)
);
CREATE INDEX IF NOT EXISTS page_preview_file_blob ON page_preview_file (sha256);
CREATE TABLE IF NOT EXISTS site_upload (
  token        TEXT PRIMARY KEY,                   -- su_…, 30 minutes, single use (db.new_id)
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  page_id      TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  expires_at   TIMESTAMPTZ NOT NULL,
  used_at      TIMESTAMPTZ,
  result       JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS site_upload_page ON site_upload (page_id, used_at DESC);
CREATE TABLE IF NOT EXISTS site_zip (
  token       TEXT PRIMARY KEY,                    -- sz_…, an hour (db.new_id)
  page_id     TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  version     INT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE page ADD COLUMN IF NOT EXISTS preview_host TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS page_preview_host ON page (preview_host) WHERE preview_host IS NOT NULL;

-- A3p-3 batch 4 (2026-10-04; spec 2026-10-03-admiry-pages-a3p-3-design P34, P36 §6.4 item 3, plan
-- 2026-10-04-agent-a3p-3-4-render-check R4, R8): the render check. A site bundle's home page as the headless browser
-- saw it at a phone size and a computer size (JPEG), kept with its preview and copied onto the version it becomes; and
-- the live site's daily render (`render_json`: when, its problems; `rendered_at`).
ALTER TABLE page_preview ADD COLUMN IF NOT EXISTS shot_phone BYTEA;
ALTER TABLE page_preview ADD COLUMN IF NOT EXISTS shot_desktop BYTEA;
ALTER TABLE page_version ADD COLUMN IF NOT EXISTS shot_phone BYTEA;
ALTER TABLE page_version ADD COLUMN IF NOT EXISTS shot_desktop BYTEA;
ALTER TABLE page ADD COLUMN IF NOT EXISTS render_json JSONB;
ALTER TABLE page ADD COLUMN IF NOT EXISTS rendered_at TIMESTAMPTZ;

-- A3p-5 batch 2 (2026-10-05; spec 2026-10-05-admiry-pages-a3p-5-own-website-design §5, W11, W12, W15; plan
-- 2026-10-05-agent-a3p-5-2-code-on-the-owners-site Q10, ruling R0-g): whether the hourly check last saw Admiry's code on
-- the owner's own page (`code_seen`: NULL until it has looked; W15's one e-mail per change), and the ways to the code an
-- owner asked for (agent/site_install.py) -- the web person's e-mail (`send`, the address only as its SHA-256 in
-- `to_hash`: W11's three a day count these rows) and Admiry's team (`help`: the contact is on its support ticket).
ALTER TABLE page ADD COLUMN IF NOT EXISTS code_seen BOOLEAN;
CREATE TABLE IF NOT EXISTS install_request (
  id           TEXT PRIMARY KEY,                    -- ir_…
  business_id  TEXT NOT NULL REFERENCES business(id),
  way          TEXT NOT NULL CHECK (way IN ('send', 'help')),
  to_hash      TEXT,                                -- send: SHA-256 of the address, lower case; help: NULL
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS install_request_business ON install_request (business_id, created_at);

-- A3p-5 batch 2 Task 4 (ruling R4-b): Admiry's code counts as gone from the owner's page after two hourly looks in a
-- row without it, as a page counts as down after two failures (H33): the first such look (`code_missed_at`, cleared
-- once the code is seen again, by a saved check and by a new address), and when the owner was last told it went
-- (`code_mailed_at`: at most one such e-mail a page in 7 days)
ALTER TABLE page ADD COLUMN IF NOT EXISTS code_missed_at TIMESTAMPTZ;
ALTER TABLE page ADD COLUMN IF NOT EXISTS code_mailed_at TIMESTAMPTZ;

-- A3p-5 batch 2 final review (ruling F-1): when a saved check first found Admiry's code after the ask -- the hourly
-- watch's own pass, or the owner's Check now (site_install.fulfil). A fulfilled ask is watched no more
-- (jobs._watched), so the watch's "Admiry's code is on {host}" e-mail goes at most once an ask.
ALTER TABLE install_request ADD COLUMN IF NOT EXISTS found_at TIMESTAMPTZ;

-- A3p-6 batch 1 (2026-10-06; spec 2026-10-05-admiry-pages-a3p-6-industry-sites-design T19, T25 §6, plan
-- 2026-10-06-agent-a3p-6-1-industry-site-templates Q1, Q7, Q9): industry sites. The stock library -- each photo once,
-- as downloaded, with its source, licence and author on record, and its WebP sizes -- the site's versioned document,
-- and the render check's third screenshot (a tablet).
CREATE TABLE IF NOT EXISTS site_asset (
  id           TEXT PRIMARY KEY,                     -- sa_…
  family       TEXT NOT NULL, kind TEXT NOT NULL,     -- the family and the manifest's slot (hero, service-roofing, …)
  blob_sha256  TEXT NOT NULL UNIQUE REFERENCES site_blob(sha256),   -- the photo as downloaded
  source       TEXT NOT NULL, page_url TEXT, author TEXT, author_url TEXT,
  licence      TEXT NOT NULL, licence_url TEXT,
  alt          TEXT NOT NULL, subject TEXT, width INT NOT NULL, height INT NOT NULL,
  people       BOOLEAN NOT NULL DEFAULT false, logos BOOLEAN NOT NULL DEFAULT false, ai BOOLEAN NOT NULL DEFAULT false,
  reviewed_at  TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS site_asset_slot ON site_asset (family, kind);
CREATE TABLE IF NOT EXISTS site_asset_file (
  asset_id  TEXT NOT NULL REFERENCES site_asset(id) ON DELETE CASCADE,
  width     INT NOT NULL,
  sha256    TEXT NOT NULL REFERENCES site_blob(sha256),
  PRIMARY KEY (asset_id, width)
);
CREATE INDEX IF NOT EXISTS site_asset_file_blob ON site_asset_file (sha256);
CREATE TABLE IF NOT EXISTS site_spec (
  id           TEXT PRIMARY KEY,                     -- ss_…
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  page_id      TEXT NOT NULL REFERENCES page(id) ON DELETE CASCADE,
  version      INT NOT NULL, family TEXT NOT NULL, style TEXT NOT NULL,
  data         JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (page_id, version)
);
ALTER TABLE page ADD COLUMN IF NOT EXISTS site_spec_id TEXT;
ALTER TABLE page_preview ADD COLUMN IF NOT EXISTS shot_tablet BYTEA;
ALTER TABLE page_version ADD COLUMN IF NOT EXISTS shot_tablet BYTEA;

-- A3p-6 batch 2 (2026-10-06; spec 2026-10-05-admiry-pages-a3p-6-industry-sites-design T12, T15, T19; plan
-- 2026-10-06-agent-a3p-6-2-facts-photos-publish Q2, Q7): the site's working copy between builds (the owner's own
-- answers, the look, the wordmark accepted, the words kept), and what a photo the owner uploaded shows (the kinds of
-- agent/sites/photos.py; NULL for a starter page's photos).
ALTER TABLE page ADD COLUMN IF NOT EXISTS site_draft JSONB;
ALTER TABLE page_photo ADD COLUMN IF NOT EXISTS kind TEXT;

-- S0 (2026-10-07, founder's dev walk): Admiry's second look at a draft plan with the money in place, before the owner
-- confirms it from the setup card (agent/plan_review.py). One row per look the model gave; the latest row for the
-- same plan, balance and launch refusal answers the next look instead of the model. Kept as the record of what Admiry
-- said before each launch (the evaluation work reads it later).
CREATE TABLE IF NOT EXISTS plan_review (
  id            TEXT PRIMARY KEY,           -- rv_…
  plan_id       TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  balance_cents BIGINT,                     -- the balance the look read; NULL for a business Google bills directly
  blocked       TEXT,                       -- the launch check's refusal at the time, or NULL
  review        JSONB NOT NULL,             -- models.PlanReview as the model answered it
  model         TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plan_review_plan ON plan_review (plan_id, created_at DESC);

-- Capability layer K1 (spec 2026-10-07-admiry-capability-layer-design §4): every second look is a Critic run -- the
-- plan rubric's score (0..1) and verdict computed from the grades in `review` (agent/roles), and who asked (web:<email>
-- or job:critic). A row without a score (written before K1) never answers a look.
ALTER TABLE plan_review ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION;
ALTER TABLE plan_review ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE plan_review ADD COLUMN IF NOT EXISTS actor TEXT;

-- Capability layer K1 (spec 2026-10-07-admiry-capability-layer-design §8.1): the roles' background work. One row per
-- piece of work a role does in the background (agent/roles/queue.py); `kind` says which work of the role it is (the
-- Critic's `plan`), and one waiting row per business, role and kind holds the newest brief (agent_job_one_queued).
-- business_id is NULL for work over a vertical (K3/K4). `skipped`: the work no longer applies (a superseded version,
-- the day's cap). `result` is what the work produced or why it stopped.
CREATE TABLE IF NOT EXISTS agent_job (
  id           TEXT PRIMARY KEY,                    -- aj_…
  business_id  TEXT REFERENCES business(id) ON DELETE CASCADE,
  role         TEXT NOT NULL,                       -- critic | analyst | researcher | strategist
  kind         TEXT NOT NULL,
  brief        JSONB NOT NULL DEFAULT '{}'::jsonb,
  status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'skipped', 'failed')),
  rounds       INT NOT NULL DEFAULT 0,
  result       JSONB,
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_job_one_queued ON agent_job (business_id, role, kind) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS agent_job_waiting ON agent_job (created_at) WHERE status = 'queued';

-- L1 (spec 2026-10-10-admiry-launch-review-design §6.1): a first launch's review after the owner's yes and before its
-- ads go live, one row per version the owner agreed to while AGENT_LAUNCH_REVIEW is on (agent/launch_review.py).
-- `steps`: one entry per step the Reviewer took (name, ok, detail, at); `hold_reason`: the admin's words for why it
-- waits for a person; `approval`: how the owner said yes (web, paid, code) and its proposal; `decided_by`: reviewer,
-- admin:<email>, or owner (a new version withdrew it); `note`: an admin's words to the owner on a return or a reject.
CREATE TABLE IF NOT EXISTS launch_review (
  id           TEXT PRIMARY KEY,                    -- lr_…
  plan_id      TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  state        TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'creative', 'configured', 'checked',
                 'passed', 'held', 'returned', 'rejected', 'withdrawn')),
  steps        JSONB NOT NULL DEFAULT '[]'::jsonb,
  hold_reason  TEXT,
  approval     JSONB NOT NULL DEFAULT '{}'::jsonb,
  decided_by   TEXT,
  decided_at   TIMESTAMPTZ,
  note         TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS launch_review_open ON launch_review (created_at)
  WHERE state IN ('queued', 'creative', 'configured', 'checked', 'held');
CREATE INDEX IF NOT EXISTS launch_review_business ON launch_review (business_id, created_at DESC);

-- spec 2026-10-11-admiry-assistant-checklist-design §6: every refusal Google answered a validateOnly or a launch with,
-- one row per refusal detail (agent/ads_refusal.py): the code, the policy topic and its type, the field Google
-- pointed at and what it is (`kind`: headline, description, keyword, url, phone, budget, criterion, ad, other), the
-- offending text (an owner's own ad text: read for that business alone, never shown to another owner), its class
-- (channels/google_search/refusals.py) and what Admiry did (`action`). The Reviewer writes it; the Google build reads
-- the dropped rows to leave their lines out (build.Dropped); the admin page shows a review's rows; Part B's daily
-- summary reads it by day (agent/refusal_summary.py).
CREATE TABLE IF NOT EXISTS ads_refusal (
  id           TEXT PRIMARY KEY,                    -- ar_…
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  plan_id      TEXT NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  review_id    TEXT REFERENCES launch_review(id) ON DELETE SET NULL,
  code         TEXT NOT NULL,
  topic        TEXT,
  type         TEXT,
  field        TEXT NOT NULL,
  kind         TEXT NOT NULL,
  text         TEXT,
  class        TEXT NOT NULL CHECK (class IN ('ad_text', 'keyword', 'destination', 'business', 'other')),
  action       TEXT NOT NULL CHECK (action IN ('dropped', 'returned', 'held')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ads_refusal_plan ON ads_refusal (plan_id, created_at);
CREATE INDEX IF NOT EXISTS ads_refusal_review ON ads_refusal (review_id);
CREATE INDEX IF NOT EXISTS ads_refusal_day ON ads_refusal (created_at);
CREATE INDEX IF NOT EXISTS ads_refusal_business ON ads_refusal (business_id, created_at);

-- spec 2026-10-11 §5 (plan D4): a return the Reviewer makes carries the resolution the owner's checklist fixes it
-- with (agent/launch_check.py: `phone`, `check_page`); an admin's return, and a reject, carry none.
ALTER TABLE launch_review ADD COLUMN IF NOT EXISTS resolution TEXT;

-- The daily digest (spec 2026-09-30-admiry-daily-digest-design §11; plan 2026-10-07-agent-k2 Task 1): the business's
-- time zone (written only by the jobs: digest_zone.ensure), how often the owner gets the report, the kind of an e-mail
-- thread (a digest's own thread is `digest`), and one digest row per business and reported local day.
ALTER TABLE business ADD COLUMN IF NOT EXISTS time_zone TEXT;
ALTER TABLE business ADD COLUMN IF NOT EXISTS digest_frequency TEXT NOT NULL DEFAULT 'daily'
  CHECK (digest_frequency IN ('daily', 'weekly', 'off'));
ALTER TABLE email_thread ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'conversation';
CREATE TABLE IF NOT EXISTS digest (
  id            TEXT PRIMARY KEY,                    -- dg_…; the Resend key and the Message-ID's local part
  business_id   TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  day           DATE NOT NULL,                       -- the reported local day
  edition       TEXT NOT NULL CHECK (edition IN ('daily', 'weekly')),
  time_zone     TEXT NOT NULL,
  body          JSONB,                               -- §4.4; NULL while building
  rendered      JSONB,                               -- {subject, text, html, headers, reply_to}: what every attempt sends
  note          TEXT,                                -- Admiry's take: the Analyst's summary an owner may read (K2 R3)
  note_dropped  TEXT,                                -- why there is no take
  numbers_final BOOLEAN,
  mode          TEXT NOT NULL CHECK (mode IN ('operator', 'on')),
  status        TEXT NOT NULL DEFAULT 'building'
                CHECK (status IN ('building', 'ready', 'sent', 'failed', 'skipped', 'stale')),
  attempts      INTEGER NOT NULL DEFAULT 0,          -- send attempts
  thread_id     TEXT REFERENCES email_thread(id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at       TIMESTAMPTZ,
  UNIQUE (business_id, day)
);
CREATE INDEX IF NOT EXISTS digest_business_day ON digest (business_id, day DESC);

-- Capability layer K2 (spec 2026-10-07 §5, §8.1; plan 2026-10-07-agent-k2 R1, R5-R7): the Analyst's daily look, one
-- per business and local day. Nothing in it is ever applied: a recommendation is data the owner acts on.
CREATE TABLE IF NOT EXISTS insight (
  id           TEXT PRIMARY KEY,                                          -- in_…
  business_id  TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  day          DATE NOT NULL,                                             -- the business's local day looked at
  plan_id      TEXT,                                                      -- the approved plan read
  mode         TEXT NOT NULL CHECK (mode IN ('validate', 'on')),          -- AGENT_ROLES when it was built
  status       TEXT NOT NULL CHECK (status IN ('building', 'done', 'failed', 'skipped')),
  metrics      JSONB NOT NULL DEFAULT '{}'::jsonb,                        -- what code computed: figures, flags, candidates
  insight      JSONB,                                                     -- the words, each with its guard reason
  review       JSONB,                                                     -- the Critic: grades, score, verdict, issues
  score        DOUBLE PRECISION,
  verdict      TEXT,
  dropped      TEXT,                     -- why no owner reads it: budget | llm | review | revise | fetch_more | error
  rounds       INT NOT NULL DEFAULT 0,
  calls        INT NOT NULL DEFAULT 0,   -- model calls made (AGENT_ANALYST_MAX_PER_DAY)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  UNIQUE (business_id, day)
);
CREATE INDEX IF NOT EXISTS insight_created ON insight (created_at);

-- Capability layer K5 (spec 2026-10-07 §7; plan 2026-10-08-agent-k5-evaluation P9): the founder's grades of the golden
-- set's frozen plans, saved as he gives them on the dev box's grading page (agent/eval_web.py) and exported into the
-- case files (`agent.eval export`). One row per plan (a case's id or a flawed copy's) and dimension; `plan_digest`
-- names the frozen plan graded (agent/eval/cases.py digest), so a grade never moves to a plan frozen again. The grade
-- is null until chosen (a note may come first). Empty on prod: the page is dev only.
CREATE TABLE IF NOT EXISTS eval_grade (
  case_id      TEXT NOT NULL,
  dimension    TEXT NOT NULL,
  grade        SMALLINT CHECK (grade BETWEEN 1 AND 5),
  note         TEXT NOT NULL DEFAULT '',
  plan_digest  TEXT NOT NULL,
  grader       TEXT NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, dimension)
);

-- Capability layer K4 (spec 2026-10-07-admiry-capability-layer-design §4 Researcher row, §4.1, §8.1; plan
-- 2026-10-08-agent-k4-researcher): one research run per agent_job ("researcher", "business") -- what the model read
-- (a digest: its sha256, the sources' ids, kinds, labels and links, never a nearby business's name), what it wrote
-- (each sentence with its guard reason), the Critic per round, and why no owner reads it. Owners read a run only with
-- AGENT_ROLES on, a run built while it was on (`mode`) and the Critic's deliver (agent/roles/researcher.py owner_view).
CREATE TABLE IF NOT EXISTS research_run (
  id               TEXT PRIMARY KEY,                                   -- rr_…
  job_id           TEXT UNIQUE REFERENCES agent_job(id) ON DELETE SET NULL,
  business_id      TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  vertical         TEXT NOT NULL,                                      -- consult.tree_for(category)
  trigger          TEXT NOT NULL CHECK (trigger IN ('auto', 'owner', 'operator')),
  brief            JSONB NOT NULL DEFAULT '{}'::jsonb,                 -- what was asked: topics, the owner's note
  mode             TEXT NOT NULL CHECK (mode IN ('validate', 'on')),   -- AGENT_ROLES when it was built
  status           TEXT NOT NULL CHECK (status IN ('building', 'done', 'failed')),
  materials_digest JSONB NOT NULL DEFAULT '{}'::jsonb,
  brief_json       JSONB,                                              -- models.ResearchBrief as stored
  review           JSONB,                                              -- {"rounds": [...], "final": {...}}
  score            DOUBLE PRECISION,
  verdict          TEXT,
  confidence       TEXT CHECK (confidence IN ('low', 'medium', 'high')),
  dropped          TEXT, -- why no owner reads it: llm | material | review | revise | fetch_more | empty | error | stale
  rounds           INT NOT NULL DEFAULT 0,
  calls            INT NOT NULL DEFAULT 0,                             -- model calls made
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),                 -- this attempt's start (a retried job's again)
  finished_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS research_run_business ON research_run (business_id, created_at DESC);
CREATE INDEX IF NOT EXISTS research_run_created ON research_run (created_at);

-- spec 2026-10-12-admiry-google-refusal-knowledge-design §2.2 (Part B phase 1): rules across businesses drawn from
-- what Google refused -- a whole word or phrase (`word`, case-insensitive, lower-cased) or one of a fixed set of
-- pattern shapes (`pattern`: agent/local_rules.py SHAPES, the shape's id; never a regular expression from a model or
-- a form) -- on headlines, descriptions, search phrases (`keyword`) or any. `warn` lists a checklist item; `block`
-- leaves the line out of every Google build (build.Dropped.holds) and blocks the launch. A `warn` rule the daily
-- summary proposes is `active` at once; a `block` one waits `proposed` for a person; an admin's own rule is active
-- as written. `evidence` is {refusal_ids, count, businesses, note}; `why` the owner's words (never Google's);
-- `topic` Google's policy topic, admin surfaces only. Decided on /ops/rules (agent/ops_rules.py). Read only while
-- AGENT_REFUSAL_RULES is on (local_rules.load).
CREATE TABLE IF NOT EXISTS refusal_rule (
  id           TEXT PRIMARY KEY,                    -- rr_…
  kind         TEXT NOT NULL CHECK (kind IN ('word', 'pattern')),
  applies_to   TEXT NOT NULL CHECK (applies_to IN ('headline', 'description', 'keyword', 'any')),
  value        TEXT NOT NULL,
  topic        TEXT,
  why          TEXT NOT NULL,
  level        TEXT NOT NULL CHECK (level IN ('warn', 'block')),
  status       TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'rejected', 'retired')),
  evidence     JSONB NOT NULL DEFAULT '{}'::jsonb,
  proposed_by  TEXT NOT NULL,                       -- summary:<day> | admin:<email>
  decided_by   TEXT,
  decided_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS refusal_rule_live ON refusal_rule (kind, applies_to, value)
  WHERE status IN ('proposed', 'active');
CREATE INDEX IF NOT EXISTS refusal_rule_status ON refusal_rule (status, created_at DESC);

-- §2.4: one row per UTC day the daily summary (agent/refusal_summary.py, job refusal-summary) read: the counts by
-- topic, class and action and the live disapprovals ads-sync keeps (`counts`), the ad-text and search-phrase lines
-- refused (`lines`), the active rules' hits and the misses (`rules`), the refusal_rule ids it proposed
-- (`proposals`), what its one model call did (`model`: proposed | nothing_to_propose | budget | no_model | failed)
-- and the calls it made (`calls`, counted with the Analyst's day: roles/analyst.used_today). A rerun rewrites the
-- row.
CREATE TABLE IF NOT EXISTS refusal_summary (
  id           TEXT PRIMARY KEY,                    -- rs_…
  day          DATE NOT NULL UNIQUE,
  counts       JSONB NOT NULL DEFAULT '{}'::jsonb,
  lines        JSONB NOT NULL DEFAULT '[]'::jsonb,
  rules        JSONB NOT NULL DEFAULT '{}'::jsonb,
  proposals    JSONB NOT NULL DEFAULT '[]'::jsonb,
  model        TEXT NOT NULL,
  calls        INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- §2.3: the active rule a refused line matched when the Reviewer recorded it (local_rules.stamp): the summary's hits
-- -- a rule that was right, and a line that still reached Google under it.
ALTER TABLE ads_refusal ADD COLUMN IF NOT EXISTS rule_id TEXT REFERENCES refusal_rule(id) ON DELETE SET NULL;

-- Verified creatives (spec 2026-11-04-muse-verified-creatives-google-ads §4): one set per studio run -- the steps it
-- took (`{step, model, ok, ms}`), the claim ledger (kept lines with their facts, removed lines and skipped photos with
-- why, the checker), an owner-readable error when it failed -- and its images: the owner's photo extended to Google's
-- sizes or plainly cropped, with what each check found. The images are the raw bytes sent to Google.
CREATE TABLE IF NOT EXISTS ad_creative_set (
  id          TEXT PRIMARY KEY,                                   -- cs_…
  business_id TEXT NOT NULL REFERENCES business(id) ON DELETE CASCADE,
  status      TEXT NOT NULL CHECK (status IN ('making', 'ready', 'failed', 'accepted')),
  steps       JSONB NOT NULL DEFAULT '[]', ledger JSONB, error TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(), accepted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ad_creative_set_biz ON ad_creative_set (business_id, created_at DESC);
CREATE TABLE IF NOT EXISTS ad_creative_image (
  id        TEXT PRIMARY KEY,                                     -- ci_…, unguessable (db.new_id)
  set_id    TEXT NOT NULL REFERENCES ad_creative_set(id) ON DELETE CASCADE,
  photo_id  TEXT NOT NULL, format TEXT NOT NULL CHECK (format IN ('landscape', 'square')),
  method    TEXT NOT NULL CHECK (method IN ('extended', 'plain_crop')),
  bytes     BYTEA NOT NULL, content_type TEXT NOT NULL, width INT NOT NULL, height INT NOT NULL,
  checks    JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ad_creative_image_set ON ad_creative_image (set_id);
