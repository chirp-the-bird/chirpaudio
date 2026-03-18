# Release Notes

This file tracks released and in-progress changes for ChirpAudio.

Source of truth used to assemble this file:
- Local git history in this repository (no git tags currently present)
- Local working tree state as of 2026-03-17
- VERSION file values in history and working copy

## Update Process (Best Practice)

When preparing a release:
1. Keep entries under standard headings: Added, Changed, Fixed, Removed.
2. Write user-impact language first, then implementation detail.
3. Link each released version to a commit or tag when available.
4. Keep "Unreleased" at the top and move items into a numbered version at release time.
5. Update VERSION and this file in the same commit.

Commit and changelog hygiene:
1. Use one logical change per commit when possible.
2. Use conventional commit prefixes (feat/fix/docs/refactor/chore/test).
3. Include a one-line "why" statement for non-obvious fixes.

---

## [Unreleased]

Added:
- No changes yet.

---

## [1.0.5] - 2026-03-17

Added:
- Copilot project instructions file: `.github/copilot-instructions.md`.
- Added repository README file: `README.md`.

Changed:
- `twitch_audio_test.py` updates for stream acquisition resilience and Linux headless browser handling paths.
- `twitch_web_wsgi.py` UI/behavior and metadata updates.
- `twitch_web_wsgi.py` now captures subprocess run metadata (`rc`, duration) and supports centralized error-only analyzer logging.
- VERSION updated to 1.0.5.

Fixed:
- Stream acquisition/browser-launch reliability improvements for recent Twitch playback/integrity behavior changes.
- Added failure diagnostics for analyzer calls by writing timestamped error records to `audiotest_error.log` with mode, target, sample seconds, duration, return code, and full subprocess output.

---

## [1.0.4] - 2026-03-15

Reference:
- Commit: `0f9454e`
- Commit message: "Minor functional changes, visual updates and organization"

Changed:
- Updated core scripts: `twitch_audio_test.py`, `twitch_web_test.py`, `twitch_web_wsgi.py`.
- Updated web assets/config: `www/loudness_meter.svg`, `www/meter_config.json`, `www/chirpythebot.png`.
- Updated project VERSION to 1.0.4.

Notes:
- Commit also included editor/cache-related files; release-focused list above highlights product-relevant files.

---

## [1.0.3] - 2026-03-13

Reference:
- Commit: `06a7d40`
- Commit message: "Visual updates and check in version 1.0.3"

Changed:
- Visual updates in `twitch_web_wsgi.py`.
- VERSION set to 1.0.3.

---

## [1.0.0] - 2026-03-13

Reference:
- Commit: `8400d49`
- Commit message: "Initial commit"

Added:
- Initial CLI/auth scripts: `twitch_audio_test.py`, `get_oauth_token.py`, `test_oauth_token.py`.
- Initial web/WGSI scripts: `twitch_web_test.py`, `twitch_web_wsgi.py`.
- Initial assets/config: `loudness_meter.svg`, `meter_config.json`, `chirpythebot.png`.
- Initial templates and project metadata: `twitch_tokens_template.json`, `.gitignore`, `.gitattributes`.
- Initial VERSION set to 1.0.0.

---

## Version Timeline (Verified)

- 1.0.0 at commit `8400d49` (2026-03-13)
- 1.0.3 at commit `06a7d40` (2026-03-13)
- 1.0.4 at commit `0f9454e` (2026-03-15)
- 1.0.5 released on 2026-03-17 (includes improved WSGI-side analyzer error logging and README addition)
