# Hermes Engagement Approval

This workflow keeps reply generation automated while preserving explicit human approval.

## Boundary

Hermes may:
- discover a relevant external Threads post;
- generate a short reply draft;
- submit it with `threads-operator engagement propose-reply`;
- send the pending item to Telebot;
- execute an item only after the user approves it.

Hermes must not:
- mark its own reply approved;
- write directly to `threads_engagement_queue`;
- execute a reply that is still `pending_approval`;
- infer approval from silence or unrelated chat text;
- bypass login, CAPTCHA, 2FA or platform security challenges.

## Persona loading

Reply generation is account-scoped. Before generating any proposed reply, Hermes must load:

```text
personas/<account-key>.md
```

For `--account syaqir`, the required file is:

```text
personas/syaqir.md
```

Rules:

- use the exact persona matching the selected account key;
- never silently use another account's persona as fallback;
- if the persona file is missing, stop reply generation and report the missing file;
- use whatever provider/model is currently configured as Hermes' primary/default model;
- do not hardcode a provider or model name into this workflow;
- apply the persona to wording, tone, language, length and reply behaviour;
- never fabricate first-person experience or account-owner facts that are not explicitly verified.

The persona is generation context only. Threads Operator remains deterministic and model-agnostic.

## Proposed reply

A discovered post must have a verified Threads media id before a reply can be queued.

```bash
.venv/bin/threads-operator engagement propose-reply \
  --account syaqir \
  --source-post-id "$THREAD_ID" \
  --url "$THREADS_PERMALINK" \
  --username "$USERNAME" \
  --source-text "$SOURCE_PREVIEW" \
  --text "$PROPOSED_REPLY" \
  --candidate-id "$CANDIDATE_ID" \
  --score "$SCORE" \
  --reason "$REASON"
```

The command inserts `status=pending_approval`. Duplicate reply actions for the same account+permalink are not inserted again.

## Telebot card

Recommended message:

```text
Reply approval

@username
Original:
<short source preview>

Suggested reply:
<proposed reply>

Engagement:
<observed metrics>

Reason:
<why this is worth engaging>

[Approve] [Edit] [Skip]
```

The callback payload should contain only the engagement queue id and action. Never put credentials or access tokens in callback data.

## Approve

On an explicit Approve callback:

```bash
.venv/bin/threads-operator engagement approve \
  --account syaqir \
  --id "$ENGAGEMENT_ID" \
  --approval-ref "$TELEBOT_MESSAGE_REF"
```

Only a row currently in `pending_approval` can transition to `approved`.

After successful approval, execute:

```bash
.venv/bin/threads-operator engagement execute \
  --account syaqir \
  --id "$ENGAGEMENT_ID"
```

Live execution additionally requires:

```text
THREADS_ENGAGEMENT_ENABLED=true
```

The executor conditionally claims `approved -> executing` before calling Threads. A row that was not approved cannot be executed.

## Edit

When the user selects Edit, collect the replacement reply text and run:

```bash
.venv/bin/threads-operator engagement edit \
  --account syaqir \
  --id "$ENGAGEMENT_ID" \
  --text "$REPLACEMENT_REPLY"
```

Editing keeps the row in `pending_approval`. Send a refreshed approval card. The edit itself is not approval.

## Skip

On Skip:

```bash
.venv/bin/threads-operator engagement reject \
  --account syaqir \
  --id "$ENGAGEMENT_ID" \
  --approval-ref "$TELEBOT_MESSAGE_REF"
```

The row becomes `rejected` and is not shown again as pending.

## Pending list

Hermes can recover pending approval cards after restart:

```bash
.venv/bin/threads-operator engagement list \
  --account syaqir \
  --status pending_approval
```

## Reply style

The selected account persona is authoritative for voice. General engagement rules still apply:

- relevant to the exact source post;
- short unless the persona explicitly needs more context;
- additive rather than paraphrasing the source;
- no generic filler;
- no sales CTA, link or unsolicited promotion;
- no fabricated personal experience;
- no sensitive/controversial engagement automation unless explicitly allowed for human review.

For `syaqir`, see [`../personas/syaqir.md`](../personas/syaqir.md).

Main content strategy remains outside this executor. Hermes only generates a micro-reply candidate for approval.
