# Account Personas

Persona files define the public voice and reply style for each Threads account operated by Hermes.

## File contract

Use exactly:

```text
personas/<account-key>.md
```

Examples:

```text
personas/syaqir.md
personas/brand_a.md
personas/brand_b.md
```

The account key must match the same key used by `threads-operator --account <key>`.

## Runtime rule

Before Hermes generates a reply or other account-voiced content, it must load the exact persona file for the selected account.

For account `syaqir`:

```text
personas/syaqir.md
```

Do not silently fall back to another account's persona.

If the selected account has no persona file:
- do not borrow another account's persona;
- do not guess the voice;
- stop the generation step and report that the account persona is missing.

This fail-closed rule prevents account A from accidentally sounding like account B.

## Model independence

Persona files contain style/content instructions only.

They must not contain:
- provider API keys;
- model names that must be used;
- provider-specific routing;
- Threads credentials;
- Supabase credentials.

Hermes uses whatever provider/model is currently configured as its primary/default model and applies the selected account persona to the generated text.

## New account

When adding a new Threads account, create a new persona file for that account key before enabling Hermes-generated replies.

Keep persona files focused on public voice, reply behaviour, factual boundaries, topic fit and examples. Do not store secrets or unnecessary private information in them.
