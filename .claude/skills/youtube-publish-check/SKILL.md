---
name: youtube-publish-check
description: Validate and normalize YouTube upload metadata (title, description, tags, category, language) before publishing. Use after drafting or optimizing a title/description/tags, e.g. from vidIQ keyword research or title generation, and before any YouTube upload.
---

# YouTube publish check

Offline pre-flight for YouTube metadata. Enforces the API limits (title 100 chars, description 5000, tags 450 chars total / 30 max, numeric category ID, valid language code), strips control characters, dedupes tags, and warns on an empty description or fewer than 3 tags.

Source: `utils/youtube-metadata-validator.js` from darkzOGx/youtube-automation-agent (MIT, see LICENSE). Needs only Node 18+.

## Workflow with vidIQ

1. Research: `vidiq_keyword_research`, `vidiq_trending_videos`.
2. Draft: `vidiq_generate_titles`, then `vidiq_score_title` to pick one.
3. Write the candidate to JSON: `{"title": "...", "description": "...", "tags": ["..."], "categoryId": "22", "defaultLanguage": "en"}`
4. Check it:
   ```bash
   node .claude/skills/youtube-publish-check/scripts/check.js metadata.json
   ```
   Exit 0 = valid (may still list warnings); exit 1 = errors; exit 2 = bad input.
5. Fix every entry in `errors`, then upload with `value` (the normalized copy), not the original, e.g. via `vidiq_update_video` or `vidiq_video_upload`.

Never publish metadata that failed the check. Warnings are advisory: report them to the user.
