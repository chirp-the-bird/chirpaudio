# GitHub Copilot Instructions — ChirpAudio

## Cross-Platform Requirements

All Python scripts in this project **must run correctly on both Windows and Unix (Linux/macOS)** without modification.

- **Python version**: target Python 3.9+. Do not use syntax or stdlib features introduced in Python 3.10 or later unless an explicit compatibility shim is in place.
  - Use `Optional[str]` from `typing`, not `str | None` (the `X | Y` union syntax requires Python 3.10+).
  - Use `Union[X, Y]` or `Optional[X]` from `typing` for all type annotations.

- **File paths**: always use `os.path.join()`, `os.path.dirname()`, `pathlib.Path`, etc. Never hardcode path separators (`/` or `\`).
  - Exception: `DEFAULT_FFMPEG_PATH` and similar *fallback* paths may be Windows-specific as long as a cross-platform `shutil.which()` lookup is attempted first.

- **Platform detection**: use `os.name == "nt"` for Windows-only code branches. Do not assume `os.name == "posix"` covers all non-Windows platforms; use `else` or check explicitly.

- **Executable discovery**: when searching for external programs (browsers, ffmpeg, etc.) always:
  1. Check known fixed paths per platform.
  2. Fall back to `shutil.which()` for PATH-based discovery.

- **File locking**: use `msvcrt.locking` on Windows (`os.name == "nt"`) and `fcntl.flock` on Unix. Never import one unconditionally at module level.

- **Process management**: `subprocess.Popen`, `proc.terminate()`, and `proc.kill()` are cross-platform safe. Prefer them over platform-specific shell invocations.

- **Encoding**: always open files with `encoding="utf-8"` explicitly. Never rely on the system default encoding.

## General Code Guidelines

- Keep all user-visible output to `stdout`; send errors and diagnostics to `stderr`.
- Gate verbose/debug output behind the `DEBUG` global flag.
- Prefer `Optional[str]` / `Optional[dict]` return types over bare `None` returns without annotation.
- Do not add new third-party dependencies without noting them in a requirements file or comment.

## Deployment Context

- `twitch_audio_test.py` is a dual-platform script and must continue to run on both Windows and Linux.
- `twitch_web_wsgi.py` is deployed on Linux under Apache + mod_wsgi on a DigitalOcean Droplet.
- Treat low-memory/headless Linux behavior as a first-class production constraint when changing stream acquisition logic.

## Stream Acquisition Notes

- Twitch playback/integrity behavior may change without notice; keep streamlink-related logic resilient and debuggable.
- Browser discovery for streamlink should remain cross-platform and include fixed paths plus `shutil.which()` fallback.
- On Linux servers, Chromium may require no-sandbox/headless-compatible launch handling. Keep this guarded to Linux-only code paths.

## Validation Expectations

- For changes that touch stream fetch/browser launch/auth flow, validate behavior in both environments:
  1. Windows local script execution.
  2. Linux server path used by WSGI deployment.
- Preserve existing CLI behavior and user-facing output format unless the task explicitly requests a change.

## Copilot Workflow

- When a fix reveals a recurring platform/deployment constraint, propose a short update to this file in the same change.
- For non-trivial fixes, include a brief "Instructions update suggestion" section in the response with exact text if relevant.
