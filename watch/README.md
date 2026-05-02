# Watch CLI

Simple command-line tool to track webpage changes by company using SHA-256 hashes.
By default (`hash_mode=auto`) it hashes normalized visible text for HTML pages, which reduces noisy false positives on dynamic sites.
Company status updates are stored in the same DB and shown in `career list` only (not in `career watch` output).
`career watch` can also show top newly added words on changed pages (after a baseline run has been recorded).
It also supports `career find-inspiration` to discover high-confidence public profiles relevant to your target areas.

## Files

- Root DB (shared with other tools): `data/companies_watch_db.json`
- Root state (last-seen hashes): `data/watch_state.json`
- CLI script: `watch/watch.py`
- Command wrapper: `career`
- Inspiration package: `inspiration/`
- Inspiration people DB: `data/inspiration_people_db.json`
- Inspiration run state: `data/inspiration_state.json`

## Quick start

```bash
./career init
./career add-company "OpenAI" "https://openai.com/careers"
./career watch
```

## Useful commands

```bash
# Show configured companies + URLs
./career list

# Add company and multiple targets in one command
./career add-company "Anthropic" \
  "https://www.anthropic.com/careers" \
  "https://www.anthropic.com/jobs"

# Add a dated status update (date is saved automatically)
./career add-status "Anthropic" "Applied through referral."

# Replace all URLs for one company in one command
./career set-targets "OpenAI" \
  "https://openai.com/careers" \
  "https://openai.com/blog"

# Remove a URL
./career remove-target "OpenAI" "https://openai.com/blog"

# Watch only one company
./career watch --company "OpenAI"

# Watch and show top 5 newly added words for each changed page
./career watch --added-words 5

# Disable added-word snippets
./career watch --added-words 0

# Debug protection-layer sites by disabling one or both fallback strategies
./career watch --no-session-bootstrap
./career watch --no-curl-fallback

# Force raw-byte hashing for a target (if you want strict byte-level checks)
./career add-target "OpenAI" "https://openai.com/careers" --hash-mode raw

# Run one inspiration query (uses company DB + resume keyword inference)
./career find-inspiration

# Tune confidence threshold
./career find-inspiration --min-score 70

# Inspect why candidates were skipped (for threshold tuning)
./career find-inspiration --debug-skips --dry-run

# Review discovered people
./career inspiration-list

# Update review status
./career inspiration-status "p_abc123def456" relevant
```

## Run as `career` (without `./`)

From this repo, one-time setup:

```bash
ln -s "$(pwd)/career" ~/.local/bin/career
```

## Manual editing

You can directly edit `data/companies_watch_db.json`.

Shape:

```json
{
  "companies": [
    {
      "name": "OpenAI",
      "targets": [
        {
          "url": "https://openai.com/careers",
          "label": "careers"
        }
      ]
    }
  ]
}
```
