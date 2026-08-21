# Obsidian Draft Publishing

## Trigger and boundary

A vault write requires explicit persistence language in an existing one-video Discord research thread, such as `Obsidian에 저장해줘` or `vault에 저장해줘`.

`글로 정리해줘`, `블로그 초안으로 정리해줘`, and `Markdown으로 정리해줘` request a chat-only draft unless the same message explicitly asks to save it to Obsidian or the vault. Never infer filesystem authorization from a request to write or format prose.

Do not publish to an external website, CMS, Git remote, or social platform. Vault output is always `status: draft` and `draft: true`.

## Vault configuration

Read the profile-local publishing configuration from:

```text
$HERMES_HOME/plugin-data/hermes-wiki-router/publishing.json
```

Resolve an unset `HERMES_HOME` as `$HOME/.hermes`. The JSON object must contain one absolute `vault_path` string. If the file or key is missing, invalid, or relative, stop and ask the user to configure it. Do not guess a fallback path and do not commit a machine-local vault path into the skill.

The publisher writes one canonical note per video at `<vault>/articles/<video-id>.md`. The human title lives in frontmatter and the H1; the title is never part of the identity path.

Secure publishing requires verified POSIX directory-descriptor and no-follow primitives. Research and chat-only drafting remain cross-platform, but vault publishing fails closed as unsupported on hosts without those primitives.

## Source preparation

1. Resolve the current Discord thread ID and its one canonical YouTube video ID.
2. Reuse the existing citation ledger without resetting it.
3. Include relevant findings from the initial research and subsequent follow-up turns. Do not copy a raw chat transcript into the article.
4. Write the body-only Markdown draft to the cache beside the ledger, for example `<video-id>.blog-body.md`, using `write_file`.
5. Follow `references/blog-output.md` exactly.
6. Render the ledger source block into the body and run strict citation verification before publishing. If verification fails, do not touch the vault.
7. Write a JSON request file beside the body with these keys: `vault`, `ledger`, `video_id`, `title`, `description`, `canonical_url`, `thread_id`, `source_count`, `transcript_status`, and `tags`.

The request file avoids shell option and command injection from titles, descriptions, and tags. Create it with `write_file`; never build it by shell interpolation.

## Publisher

Use the bundled `scripts/publish_note.py`. Resolve its absolute path from the loaded skill directory. The command contains only validated cache paths:

```text
uv run python "$PUBLISHER" \
  --request-file "$REQUEST" \
  --body-file "$BODY"
```

Build even these path arguments through `execute_code` and `shell_quote`. The publisher securely opens the request, body, ledger, vault, and articles directory without following symlinks.

The helper validates identifiers, HTTPS credential-free source URLs, YouTube identity, the citation ledger, source count, inline citation resolution, the exact mechanically rendered raw Sources block including its blank-line policy, required heading uniqueness/order, Unicode control and line separators, body size, profile-configured vault identity, and output containment. After proving the raw Sources block matches the ledger, it Markdown/HTML-escapes source titles for the vault copy. It walks every absolute path component from the root directory descriptor with no-follow semantics, retains and rechecks the complete descriptor chain, performs bounded single-descriptor reads, and uses an atomic no-overwrite create.

## Existing note policy

Publishing is create-only. The helper must refuse to overwrite an existing note for the same video ID, including concurrent creation attempts. There is no `--update` mode.

When a note already exists:

1. report the existing canonical path in Discord
2. do not alter the file
3. tell the user that revisions require manually moving or deleting the current draft after preserving any edits

This intentionally avoids silent data loss from concurrent edits or compare-and-replace races.

## Verification and delivery

After the publisher succeeds:

1. read the exact returned file path with `read_file`
2. confirm frontmatter contains `status: draft`, the expected video ID, thread ID, source count, and transcript status
3. confirm every required heading and the exact ledger-derived `## Sources` block is present
4. reply in the same Discord thread with the absolute vault path and a short uncertainty summary

A successful process exit alone is not proof of publication; the exact note must be read back before reporting success.
