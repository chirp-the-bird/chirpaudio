# ChirpAudio

Twitch audio analysis tools for CLI and web usage.

## What This Repository Contains

- CLI analyzer for Twitch live streams and VODs.
- OAuth token helper and token validation utility.
- WSGI web app that runs the analyzer and streams output to the browser.
- Static assets for the web meter UI.

Primary scripts:
- twitch_audio_test.py: Core analyzer (live and VOD).
- twitch_web_wsgi.py: WSGI app for Apache/mod_wsgi deployment.
- get_oauth_token.py: OAuth auth-code + refresh workflow helper.
- test_oauth_token.py: Quick token validity check against Twitch Helix.

## Requirements

## Runtime

- Python 3.9+
- ffmpeg installed and available on PATH (or Windows fallback path in script)
- Twitch application credentials and OAuth token

## Python dependencies

Install required packages:

```bash
pip install requests streamlink
```

## External dependency: ffmpeg

Windows example from project defaults:

- Suggested location: C:\ffmpeg\bin\ffmpeg.exe
- Or install anywhere and ensure ffmpeg is on PATH

Linux/macOS:

- Install ffmpeg via your package manager
- Ensure ffmpeg is on PATH

## Platform and Deployment Notes

- twitch_audio_test.py is intended to run on both Windows and Linux.
- twitch_web_wsgi.py is intended for Linux deployment under Apache + mod_wsgi.
- Streamlink/Twitch playback behavior can change; browser-assisted integrity token handling may be required.
- On some low-memory/headless Linux hosts, Chromium launch behavior can require no-sandbox style handling.

## Quick Start

1. Clone repository and enter folder.
2. Install Python dependencies:

```bash
pip install requests streamlink
```

3. Create config file from template:

```bash
cp twitch_tokens_template.json twitch_tokens.json
```

On Windows PowerShell:

```powershell
Copy-Item twitch_tokens_template.json twitch_tokens.json
```

4. Add your Twitch app credentials/tokens in twitch_tokens.json.
5. Run token validation:

```bash
python test_oauth_token.py --app-name chirpaudio --token-name audiotest
```

6. Run analyzer:

```bash
python twitch_audio_test.py <channel> --sample-seconds 30
```

## Configuration and Credentials

Credentials can be supplied either by config file or environment variables.

Priority in analyzer:
1. Environment variables:
   - TWITCH_CLIENT_ID
   - TWITCH_OAUTH_TOKEN
2. Config file twitch_tokens.json entries

Template shape:

```json
{
  "apps": {
    "chirpaudio": {
      "client_id": "YOUR_TWITCH_CLIENT_ID",
      "client_secret": "YOUR_TWITCH_CLIENT_SECRET",
      "tokens": {
        "audiotest": {
          "access_token": "YOUR_TWITCH_OAUTH_ACCESS_TOKEN"
        }
      }
    }
  }
}
```

## OAuth Utility Usage

Generate or refresh user tokens with get_oauth_token.py.

Create/update token:

```bash
python get_oauth_token.py \
  --app-name chirpaudio \
  --token-name audiotest
```

Refresh token:

```bash
python get_oauth_token.py \
  --app-name chirpaudio \
  --token-name audiotest \
  --refresh
```

Validate token:

```bash
python test_oauth_token.py --app-name chirpaudio --token-name audiotest
```

## Analyzer Usage

## Live stream mode

```bash
python twitch_audio_test.py <channel> [--sample-seconds N] [--debug]
```

Example:

```bash
python twitch_audio_test.py willowstephens --sample-seconds 30
```

## VOD mode

```bash
python twitch_audio_test.py --vod-url <url> [--start-time HH:MM:SS] [--sample-seconds N] [--debug]
```

Example:

```bash
python twitch_audio_test.py --vod-url https://www.twitch.tv/videos/123456789 --start-time 00:10:00 --sample-seconds 45
```

## Output

Analyzer prints human-readable progress/results and also emits a machine-readable JSON result line prefixed by:

- CHIRPAUDIO_RESULT_JSON:

This is consumed by the WSGI app for UI rendering and audit data.

## WSGI Web App

WSGI entry point:
- application(environ, start_response) in twitch_web_wsgi.py

Behavior:
- Serves UI and static meter assets
- Launches analyzer subprocess with streaming output (SSE)
- Records request/result audit events to activity_audit.jsonl
- Records analyzer failures to audiotest_error.log (error-only)

Expected deployment model:
- Linux + Apache + mod_wsgi

## Logging and Audit Files

- activity_audit.jsonl:
  - Request-level audit records from web calls
  - Includes timestamp, request args, and analyzer result payload when available
- audiotest_error.log:
  - Error-only JSON lines from WSGI analyzer invocations
  - Includes timestamp, mode, target, sample_seconds, duration_ms, rc, and full subprocess output

No entry is written to audiotest_error.log for successful analyzer requests.

## Troubleshooting

- activity_audit.jsonl - Contains detailed log of the each request
- audiotest_error.log - Contains only output of errors when call to twitch_audio_test.py fails
    - (Called by twitch_web_wsgi.py)

## Streamlink or Twitch playback errors

Symptoms can include failed stream fetch, integrity token errors, or PersistedQueryNotFound.

Recommended checks:
1. Confirm token validity:

```bash
python test_oauth_token.py --app-name chirpaudio --token-name audiotest
```

2. Run analyzer with debug:

```bash
python twitch_audio_test.py <channel> --debug
```

3. Confirm ffmpeg availability:

```bash
ffmpeg -version
```

4. On Linux server deployments, confirm browser/streamlink compatibility and review recent streamlink updates.

## Common setup issues

- Missing twitch_tokens.json or wrong app/token keys.
- Invalid/expired OAuth token.
- ffmpeg not installed or not on PATH.
- Python package missing (requests/streamlink).

## Project Files

- VERSION: current app version string
- RELEASE_NOTES.md: release history and unreleased changes
- .github/copilot-instructions.md: project constraints for cross-platform and deployment-safe changes
- www/: static web assets (meter config, SVG, images)

## Development Notes

- Keep Python syntax compatible with Python 3.9+.
- Use explicit UTF-8 file I/O.
- Preserve cross-platform path handling.
- Validate stream-fetch changes on both:
  1. Windows local script runs
  2. Linux WSGI deployment path
