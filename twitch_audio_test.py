#!/usr/bin/env python3
"""Twitch audio analyzer using Helix API and streamlink.

Fetch stream info via Helix API, get HLS URL via streamlink, process audio,
and calculate average dB and peak dB.

Usage:
    python twitch_audio_test.py <channel> [--sample-seconds N]
    python twitch_audio_test.py --vod-url <url> [--start-time HH:MM:SS] [--sample-seconds N]

Credentials are supplied via the config file (`twitch_tokens.json`) or
via `TWITCH_CLIENT_ID` / `TWITCH_OAUTH_TOKEN` environment variables.

Requirements:
    pip install requests streamlink

    Download ffmpeg-release-full.7z from: https://www.gyan.dev/ffmpeg/builds/
    Extract to: C:\\ffmpeg
    Ensure ffmpeg.exe is at: C:\\ffmpeg\\bin\\ffmpeg.exe
    or add the installation directory to your PATH environment variable.
    
"""

import argparse
from array import array
import math
import os
import re
import shutil
import subprocess
import sys
import datetime
import json
import time

import requests

try:
    from streamlink import Streamlink
except ImportError:
    print("streamlink not installed. Install it with: pip install streamlink")
    sys.exit(1)

# -----------------------------
# defaults and configuration
# -----------------------------
DEFAULT_CHANNEL = "willowstephens"
DEFAULT_SAMPLE_SECONDS = 30
DEFAULT_FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"
MAX_SAMPLE_SECONDS = 360  # 6 minutes
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitch_tokens.json")
APP_NAME = "chirpaudio"
TOKEN_NAME = "audiotest"

# Ad detection settings (Twitch HLS playlist markers)
AD_DETECTION_ENABLED = True
AD_POLL_INTERVAL_SECONDS = 5
AD_MAX_WAIT_SECONDS = 300
AD_TAG_PATTERNS = [
    r"twitch-stitched-ad",
    r"stitched-ad",
    r"ad-break",
    r"EXT-X-DATERANGE:.*CLASS=\"twitch-stitched-ad\"",
]

HELIX_STREAMS_URL = "https://api.twitch.tv/helix/streams"
HELIX_VIDEOS_URL = "https://api.twitch.tv/helix/videos"
RESULT_JSON_PREFIX = "CHIRPAUDIO_RESULT_JSON:"
DEBUG = False


def debug_print(message: str) -> None:
    if DEBUG:
        print(message)


