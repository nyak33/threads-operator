# Optional Hermes Content Generation

Threads Operator does not contain an LLM client and does not require an LLM API key. Hermes may optionally use its own configured model/provider to generate content, then hand the resulting text to Threads Operator for deterministic, account-scoped storage in Supabase.

## Secret layout

Recommended deployment boundary:

```text
Hermes global/runtime environment
  -> LLM provider/model API key(s)
  -> Hermes model configuration

~/.threads-operator/accounts/<account-key>.env
  -> THREADS_ACCESS_TOKEN
  -> THREADS_USER_ID
  -> SUPABASE_URL
  -> SUPABASE_SERVICE_ROLE_KEY
  -> THREADS_BROWSER_PROFILE
  -> posting/activity/queue settings

threads-operator Git repository
  -> no production secrets
  -> no LLM provider API keys
```

Do not duplicate a shared LLM provider key into every Threads account file. Hermes owns that credential. Threads Operator only receives already-generated text.

## Mandatory Threads content rules

Every content-generation path must follow [`docs/threads-platform-rules.md`](threads-platform-rules.md) before queueing a draft.

- Normal root posts and every reply are each limited to **500 characters** by the current operator capability.
- Threads' separate long-text attachment feature (up to 10,000 characters) is **not** the normal post limit and is not currently emitted by Threads Operator.
- For a root + reply chain, write the root as the strongest hook/open loop; use replies to progressively deliver the explanation, examples, proof points, and final CTA. Do not dump the full payoff into the root when the brief calls for a thread.
- Count and validate each part independently before `enqueue-draft`.
- Continue assigning a meaningful content-specific topic.
- If Meta releases or changes a relevant Threads feature, or the platform-rules verification date is older than 30 days, re-check official Meta sources and update the rules/constants/tests before relying on a new limit or capability.

The supported draft-ingress path validates the whole chain before writing it to Supabase, and the Threads API boundary validates every individual root/reply again before making a Meta request.

## Mode A — external content brain (default)

This remains the normal workflow when ChatGPT or another external system creates the content:

```text
ChatGPT / external system
        -> Supabase queue
        -> Threads Operator
        -> Threads
```

No Hermes LLM generation is required.

## Mode B — Hermes-generated drafts (optional)

Hermes may generate text with whatever model/provider is configured in Hermes, then submit the generated result as a draft:

```bash
.venv/bin/threads-operator enqueue-draft \
  --account brand_a \
  --text "$GENERATED_TEXT" \
  --campaign-code HERMES_GENERATED
```

Optional reply-chain content can be supplied by repeating `--reply`:

```bash
.venv/bin/threads-operator enqueue-draft \
  --account brand_a \
  --text "$GENERATED_TEXT" \
  --reply "$GENERATED_REPLY_1" \
  --reply "$GENERATED_REPLY_2" \
  --campaign-code HERMES_GENERATED
```

Always assign a meaningful, content-specific topic with `--topic` (e.g. `--topic "Local SEO"`, `--topic "Career Upskilling"`). Do not submit drafts with a generic "General" topic when a real one is determinable, and never hardcode one topic across an account's whole output. The topic is stored on the row and published as Meta's `topic_tag` on the root post — see `README.md` § Content Topics.

The command writes only `status=draft`. It never calls a model, never approves the row, and never publishes to Threads.

Hermes should therefore treat generation and queue submission as two separate steps:

1. Generate content using the model/API already configured in Hermes.
2. Pass only the generated text to `threads-operator enqueue-draft --account <key> ...`.
3. Review/approve/schedule the draft through the chosen workflow.
4. Let the existing deterministic publisher handle approved, due rows.

## Multi-account behavior

Every generated draft must name the target account explicitly. Account selection controls which account ENV and Supabase connection are used.

A personal deployment may intentionally point several account ENV files at one shared Supabase project. Account-scoped mutable rows still carry `account_key`.

For client/high-isolation deployments, give each account its own Supabase project and service-role key. The same Threads Operator installation can operate both shared and separate Supabase layouts.

## What must stay outside Threads Operator

Do not add provider-specific variables such as OpenAI, Qwen, Kimi, Anthropic, Gemini, or other LLM API keys to `accounts/*.env`, `.env.example`, or Threads Operator source code.

If Hermes changes model/provider later, Threads Operator should require no code or credential change. Only Hermes generation configuration changes.

## Safety

- Generated content enters as `draft`, never `approved`.
- Live posting remains separately gated by `THREADS_POSTING_ENABLED=true` and `THREADS_EXECUTION_MODE=auto_post`.
- A model/API failure cannot silently fall back to another Threads account.
- Threads Operator never sends LLM credentials anywhere because it never receives them.
- The normal claim-before-publish and partial-failure reconciliation rules remain unchanged.