def parse_hhmmss(value: str) -> int:
    """Parse HH:MM:SS into total seconds."""
    m = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError("start-time must be in HH:MM:SS format")
    hours = int(m.group(1))
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def format_hhmmss(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def extract_vod_id(vod_url: str) -> str:
    """Extract Twitch VOD id from common URL formats."""
    if not vod_url:
        return ""

    # Formats:
    # https://www.twitch.tv/videos/123456789
    # https://www.twitch.tv/<channel>/v/123456789 (legacy)
    # https://www.twitch.tv/videos/123456789?t=1h2m3s
    m = re.search(r"twitch\.tv/videos/(\d+)", vod_url, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r"twitch\.tv/.+/v/(\d+)", vod_url, re.IGNORECASE)
    if m:
        return m.group(1)

    return ""


def get_vod_info(vod_url: str, client_id: str, token: str) -> tuple:
    """Fetch VOD metadata (title, channel/user_name, created/published date).

    Returns (info_dict_or_none, error_text_or_none)
    """
    vod_id = extract_vod_id(vod_url)
    if not vod_id:
        return None, "Could not parse VOD id from URL"

    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    params = {"id": vod_id}

    print(f"Fetching VOD info for id '{vod_id}' from Helix API...")
    r = requests.get(HELIX_VIDEOS_URL, headers=headers, params=params, timeout=10)
    if r.status_code != 200:
        return None, (r.text or f"HTTP {r.status_code}")

    data = r.json() if r.content else {}
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        return None, "VOD not found"

    v = items[0]
    info = {
        "id": v.get("id", vod_id),
        "title": v.get("title") or "(VOD)",
        "channel": v.get("user_name") or "(unknown)",
        "created_at": v.get("created_at"),
        "published_at": v.get("published_at"),
        "url": v.get("url") or vod_url,
    }
    return info, None


def parse_args():
    # argparse parses positional channel and optional --sample-seconds
    # Usage: python twitch_audio_test.py <channel> [--sample-seconds N]
    # Defaults: channel=willowstephens, sample_seconds=30
    # Example: python twitch_audio_test.py pokimane --sample-seconds 60
    parser = argparse.ArgumentParser(
        description="Twitch audio analyzer: fetch stream info and analyze audio"
    )
    parser.add_argument("channel", nargs="?", default=DEFAULT_CHANNEL,
                        help="Twitch channel name")
    parser.add_argument("--vod-url", default=None,
                        help="Twitch VOD URL to analyze instead of live channel")
    parser.add_argument("--start-time", type=parse_hhmmss, default=0,
                        help="Start offset for VOD analysis in HH:MM:SS (default: 00:00:00)")
    parser.add_argument("--sample-seconds", type=int,
                        default=DEFAULT_SAMPLE_SECONDS,
                        help="number of seconds of audio to analyze")
    parser.add_argument("--debug", action="store_true",
                        help="enable debug logging, including potentially sensitive stream URL diagnostics")
    return parser.parse_args()


def load_config(config_path: str) -> dict:
    # Loads JSON config from twitch_tokens.json if present.
    # Returns {} if file missing or error.
    # Expected structure:
    # {
    #   "apps": {
    #     "chirpaudio": {
    #       "client_id": "...",
    #       "tokens": {
    #         "audiotest": {
    #           "access_token": "..."
    #         }
    #       }
    #     }
    #   }
    # }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"Error reading config file: {config_path} ({exc})", file=sys.stderr)
        return {}


def resolve_credentials() -> tuple:
    # Priority: ENV > config file
    # Looks for TWITCH_CLIENT_ID and TWITCH_OAUTH_TOKEN in environment.
    # If not found, loads config and looks up:
    #   client_id = config["apps"][APP_NAME]["client_id"]
    #   token = config["apps"][APP_NAME]["tokens"][TOKEN_NAME]["access_token"]
    # Returns (client_id, token)
    client_id = os.getenv("TWITCH_CLIENT_ID")
    token = os.getenv("TWITCH_OAUTH_TOKEN")

    if client_id and token:
        return client_id, token

    config = load_config(CONFIG_PATH)
    apps = config.get("apps", {}) if isinstance(config, dict) else {}
    app = apps.get(APP_NAME) if isinstance(apps, dict) else None
    if app is None:
        return client_id, token

    if not client_id:
        client_id = app.get("client_id")

    tokens = app.get("tokens", {}) if isinstance(app, dict) else {}
    token_entry = tokens.get(TOKEN_NAME) if isinstance(tokens, dict) else None
    if not token and isinstance(token_entry, dict):
        token = token_entry.get("access_token")

    return client_id, token


def check_ffmpeg():
    """Check if ffmpeg is available on PATH or at DEFAULT_FFMPEG_PATH."""
    ffmpeg_cmd = shutil.which("ffmpeg")
    if ffmpeg_cmd:
        return ffmpeg_cmd
    
    if os.path.exists(DEFAULT_FFMPEG_PATH):
        return DEFAULT_FFMPEG_PATH
    
    error_msg = f"""
ffmpeg not found. Please install it:

1. Download ffmpeg-release-full.7z from:
   https://www.gyan.dev/ffmpeg/builds/

2. Extract to: C:\\ffmpeg
   (or elsewhere and update DEFAULT_FFMPEG_PATH in this script)

3. Ensure ffmpeg.exe is at: {DEFAULT_FFMPEG_PATH}
   or add the installation directory to your PATH environment variable.

After installation, rerun this script.
"""
    sys.exit(error_msg)


def get_stream_info(channel, client_id, token):
    # Calls Twitch Helix API:
    #   GET https://api.twitch.tv/helix/streams?user_login=<channel>
    # Headers:
    #   Client-ID: <client_id>
    #   Authorization: Bearer <token>
    # Returns stream info dict if live, else None.
    url = HELIX_STREAMS_URL
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    params = {"user_login": channel}
    
    r = requests.get(url, headers=headers, params=params, timeout=10)

    # debugging: rate limit headers (commented out by default)
    # Typical Helix limits are hundreds of requests per minute per client ID;
    # These rate headers indicate the actual quota remaining.
    # rate_headers = {
    #     "limit": r.headers.get("Ratelimit-Limit") or r.headers.get("RateLimit-Limit"),
    #     "remaining": r.headers.get("Ratelimit-Remaining") or r.headers.get("RateLimit-Remaining"),
    #     "reset": r.headers.get("Ratelimit-Reset") or r.headers.get("RateLimit-Reset"),
    # }
    # print("Rate headers:", rate_headers)
    
    if r.status_code != 200:
        print(f"Error: HTTP {r.status_code}")
        print(f"Response: {r.text}")
        return None, (r.text or "")
    
    data = r.json()
    if not data.get("data"):
        print(f"Stream '{channel}' is currently offline.")
        return None, None
    
    stream = data["data"][0]
    print(f"✓ Stream is LIVE")
    print(f"  Title: {stream['title']}")
    print(f"  Game: {stream['game_name']}")
    print(f"  Viewers: {stream['viewer_count']}")
    # Twitch Helix returns `started_at` as an ISO 8601 UTC timestamp
    # (e.g. 2026-03-04T01:30:51Z) indicating when the stream began.
    # Compute and display both the start time (UTC) and the uptime duration.
    started_at = stream.get("started_at")
    if started_at:
        try:
            # Parse ISO 8601 UTC timestamp ending with 'Z'
            started_dt = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = now - started_dt
            total_seconds = int(delta.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}:{minutes:02d}:{seconds:02d}"
            print(f"  Started at (UTC): {started_dt.isoformat()}")
            print(f"  Uptime: {uptime_str}\n")
        except Exception:
            # Fallback: print the raw value if parsing fails
            print(f"  Uptime/Started at: {started_at}\n")
    else:
        print("  Uptime: (not available)\n")
    
    return stream, None


def refresh_token_via_script() -> bool:
    """Attempt to refresh the OAuth token using get_oauth_token.py --refresh."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "get_oauth_token.py")
    if not os.path.exists(script_path):
        print(f"Refresh script not found: {script_path}", file=sys.stderr)
        return False

    cmd = [
        sys.executable,
        script_path,
        "--app-name",
        APP_NAME,
        "--token-name",
        TOKEN_NAME,
        "--refresh",
        "--config-path",
        CONFIG_PATH,
    ]

    print("Invalid OAuth token detected. Attempting refresh via get_oauth_token.py...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as exc:
        print(f"Failed to run refresh script: {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        if DEBUG and result.stdout:
            print(result.stdout)
        if DEBUG and result.stderr:
            print(result.stderr, file=sys.stderr)
        return False

    if DEBUG and result.stdout:
        print(result.stdout)
    return True


def get_hls_url(source, client_id=None, token=None, is_vod=False):
    """Get HLS URL for a channel or VOD using streamlink with optional authentication."""
    session = Streamlink()
    
    # Authenticate streamlink with Twitch OAuth token to reduce pre-roll ads
    if client_id and token:
        session.set_option("http-headers", {
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}"
        })
        print(f"✓ Using authenticated session (may reduce ads)")

    source_url = source if is_vod else f"https://twitch.tv/{source}"
    
    try:
        streams = session.streams(source_url)
    except Exception as e:
        print(f"Error: {e}")
        return None
    
    if not streams:
        label = "VOD" if is_vod else source
        print(f"No streams available for '{label}'")
        return None
    
    # prefer audio_only, then best quality
    if "audio_only" in streams:
        stream = streams["audio_only"]
        print(f"✓ Using audio_only stream")
    elif "best" in streams:
        stream = streams["best"]
        print(f"✓ Using best quality stream")
    else:
        stream = list(streams.values())[0]
        print(f"✓ Using available stream")
    
    # get the URL from the stream object
    try:
        url = stream.url
        debug_print(f"  URL: {url[:80]}...")
        if DEBUG:
            print()
        return url
    except Exception as e:
        print(f"Error getting stream URL: {e}")
        return None


def analyze_audio(hls_url, ffmpeg_cmd, sample_seconds, log_start=True, start_seconds=None):
    """Pipe audio through ffmpeg and calculate average/peak dB."""
    
    cmd = [
        ffmpeg_cmd,
        "-hide_banner",
        "-loglevel", "error",
    ]

    if start_seconds is not None and start_seconds > 0:
        cmd.extend(["-ss", str(start_seconds)])

    cmd.extend([
        "-i", hls_url,
        "-vn",  # no video
        "-ac", "2",  # stereo
        "-ar", "48000",  # 48kHz
        "-f", "s16le",  # 16-bit PCM
        "pipe:1",
    ])
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if proc.stdout is None:
        print("ffmpeg stdout pipe not available", file=sys.stderr)
        return None
    
    bytes_to_read = sample_seconds * 48000 * 2 * 2  # 48kHz, stereo, 16-bit
    raw = proc.stdout.read(bytes_to_read)
    
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    
    if not raw:
        print("No audio received.")
        return None
    
    # Keep samples in a compact int16 container to avoid large Python-int memory overhead.
    samples = array("h")
    samples.frombytes(raw)
    return samples


def hls_playlist_has_ad(hls_url: str) -> bool:
    """Detect ad markers in Twitch HLS playlist."""
    try:
        r = requests.get(hls_url, timeout=10)
        if r.status_code != 200:
            return False
        text = r.text or ""
        for pattern in AD_TAG_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False
    except Exception:
        return False


def wait_for_no_ads(hls_url: str, context: str) -> None:
    if not AD_DETECTION_ENABLED:
        return
    if not hls_playlist_has_ad(hls_url):
        return

    print(f"Ad detected ({context}). Waiting for ad to finish...")
    waited = 0
    while waited < AD_MAX_WAIT_SECONDS:
        if not hls_playlist_has_ad(hls_url):
            print("Ad finished. Resuming analysis.")
            return
        time.sleep(AD_POLL_INTERVAL_SECONDS)
        waited += AD_POLL_INTERVAL_SECONDS

    print("Ad still present after max wait; aborting analysis.")
    raise RuntimeError("Ad timeout exceeded; aborting analysis to avoid ad audio.")


def capture_audio_with_ad_handling(hls_url, ffmpeg_cmd, sample_seconds, start_seconds=0, is_vod=False):
    """Capture audio while pausing for detected ads (pre-roll or mid-roll)."""
    samples = array("h")
    sample_start_utc = None
    remaining = sample_seconds
    chunk_seconds = 5
    attempts_without_audio = 0

    # Handle pre-roll ads
    wait_for_no_ads(hls_url, "pre-roll")

    while remaining > 0:
        # If an ad appears mid-stream, wait before capturing next chunk
        wait_for_no_ads(hls_url, "mid-roll")

        current_chunk = min(chunk_seconds, remaining)
        if sample_start_utc is None:
            # First real capture starts after any pre-roll wait.
            sample_start_utc = datetime.datetime.now(datetime.timezone.utc)
        chunk_start = (start_seconds + (sample_seconds - remaining)) if is_vod else None
        chunk = analyze_audio(
            hls_url,
            ffmpeg_cmd,
            current_chunk,
            log_start=False,
            start_seconds=chunk_start,
        )
        if not chunk:
            attempts_without_audio += 1
            if attempts_without_audio >= 3:
                print("No audio received after multiple attempts.")
                return None
            # If audio is missing, check for ads again and retry
            wait_for_no_ads(hls_url, "mid-roll")
            continue

        attempts_without_audio = 0
        samples.extend(chunk)
        remaining -= current_chunk

    return samples, sample_start_utc


def calculate_db(samples):
    """Calculate average and peak dB from audio samples."""
    if not samples:
        return None, None
    
    # RMS (Root Mean Square) for average level
    rms = math.sqrt(sum(s**2 for s in samples) / len(samples))
    
    # Peak is the maximum absolute value
    peak = max(abs(s) for s in samples)
    
    # Convert to dB (normalize to 16-bit max = 32768)
    ref = 32768  # 16-bit full scale
    avg_db = 20 * math.log10(rms / ref) if rms > 0 else -float('inf')
    peak_db = 20 * math.log10(peak / ref) if peak > 0 else -float('inf')
    
    return avg_db, peak_db


def compute_lufs(hls_url, ffmpeg_cmd, sample_seconds, start_seconds=None):
    """Run ffmpeg's ebur128 filter to compute LUFS, LRA and true peak.

    Returns a dict with keys: 'integrated_loudness' (LUFS), 'loudness_range' (LU),
    'true_peak' (dBTP or dBFS), 'true_peak_clip_count' or None if not available.
    """
    cmd = [
        ffmpeg_cmd,
        "-hide_banner",
        "-nostats",
        "-t", str(sample_seconds),
    ]

    if start_seconds is not None and start_seconds > 0:
        cmd.extend(["-ss", str(start_seconds)])

    cmd.extend([
        "-i", hls_url,
        "-filter_complex", "ebur128=peak=true",
        "-f", "null",
        "-",
    ])

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("ffmpeg not found when computing LUFS", file=sys.stderr)
        return {}

    stderr = proc.stderr or ""

    # Try to extract Integrated Loudness (summary block) using robust patterns
    integrated = None
    # Pattern: Summary\n\n  Integrated loudness:\n    I:         -17.2 LUFS
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*([\-\d\.]+)\s*LUFS", stderr, re.IGNORECASE)
    if m:
        try:
            integrated = float(m.group(1))
        except ValueError:
            integrated = None
    else:
        # fallback to inline I: ... LUFS occurrences
        m2 = re.search(r"\bI:\s*([\-\d\.]+)\s*LUFS\b", stderr)
        if m2:
            try:
                integrated = float(m2.group(1))
            except ValueError:
                integrated = None

    # Loudness Range (LRA) from summary block
    lra = None
    m = re.search(r"Loudness range:\s*\n[\s\S]*?LRA:\s*([\-\d\.]+)\s*LU", stderr, re.IGNORECASE)
    if m:
        try:
            lra = float(m.group(1))
        except ValueError:
            lra = None
    else:
        m2 = re.search(r"\bLRA:\s*([\-\d\.]+)\s*LU\b", stderr)
        if m2:
            try:
                lra = float(m2.group(1))
            except ValueError:
                lra = None

    # True peak: look in the summary block under 'True peak' -> 'Peak:  -4.6 dBFS'
    true_peak = None
    m = re.search(r"True peak:\s*\n[\s\S]*?Peak:\s*([\-\d\.]+)\s*dBFS", stderr, re.IGNORECASE)
    if m:
        try:
            true_peak = float(m.group(1))
        except ValueError:
            true_peak = None
    else:
        # fallback: sometimes 'TPK:' or 'TPK' lines exist with values like 'TPK:  -4.6  -4.6 dBFS'
        m2 = re.search(r"TPK:\s*([\-\d\.]+)\s*[\-\d\.]*\s*dBFS", stderr)
        if m2:
            try:
                true_peak = float(m2.group(1))
            except ValueError:
                true_peak = None

    # Count true-peak clips: parse all TPK lines and count values >= 0.0 dB
    true_peak_clip_count = 0
    for line in stderr.splitlines():
        # Look for lines like: TPK:  -4.6  -4.6 dBFS
        m = re.search(r"TPK:\s*([\-\d\.]+)\s*([\-\d\.]+)?\s*dBFS", line)
        if m:
            try:
                left_peak = float(m.group(1))
                if left_peak >= 0.0:
                    true_peak_clip_count += 1
                if m.group(2):
                    right_peak = float(m.group(2))
                    if right_peak >= 0.0:
                        true_peak_clip_count += 1
            except (ValueError, TypeError):
                pass

    return {
        "integrated_loudness": integrated,
        "loudness_range": lra,
        "true_peak": true_peak,
        "true_peak_clip_count": true_peak_clip_count,
        "raw_stderr": stderr,
    }


def _round_metric(value, digits=2):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, digits)


def build_result_payload(args, is_vod, stream_info, start_seconds, sample_start_time, sample_start_time_utc, samples, avg_db, peak_db, clip_count, clip_pct, clip_rate_per_sec, lufs):
    integrated_loudness = lufs.get("integrated_loudness") if lufs else None
    loudness_range = lufs.get("loudness_range") if lufs else None
    true_peak = lufs.get("true_peak") if lufs else None
    true_peak_clip_count = int(lufs.get("true_peak_clip_count", 0)) if lufs else 0
    dynamic_range = peak_db - avg_db
    true_peak_clip_rate_per_sec = (true_peak_clip_count / args.sample_seconds) if args.sample_seconds > 0 else 0.0
    true_peak_clipping_detected = bool(true_peak_clip_count or (true_peak is not None and true_peak >= 0.0))
    true_peak_near_limit = bool(true_peak is not None and -1.0 <= true_peak < 0.0)
    true_peak_available = true_peak is not None

    if true_peak_clipping_detected:
        true_peak_status = "clipping"
    elif true_peak_near_limit:
        true_peak_status = "near_limit"
    elif true_peak_available:
        true_peak_status = "ok"
    else:
        true_peak_status = "unavailable"

    source = {
        "mode": "vod" if is_vod else "live",
        "channel": (stream_info.get("channel") if stream_info else None) or args.channel,
        "title": stream_info.get("title") if stream_info else None,
        "sampleStartTime": sample_start_time,
        "sampleStartTimeUtc": sample_start_time_utc,
    }
    if is_vod:
        source.update({
            "vodUrl": args.vod_url,
            "vodId": stream_info.get("vod_id") if stream_info else None,
            "publishedAt": stream_info.get("published_at") if stream_info else None,
            "startOffsetSeconds": int(start_seconds),
            "startOffset": format_hhmmss(start_seconds),
        })

    return {
        "schemaVersion": 1,
        "meterConfigUrl": "/meter_config.json",
        "meterSvgUrl": "/loudness_meter.svg",
        "source": source,
        "analysis": {
            "sampleSeconds": int(args.sample_seconds),
            "totalSamples": int(len(samples)),
            "rms": {
                "averageDb": _round_metric(avg_db),
                "dynamicRangeDb": _round_metric(dynamic_range),
                "peakDb": _round_metric(peak_db),
            },
            "sampleClipping": {
                "detected": bool(clip_count),
                "noClippingDetected": not bool(clip_count),
                "count": int(clip_count),
                "percent": _round_metric(clip_pct, 6),
                "ratePerSecond": _round_metric(clip_rate_per_sec),
            },
            "loudness": {
                "available": bool(lufs),
                "integratedLufs": _round_metric(integrated_loudness),
                "integrated_loudness": _round_metric(integrated_loudness),
                "loudnessRangeLu": _round_metric(loudness_range),
                "lraLu": _round_metric(loudness_range),
                "loudness_range": _round_metric(loudness_range),
                "truePeakDb": _round_metric(true_peak),
                "true_peak": _round_metric(true_peak),
                "truePeakAvailable": true_peak_available,
                "truePeakClipEventCount": true_peak_clip_count,
                "true_peak_clip_count": true_peak_clip_count,
                "truePeakClipRatePerSecond": _round_metric(true_peak_clip_rate_per_sec),
                "truePeakClippingDetected": true_peak_clipping_detected,
                "noTruePeakClippingDetected": true_peak_available and not true_peak_clipping_detected and not true_peak_near_limit,
                "truePeakNearLimit": true_peak_near_limit,
                "truePeakStatus": true_peak_status,
            },
        },
    }


def emit_result_payload(payload):
    print(f"{RESULT_JSON_PREFIX}{json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}", flush=True)


def main():
    # Enable line-buffering for real-time output (equivalent to python -u or perl $| = 1)
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace', line_buffering=True)
    
    args = parse_args()
    global DEBUG
    DEBUG = bool(args.debug)
    is_vod = bool(args.vod_url)

    client_id, token = resolve_credentials()
    if not client_id or not token:
        print(
            "client id and token must be provided via environment variables or config file",
            file=sys.stderr,
        )
        print(
            f"Config path: {CONFIG_PATH} | app-name: {APP_NAME} | token-name: {TOKEN_NAME}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.sample_seconds > MAX_SAMPLE_SECONDS:
        print(f"sample_seconds must be <= {MAX_SAMPLE_SECONDS // 60} minutes ({MAX_SAMPLE_SECONDS} seconds)", file=sys.stderr)
        sys.exit(1)

    start_seconds = args.start_time

    ffmpeg_cmd = check_ffmpeg()

    stream_info = None
    if not is_vod:
        # Get stream info via Helix API for live channel mode
        print(f"Fetching stream info for '{args.channel}' from Helix API...", flush=True)
        stream_info, error_text = get_stream_info(args.channel, client_id, token)
        if not stream_info:
            if error_text and "invalid oauth token" in error_text.lower():
                if refresh_token_via_script():
                    # Reload credentials after refresh and retry once
                    client_id, token = resolve_credentials()
                    stream_info, error_text = get_stream_info(args.channel, client_id, token)
                    if not stream_info:
                        print("Token refresh did not resolve the API error.", file=sys.stderr)
                        sys.exit(1)
                else:
                    print("Token refresh failed; aborting.", file=sys.stderr)
                    sys.exit(1)
            else:
                sys.exit(1)
    else:
        print(f"Processing VOD: {args.vod_url}")
        print(f"Start Offset: {format_hhmmss(start_seconds)} ({start_seconds} seconds)\n")
        vod_info, vod_err = get_vod_info(args.vod_url, client_id, token)
        if vod_info:
            stream_info = {
                "title": vod_info.get("title") or "(VOD)",
                "channel": vod_info.get("channel") or "(unknown)",
                "published_at": vod_info.get("published_at") or vod_info.get("created_at"),
                "vod_id": vod_info.get("id"),
            }
        else:
            if vod_err:
                print(f"Warning: could not fetch VOD metadata: {vod_err}")
            stream_info = {
                "title": "(VOD)",
                "channel": "(unknown)",
                "published_at": None,
                "vod_id": extract_vod_id(args.vod_url) or "(unknown)",
            }
    
    # Get HLS URL via streamlink (pass credentials for authentication)
    source = args.vod_url if is_vod else args.channel
    print(f"Fetching HLS URL via streamlink...", flush=True)
    hls_url = get_hls_url(source, client_id, token, is_vod=is_vod)
    if not hls_url:
        sys.exit(1)
    
    # Capture and analyze audio (with ad detection)
    print(f"Starting ffmpeg audio capture ({args.sample_seconds} seconds)...", flush=True)
    capture_result = capture_audio_with_ad_handling(
        hls_url,
        ffmpeg_cmd,
        args.sample_seconds,
        start_seconds=start_seconds,
        is_vod=is_vod,
    )
    if not capture_result:
        sys.exit(1)
    samples, live_sample_start_utc = capture_result

    if is_vod:
        sample_start_time = format_hhmmss(start_seconds)
        sample_start_time_utc = None
    else:
        sample_start_time_utc = (
            live_sample_start_utc.isoformat().replace("+00:00", "Z")
            if live_sample_start_utc else None
        )
        sample_start_time = sample_start_time_utc
    
    avg_db, peak_db = calculate_db(samples)
    # sample-level clipping detection (raw PCM hard clips)
    clip_count = sum(1 for s in samples if s >= 32767 or s <= -32768)
    clip_pct = (clip_count / len(samples) * 100.0) if samples else 0.0
    clip_rate_per_sec = (clip_count / args.sample_seconds) if args.sample_seconds > 0 else 0.0

    # Also run ffmpeg's ebur128 to compute LUFS / LRA / True Peak
    lufs = compute_lufs(
        hls_url,
        ffmpeg_cmd,
        args.sample_seconds,
        start_seconds=start_seconds if is_vod else None,
    )

    print("=" * 60, flush=True)
    print("AUDIO ANALYSIS RESULTS", flush=True)
    print("=" * 60, flush=True)
    if is_vod:
        print(f"VOD URL: {args.vod_url}")
        if stream_info.get("vod_id"):
            print(f"VOD ID: {stream_info['vod_id']}")
        if stream_info.get("channel"):
            print(f"Channel: {stream_info['channel']}")
        if stream_info.get("published_at"):
            print(f"VOD Date (UTC): {stream_info['published_at']}")
        print(f"Start Offset: {format_hhmmss(start_seconds)} ({start_seconds} seconds)")
    else:
        print(f"Channel: {args.channel}")
    print(f"Stream Title: {stream_info['title']}")
    if sample_start_time:
        print(f"Sample Start Time: {sample_start_time}")
    print(f"Sample Duration: {args.sample_seconds} seconds")
    print(f"Total Samples: {len(samples)}")
    print(f"\nRMS Average Level: {avg_db:.2f} dB")
    print(f"RMS Dynamic Range: {peak_db - avg_db:.2f} dB")
    print(f"RMS Peak Level: {peak_db:.2f} dB")
    if clip_count:
        print(
            f"*** SAMPLE HARD-CLIPS: {clip_count} samples "
            f"({clip_pct:.6f}% | {clip_rate_per_sec:.2f}/sec) ***"
        )
    else:
        print("No sample-level clipping detected.")

    if lufs:
        il = lufs.get("integrated_loudness")
        lra = lufs.get("loudness_range")
        tp = lufs.get("true_peak")
        tp_clip_count = lufs.get("true_peak_clip_count", 0)
        tp_clip_rate_per_sec = (tp_clip_count / args.sample_seconds) if args.sample_seconds > 0 else 0.0
        raw = lufs.get("raw_stderr", "")
        print("\nLUFS / Loudness Range / True Peak (via ffmpeg ebur128):")
        if il is not None:
            print(f"  Integrated Loudness (I): {il:.2f} LUFS")
        else:
            print("  Integrated Loudness (I): (not available)")
        if lra is not None:
            print(f"  Loudness Range: {lra:.2f} LU")
        else:
            print("  Loudness Range (LRA): (not available)")
        if tp is not None:
            print(f"  True Peak (TPK): {tp:.2f} dB")
            if tp >= 0.0:
                print(
                    "  *** TRUE-PEAK CLIP EVENTS: "
                    f"{tp_clip_count} frame events >= 0 dBTP "
                    f"({tp_clip_rate_per_sec:.2f}/sec) ***"
                )
                print("  Note: sample hard-clips and true-peak clip events are different measurements and not 1:1 comparable.")
            elif tp >= -1.0:
                print("  *** TRUE-PEAK NEAR LIMIT: possible clipping risk (>= -1.0 dBTP) ***")
            else:
                print("  No True-peak clipping detected")
        else:
            print("  True Peak: (not available)")

        # If values look suspicious or true peak is missing, show ffmpeg's stderr for debugging
        suspicious = (
            tp is None or
            il is None or
            (isinstance(il, float) and il < -60) or
            (isinstance(lra, float) and lra == 0)
        )
        if suspicious:
            print("\n[DEBUG] ffmpeg ebur128 output (for debugging):")
            # print a reasonable amount
            for line in raw.splitlines()[-200:]:
                print("  ", line)
    else:
        print("\nLUFS metrics not available (ffmpeg ebur128 failed).")

    emit_result_payload(
        build_result_payload(
            args,
            is_vod,
            stream_info,
            start_seconds,
            sample_start_time,
            sample_start_time_utc,
            samples,
            avg_db,
            peak_db,
            clip_count,
            clip_pct,
            clip_rate_per_sec,
            lufs,
        )
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
