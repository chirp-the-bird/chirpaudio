#!/usr/bin/env python3
"""WSGI application for Twitch Audio Tester with streaming output support."""
import mimetypes
import sys
import warnings
from urllib.parse import parse_qs
import os
import re
import subprocess
import time
from threading import Thread
from queue import Queue, Empty

# Route warnings to stderr
def _warning_to_stderr(message, category, filename, lineno, file=None, line=None):
    sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))
    sys.stderr.flush()

warnings.showwarning = _warning_to_stderr
warnings.simplefilter("default")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYZER = os.path.join(BASE_DIR, "twitch_audio_test.py")
RESULT_JSON_PREFIX = "CHIRPAUDIO_RESULT_JSON:"
STATIC_ASSETS = {
    "/meter_config.json": "meter_config.json",
    "/loudness_meter.svg": "loudness_meter.svg",
}
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")


def load_app_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as version_file:
            version = version_file.read().strip()
            if version:
                return version
    except Exception:
        pass
    return "0.0.0"

APP_VERSION = load_app_version()

CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
HHMMSS_RE = re.compile(r"^\d+:[0-5]\d:[0-5]\d$")


def as_int(value, default, min_v=1, max_v=360):
    try:
        v = int(value)
    except Exception:
        return default
    return max(min_v, min(max_v, v))


def parse_form_data(environ):
    """Parse POST/GET form data from WSGI environ."""
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method == "GET":
        query_string = environ.get("QUERY_STRING", "")
        parsed = parse_qs(query_string, keep_blank_values=True)
    else:
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
        except ValueError:
            content_length = 0
        body = environ["wsgi.input"].read(content_length).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
    
    return {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed.items()}


def getfirst(form, key, default=""):
    value = form.get(key, default)
    return value if value is not None else default


def build_command(form):
    """Build subprocess command from form data."""
    mode = (getfirst(form, "mode") or "").strip().lower()
    sample_seconds = as_int(getfirst(form, "sample_seconds", "30"), 30)
    cmd = [sys.executable, "-u", ANALYZER]

    if mode == "live":
        channel = (getfirst(form, "channel") or "").strip()
        if not channel:
            raise ValueError("Missing channel.")
        if not CHANNEL_RE.fullmatch(channel):
            raise ValueError("Invalid channel format.")
        cmd += [channel, "--sample-seconds", str(sample_seconds)]
        return cmd

    if mode == "vod":
        vod_url = (getfirst(form, "vod_url") or "").strip()
        start_time = (getfirst(form, "start_time") or "").strip()
        if not vod_url:
            raise ValueError("Missing VOD URL.")
        if "twitch.tv" not in vod_url.lower():
            raise ValueError("VOD URL must be a twitch.tv URL.")
        cmd += ["--vod-url", vod_url, "--sample-seconds", str(sample_seconds)]
        if start_time:
            if not HHMMSS_RE.fullmatch(start_time):
                raise ValueError("start-time must be HH:MM:SS.")
            cmd += ["--start-time", start_time]
        return cmd

    raise ValueError("Invalid mode.")


def serve_static_asset(path_info, start_response):
    asset_name = STATIC_ASSETS.get(path_info)
    if not asset_name:
        return None

    file_path = os.path.join(BASE_DIR, asset_name)
    if not os.path.isfile(file_path):
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [f"Missing asset: {asset_name}\n".encode("utf-8")]

    mime_type, _ = mimetypes.guess_type(file_path)
    content_type = mime_type or "application/octet-stream"
    with open(file_path, "rb") as asset_file:
        payload = asset_file.read()

    start_response(
        "200 OK",
        [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
        ],
    )
    return [payload]


def stream_generator(cmd):
    """Generator that yields structured subprocess events as they arrive."""
    proc = None
    t = None
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            env=env,
            stdin=subprocess.DEVNULL,  # Don't let subprocess wait for stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        yield {"type": "stdout", "text": f"Failed to start process: {exc}\n"}
        return

    q: Queue = Queue()

    def pump_output(pipe, queue: Queue):
        try:
            for line in iter(pipe.readline, ""):
                queue.put(line)
        finally:
            queue.put(None)

    assert proc.stdout is not None
    t = Thread(target=pump_output, args=(proc.stdout, q), daemon=True)
    t.start()

    keepalive_interval = 8.0
    last_keepalive = time.monotonic()
    done = False
    try:
        while not done:
            try:
                item = q.get(timeout=0.25)
            except Empty:
                if proc.poll() is not None and q.empty():
                    break
                now = time.monotonic()
                if now - last_keepalive >= keepalive_interval:
                    last_keepalive = now
                    yield {"type": "keepalive"}
                continue

            if item is None:
                done = True
            else:
                stripped = item.rstrip("\r\n")
                if stripped.startswith(RESULT_JSON_PREFIX):
                    yield {"type": "result", "json": stripped[len(RESULT_JSON_PREFIX):].strip()}
                else:
                    yield {"type": "stdout", "text": item}

        rc = proc.wait()
        if rc == 0:
            yield {"type": "stdout", "text": "Done.\n"}
        else:
            yield {"type": "stdout", "text": f"\n[exit code: {rc}]\n"}
    finally:
        if proc and proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if t and t.is_alive():
            t.join(timeout=1.0)


def render_html():
    """Return HTML page."""
    return '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>Twitch Audio Tester</title>
<style>
  :root{
    --bg:#12161d;--panel:#1b2330;--panel2:#222d3d;--text:#dbe7ff;--muted:#9fb0cc;
    --accent:#5c7cff;--accent2:#7f5af0;--border:#2d3a52;color-scheme:dark;
  }
    html,body{min-height:100%;}
    body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:linear-gradient(180deg,#0f131a,#141b26);background-color:#141b26;color:var(--text);}
        .wrap{display:grid;grid-template-columns:1fr minmax(auto,1000px) 1fr;gap:0;padding:20px;margin:0;}
    .sidebar{grid-column:1;display:flex;flex-direction:column;align-items:flex-end;gap:0;padding-right:40px;}
    .main{grid-column:2;}
    .img-stack{position:relative;width:300px;overflow:hidden;}
    .img-stack video{position:absolute;top:0;left:50%;width:345px;height:100%;transform:translateX(-50%);object-fit:cover;object-position:top center;}
    .img-stack img{position:relative;z-index:1;width:300px;display:block;margin-top:20px;}
        .sidebar > img{display:block;width:300px;max-width:300px;}
    .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:0 10px 35px rgba(0,0,0,.35);display:flex;flex-direction:column}
  h1{margin:.2rem 0 1rem;display:flex;align-items:center;gap:.6rem;}
        .app-version{margin-left:auto;font-size:.82rem;color:var(--muted);font-weight:600;letter-spacing:.02em}
    h3{margin:0 0 .6rem}
  .logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:inline-block;}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;order:2}
  .form{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:12px;}
  label{display:block;font-size:.86rem;color:var(--muted);margin:.5rem 0 .2rem;}
    input,select{width:100%;box-sizing:border-box;padding:9px;border-radius:8px;border:1px solid #3a4a67;background:#111824;color:var(--text);}
    .inline-fields{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:end}
    .sample-seconds-compact{max-width:120px}
    button{display:block;margin-top:.8rem;background:var(--accent);color:white;border:none;border-radius:8px;padding:10px 14px;cursor:pointer}
  button:hover{filter:brightness(1.08)}
    .out{margin-top:16px;order:4}
    pre{background:#0f141d;border:1px solid var(--border);border-radius:10px;padding:12px;min-height:220px;max-height:60vh;overflow:auto;white-space:pre-wrap}
                .results{margin-top:16px;order:3}
                .meter-wrap{display:none;background:#0f141d;border:1px solid var(--border);border-radius:10px;padding:14px;overflow-x:auto}
                .meter-status{margin:0 0 12px;color:var(--muted);font-size:.9rem}
                .processing-indicator{display:inline-flex;align-items:center;gap:8px;color:#e9f2ff;font-weight:600}
                .processing-spinner{width:12px;height:12px;border-radius:50%;border:2px solid #4f6f91;border-top-color:#e9f2ff;animation:spin 900ms linear infinite}
                @keyframes spin{to{transform:rotate(360deg)}}
                .meter-channel-name{font-size:1.15rem;font-weight:700;color:#eef5ff;display:block;margin-bottom:3px}
                .meter-source-title{font-size:.9rem;color:var(--muted);display:block}
                .meter-board{display:grid;grid-template-columns:520px minmax(200px,280px);gap:20px;align-items:start}
                .meter-stack{display:flex;flex-direction:column;gap:18px}
                .meter-panel{background:#111824;border:1px solid #2e3d53;border-radius:12px;padding:14px}
                .meter-title{margin:0;font-size:1.05rem;color:#eef5ff}
                .meter-copy{margin:6px 0 0;color:var(--muted);font-size:.84rem;line-height:1.35}
                .meter-row{display:grid;grid-template-columns:minmax(0,1fr) 84px;gap:14px;align-items:center;margin-top:14px}
                .meter-range{display:flex;justify-content:space-between;align-items:center;margin:0 0 8px;color:#d5e2f1;font-size:.92rem}
                .meter-shell{background:#0f1822;border:1px solid #3a5069;border-radius:14px;padding:10px;position:relative;overflow:visible}
                .meter-fill{position:relative;border-radius:10px;overflow:hidden}
                .meter-fill.horizontal{height:54px}
                .meter-fill.vertical{width:54px;height:370px;margin:0}
                .meter-fill::after{content:'';position:absolute;inset:0;pointer-events:none}
                .value-marker{position:absolute;display:none;pointer-events:none;z-index:4}
                .value-marker-v{top:0;bottom:0;width:2px;background:#f8fcff;box-shadow:0 0 6px rgba(231,245,255,.5)}
                .value-marker-h{left:0;right:0;height:2px;background:#f8fcff;box-shadow:0 0 6px rgba(231,245,255,.5)}
                .value-marker.alt{background:#7de7dd;box-shadow:0 0 6px rgba(0,182,174,.65)}
                .value-label{position:absolute;background:rgba(8,14,20,.92);border:1px solid #415c7a;border-radius:6px;padding:2px 6px;color:#eaf3ff;font-size:.74rem;line-height:1.1;white-space:nowrap}
                .value-marker-v .value-label{top:6px;left:8px}
                .value-marker-v.label-bottom .value-label{top:auto;bottom:6px}
                .value-marker-v.label-left .value-label{left:auto;right:8px}
                .value-marker-v .value-label::before{content:'';position:absolute;top:50%;right:100%;width:8px;border-top:1px solid rgba(0,0,0,.9);transform:translateY(-50%)}
                .value-marker-v.label-left .value-label::before{right:auto;left:100%}
                .value-marker-h .value-label{top:50%;left:calc(100% + 8px);transform:translateY(-50%)}
                .rms-gradient{background:linear-gradient(90deg,#0A1A3A 0%,#1E6A5A 48%,#3CC76A 72%,#00b6ae 90%,#FFFFFF 100%)}
                .lufs-gradient{background:linear-gradient(90deg,#0A1A3A 0%,#1E6A5A 22%,#3CC76A 38%,#00b6ae 70%,#FFFFFF 78%,#FFFFFF 100%)}
                .lra-gradient{background:linear-gradient(0deg,#0A1A3A 0%,#1E6A5A 18%,#3CC76A 38%,#00b6ae 88%,#9ff3ef 98%,#FFFFFF 100%)}
                .horizontal-ticks::after{background:repeating-linear-gradient(90deg,rgba(201,212,223,.4) 0 1px,transparent 1px 10%)}
                .vertical-ticks::after{background:repeating-linear-gradient(0deg,rgba(201,212,223,.4) 0 1px,transparent 1px 16.6667%)}
                .scale-labels{margin-top:10px;color:#c7d4e2;font-size:.82rem}
                .scale-labels.horizontal-11{position:relative;height:16px;width:calc(100% - 20px);margin:10px auto 0}
                .scale-labels.horizontal-11 span{position:absolute;top:0;transform:translateX(-50%);text-align:center;min-width:22px;line-height:1}
                .scale-labels.horizontal-11 span:nth-child(1){left:0%;transform:translateX(-50%);text-align:center;min-width:22px}
                .scale-labels.horizontal-11 span:nth-child(2){left:10%}
                .scale-labels.horizontal-11 span:nth-child(3){left:20%}
                .scale-labels.horizontal-11 span:nth-child(4){left:30%}
                .scale-labels.horizontal-11 span:nth-child(5){left:40%}
                .scale-labels.horizontal-11 span:nth-child(6){left:50%}
                .scale-labels.horizontal-11 span:nth-child(7){left:60%}
                .scale-labels.horizontal-11 span:nth-child(8){left:70%}
                .scale-labels.horizontal-11 span:nth-child(9){left:80%}
                .scale-labels.horizontal-11 span:nth-child(10){left:90%}
                .scale-labels.horizontal-11 span:nth-child(11){left:100%;transform:translateX(-50%);text-align:center;min-width:22px}
                .clip-box{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;background:#111824;border:1px solid #2e3d53;border-radius:12px;min-height:132px}
                .clip-label{font-size:.82rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
                .clip-led{width:21px;height:21px;border-radius:50%;background:linear-gradient(180deg,#5b1717,#2a0a0a);border:1px solid #7a2d2d;box-shadow:inset 0 1px 2px rgba(255,255,255,.08)}
                .clip-led.active{background:radial-gradient(circle at 35% 30%,#ffd5d5 0%,#ff6c6c 35%,#cb1111 68%,#5b0a0a 100%);border-color:#ff7171;box-shadow:0 0 10px rgba(255,80,80,.78),inset 0 1px 3px rgba(255,255,255,.3)}
                .value-marker.warn-near{background:#ffe26f;box-shadow:0 0 8px rgba(255,226,111,.8)}
                .value-marker.warn-clip{background:#ff4a4a;box-shadow:0 0 9px rgba(255,74,74,.88)}
                .lra-panel .meter-copy{margin-bottom:12px}
                .lra-panel .meter-shell{width:fit-content}
                .vertical-layout{display:grid;grid-template-columns:44px auto;gap:14px;align-items:center;margin:0 auto;width:fit-content;transform:translateX(-30px)}
                .vertical-labels{position:relative;height:370px;color:#c7d4e2;font-size:.82rem}
                .vertical-labels span{position:absolute;right:0;transform:translateY(-50%);display:block;line-height:1}
                .vertical-ends{display:flex;flex-direction:column;align-items:center;gap:10px;color:#d5e2f1;font-size:.82rem;margin-bottom:10px}
                .meter-note{margin:14px 0 0;padding:10px 12px;background:#111824;border:1px solid #2e3d53;border-radius:8px;color:var(--muted);font-size:.88rem}
  .hint{font-size:.82rem;color:var(--muted)}
    @media (max-width:860px){.grid{grid-template-columns:1fr}.meter-board{grid-template-columns:1fr}}
                @media (max-width:1100px){.wrap{grid-template-columns:1fr;}.sidebar{grid-column:1;align-items:center;padding-right:0;padding-bottom:20px;}.main{grid-column:1;}}
                @media (max-width:760px){.inline-fields{grid-template-columns:1fr}.sample-seconds-compact{max-width:100%}.meter-row{grid-template-columns:1fr;}.clip-box{min-height:80px;padding:12px}.meter-fill.vertical{height:300px}.vertical-labels{height:300px}.vertical-layout{transform:translateX(-18px)}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="sidebar">
    <div class="img-stack">
      <video autoplay loop muted playsinline>
        <source src="/EQ.mp4" type="video/mp4" />
      </video>
      <img src="/ChirpAudio.png" alt="ChirpAudio" />
    </div>
    <img src="/chirpthebird_discord.png" alt="Chirp the Bird" />
    </div>
    <div class="main">
    <div class="card">
    <h1><span class="logo"></span> Twitch Audio Test <span class="app-version">v __APP_VERSION__</span></h1>
      <div class="hint">Runs Chirpaudio Twitch Audio Test for a live stream or VOD.</div>

      <div class="grid">
        <form id="liveForm" class="form" method="post" action="">
          <h3>Live Channel</h3>
          <input type="hidden" name="mode" value="live" />
          <label>Channel</label>
          <input name="channel" value="willowstephens" required />
                    <label>Sample Seconds</label>
                    <select class="sample-seconds-compact" name="sample_seconds">
                        <option value="15">15</option>
                        <option value="30" selected>30</option>
                        <option value="60">60</option>
                        <option value="120">120</option>
                    </select>
          <button type="submit">Run Live Test</button>
        </form>

        <form id="vodForm" class="form" method="post" action="">
          <h3>VOD</h3>
          <input type="hidden" name="mode" value="vod" />
          <label>VOD URL</label>
          <input name="vod_url" placeholder="https://www.twitch.tv/videos/123456789" />
                    <div class="inline-fields">
                        <div>
                            <label>Start Time (HH:MM:SS)</label>
                            <input name="start_time" placeholder="00:00:00" />
                        </div>
                        <div>
                            <label>Sample Seconds</label>
                            <select name="sample_seconds">
                                    <option value="15">15</option>
                                    <option value="30" selected>30</option>
                                    <option value="60">60</option>
                                    <option value="120">120</option>
                            </select>
                        </div>
                    </div>
          <button type="submit">Run VOD Test</button>
        </form>
      </div>

            <div class="results">
                <h3>Results</h3>
                <div id="resultsHint" class="hint">Generate audio meters with RMS level and peak, LUFS Loudness and True Peak, clipping indicators and Loudness range.</div>
                                                                <div id="meterWrap" class="meter-wrap">
                                                                        <p id="meterStatus" class="meter-status">Waiting for run...</p>
                                                                                                                                                <div id="meterBoard" class="meter-board">
                                                                            <div class="meter-stack">
                                                                                <section class="meter-panel">
                                                                                    <h4 class="meter-title">RMS Loudness</h4>
                                                                                    <p class="meter-copy">Audio sample RMS average loudness and RMS peak level (in dBFS).</p>
                                                                                    <div class="meter-row">
                                                                                        <div>
                                                                                            <div class="meter-range"><span>-50 dB</span><span>0 dB</span></div>
                                                                                            <div id="rmsShell" class="meter-shell">
                                                                                                <div class="meter-fill horizontal rms-gradient horizontal-ticks"></div>
                                                                                                <div id="rmsAvgMarker" class="value-marker value-marker-v label-bottom"><span id="rmsAvgLabel" class="value-label">-- db</span></div>
                                                                                                <div id="rmsPeakMarker" class="value-marker value-marker-v alt"><span id="rmsPeakLabel" class="value-label">-- db</span></div>
                                                                                            </div>
                                                                                            <div class="scale-labels horizontal-11">
                                                                                                <span>-50</span><span>-45</span><span>-40</span><span>-35</span><span>-30</span><span>-25</span><span>-20</span><span>-15</span><span>-10</span><span>-5</span><span>0</span>
                                                                                            </div>
                                                                                        </div>
                                                                                        <aside class="clip-box">
                                                                                            <div class="clip-label">Clip</div>
                                                                                            <div id="rmsClipLed" class="clip-led" aria-hidden="true"></div>
                                                                                        </aside>
                                                                                    </div>
                                                                                </section>

                                                                                <section class="meter-panel">
                                                                                    <h4 class="meter-title">LUFS Loudness</h4>
                                                                                    <p class="meter-copy">Audio sample Integrated loudness (LUFS) and True Peak (dBTPK).</p>
                                                                                    <div class="meter-row">
                                                                                        <div>
                                                                                            <div class="meter-range"><span>-40 LUFS</span><span>10 LUFS</span></div>
                                                                                            <div id="lufsShell" class="meter-shell">
                                                                                                <div class="meter-fill horizontal lufs-gradient horizontal-ticks"></div>
                                                                                                <div id="lufsIntMarker" class="value-marker value-marker-v label-bottom"><span id="lufsIntLabel" class="value-label">-- LUFS</span></div>
                                                                                                <div id="lufsTpkMarker" class="value-marker value-marker-v alt"><span id="lufsTpkLabel" class="value-label">-- TPK</span></div>
                                                                                            </div>
                                                                                            <div class="scale-labels horizontal-11">
                                                                                                <span>-40</span><span>-35</span><span>-30</span><span>-25</span><span>-20</span><span>-15</span><span>-10</span><span>-5</span><span>0</span><span>5</span><span>10</span>
                                                                                            </div>
                                                                                        </div>
                                                                                        <aside class="clip-box">
                                                                                            <div class="clip-label">Clip</div>
                                                                                            <div id="lufsClipLed" class="clip-led" aria-hidden="true"></div>
                                                                                        </aside>
                                                                                    </div>
                                                                                </section>
                                                                            </div>

                                                                            <section class="meter-panel lra-panel">
                                                                                <h4 class="meter-title">Loudness Range (LRA)</h4>
                                                                                <p class="meter-copy">Audio sample Loudness Range (LRA) indicating dynamic spread/compression.</p>
                                                                                <div class="vertical-ends">
                                                                                    <span>Highly Dynamic</span>
                                                                                </div>
                                                                                <div class="vertical-layout">
                                                                                    <div class="vertical-labels">
                                                                                        <span style="top:0%">30</span><span style="top:16.67%">25</span><span style="top:33.33%">20</span><span style="top:50%">15</span><span style="top:66.67%">10</span><span style="top:83.33%">5</span><span style="top:100%">0</span>
                                                                                    </div>
                                                                                    <div id="lraShell" class="meter-shell">
                                                                                        <div class="meter-fill vertical lra-gradient vertical-ticks"></div>
                                                                                        <div id="lraMarker" class="value-marker value-marker-h"><span id="lraLabel" class="value-label">-- LU</span></div>
                                                                                    </div>
                                                                                </div>
                                                                                <div class="vertical-ends">
                                                                                    <span>Compressed</span>
                                                                                </div>
                                                                            </section>
                                                                        </div>
                                                                        <div id="meterNote" class="meter-note">Layout scaffold loaded. Marker bars, LED state changes, and numeric overlays will be wired to result values next.</div>
                                                                </div>
            </div>

            <div class="out">
                <h3>Log</h3>
                <pre id="output">Ready.</pre>
                <noscript><p class="hint">JavaScript is required for live streaming output in this view.</p></noscript>
            </div>
    </div>
    </div>
  </div>

<script>
let currentSource = null;
let lastResultPayload = null;
function escHtml(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

function setRunButtonsDisabled(disabled){
    document.querySelectorAll('#liveForm button[type="submit"], #vodForm button[type="submit"]').forEach((btn) => {
        btn.disabled = disabled;
        btn.style.opacity = disabled ? '0.65' : '1';
        btn.style.cursor = disabled ? 'not-allowed' : 'pointer';
    });
}

function asNumber(v){
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
}

function pickNumber(candidates){
    for (const candidate of candidates){
        const n = asNumber(candidate);
        if (n !== null){
            return n;
        }
    }
    return null;
}

function clampToRange(value, minValue, maxValue){
    if (!Number.isFinite(value)) return value;
    return Math.max(minValue, Math.min(maxValue, value));
}

function formatValue(value, unit){
    if (!Number.isFinite(value)) return '-- ' + unit;
    return value.toFixed(2) + ' ' + unit;
}

function setClipLed(id, isActive){
    const led = document.getElementById(id);
    if (!led) return;
    led.classList.toggle('active', !!isActive);
}

function hideMarker(markerId){
    const marker = document.getElementById(markerId);
    if (!marker) return;
    marker.classList.remove('warn-near', 'warn-clip');
    marker.style.display = 'none';
}

function applyPeakMarkerWarning(markerId, value){
    const marker = document.getElementById(markerId);
    if (!marker) return;
    marker.classList.remove('warn-near', 'warn-clip');
    if (!Number.isFinite(value)) return;
    if (value > -1){
        marker.classList.add('warn-clip');
    } else if (value > -3 && value <= -1){
        marker.classList.add('warn-near');
    }
}

function placeHorizontalMarker(shellId, markerId, labelId, value, minValue, maxValue, unit){
    const shell = document.getElementById(shellId);
    const marker = document.getElementById(markerId);
    const label = document.getElementById(labelId);
    if (!shell || !marker || !label) return;

    if (!Number.isFinite(value)){
        hideMarker(markerId);
        return;
    }

    const pinnedValue = clampToRange(value, minValue, maxValue);
    const style = window.getComputedStyle(shell);
    const padLeft = parseFloat(style.paddingLeft) || 0;
    const padRight = parseFloat(style.paddingRight) || 0;
    const width = shell.clientWidth;
    const usableWidth = Math.max(0, width - padLeft - padRight);
    const range = maxValue - minValue;
    const ratio = range === 0 ? 0 : (pinnedValue - minValue) / range;
    const clamped = Math.max(0, Math.min(1, ratio));
    const x = padLeft + (usableWidth * clamped);

    marker.style.left = `${x}px`;
    marker.style.display = 'block';
    marker.classList.toggle('label-left', x > width - 98);
    label.textContent = formatValue(value, unit);
}

function placeVerticalMarker(shellId, markerId, labelId, value, minValue, maxValue, unit){
    const shell = document.getElementById(shellId);
    const marker = document.getElementById(markerId);
    const label = document.getElementById(labelId);
    if (!shell || !marker || !label) return;

    if (!Number.isFinite(value)){
        hideMarker(markerId);
        return;
    }

    const pinnedValue = clampToRange(value, minValue, maxValue);
    const style = window.getComputedStyle(shell);
    const padTop = parseFloat(style.paddingTop) || 0;
    const padBottom = parseFloat(style.paddingBottom) || 0;
    const height = shell.clientHeight;
    const usableHeight = Math.max(0, height - padTop - padBottom);
    const range = maxValue - minValue;
    const ratio = range === 0 ? 0 : (pinnedValue - minValue) / range;
    const clamped = Math.max(0, Math.min(1, ratio));
    const y = height - padBottom - (usableHeight * clamped);

    marker.style.top = `${y}px`;
    marker.style.display = 'block';
    label.textContent = formatValue(value, unit);
}

function resetMeters(){
    hideMarker('rmsAvgMarker');
    hideMarker('rmsPeakMarker');
    hideMarker('lufsIntMarker');
    hideMarker('lufsTpkMarker');
    hideMarker('lraMarker');
    setClipLed('rmsClipLed', false);
    setClipLed('lufsClipLed', false);
}

function renderMeters(payload){
    const analysis = payload.analysis || {};
    const rms = analysis.rms || {};
    const loud = analysis.loudness || {};
    const sampleClip = analysis.sampleClipping || {};

    const rmsAvg = pickNumber([rms.averageDb, rms.avgDb]);
    const rmsPeak = pickNumber([rms.peakDb]);
    const integratedLufs = pickNumber([loud.integratedLufs, loud.integrated_loudness]);
    const truePeak = pickNumber([loud.truePeakDb, loud.true_peak]);
    const lra = pickNumber([loud.loudnessRangeLu, loud.lraLu, loud.loudness_range]);

    placeHorizontalMarker('rmsShell', 'rmsAvgMarker', 'rmsAvgLabel', rmsAvg, -50, 0, 'db ave');
    placeHorizontalMarker('rmsShell', 'rmsPeakMarker', 'rmsPeakLabel', rmsPeak, -50, 0, 'db pk');
    placeHorizontalMarker('lufsShell', 'lufsIntMarker', 'lufsIntLabel', integratedLufs, -40, 10, 'LUFS');
    placeHorizontalMarker('lufsShell', 'lufsTpkMarker', 'lufsTpkLabel', truePeak, -40, 10, 'TPK');
    placeVerticalMarker('lraShell', 'lraMarker', 'lraLabel', lra, 0, 30, 'LU');

    applyPeakMarkerWarning('rmsPeakMarker', rmsPeak);
    applyPeakMarkerWarning('lufsTpkMarker', truePeak);

    const rmsPeakAtOrOverClip = Number.isFinite(rmsPeak) && rmsPeak >= -0.005;
    setClipLed('rmsClipLed', !!sampleClip.detected || rmsPeakAtOrOverClip);
    setClipLed('lufsClipLed', !!loud.truePeakClippingDetected);
}

async function runForm(form){
  const out = document.getElementById('output');
    const meterWrap = document.getElementById('meterWrap');
        const resultsHint = document.getElementById('resultsHint');
    const meterStatus = document.getElementById('meterStatus');
    const meterBoard = document.getElementById('meterBoard');
    const meterNote = document.getElementById('meterNote');
    const startedAt = new Date().toLocaleString();
    if ((out.textContent || '').trim() === 'Ready.'){
        out.textContent = '';
    } else if (out.textContent && out.textContent.trim()){
        out.textContent += '\\n';
    }
    out.textContent += `===== ${startedAt} =====\\n`;
    meterWrap.style.display = 'block';
    if (resultsHint) resultsHint.style.display = 'block';
    meterBoard.style.display = 'none';
    meterNote.style.display = 'none';
    meterStatus.innerHTML = '<span class="processing-indicator"><span class="processing-spinner" aria-hidden="true"></span>Processing...</span>';
    lastResultPayload = null;
    resetMeters();
    setRunButtonsDisabled(true);
    const qs = new URLSearchParams(new FormData(form));
    qs.append('stream', '1');
    let gotResult = false;

    if (currentSource){
        currentSource.close();
        currentSource = null;
    }

    const url = window.location.pathname + '?' + qs.toString();
    const source = new EventSource(url);
    currentSource = source;

    source.onmessage = (evt) => {
        out.textContent += evt.data + '\\n';
        out.scrollTop = out.scrollHeight;
    };

    source.addEventListener('result', (evt) => {
        try {
            const payload = JSON.parse(evt.data);
            const src = payload.source || {};
            const ch = src.channel || src.channelName || src.user_name || src.userName || src.broadcaster_login || src.broadcasterLogin || '';
            const title = src.title || src.streamTitle || '';
            gotResult = true;
            if (resultsHint) resultsHint.style.display = 'none';
            meterBoard.style.display = 'grid';
            meterNote.style.display = 'block';
            meterStatus.innerHTML = (ch ? '<strong class="meter-channel-name">' + escHtml(ch) + '</strong>' : '') +
                (title ? '<span class="meter-source-title">' + escHtml(title) + '</span>' : (!ch ? 'Results received.' : ''));
            lastResultPayload = payload;
            renderMeters(payload);
            meterNote.textContent = 'Meter green zones indicate target ranges for audio level. Avoid only the very dark and light. Peak lines turn yellow or red to indicate loud audio/risk of clipping. Consistent clipping should not be ignored. Loudness Range (LRA) indicates dynamic spread; higher is more dynamic, lower is more compressed.';
        } catch (err) {
            meterStatus.textContent = 'Result parse error';
            meterBoard.style.display = 'none';
            meterNote.style.display = 'block';
            meterNote.textContent = 'Failed to parse structured results.\\n\\n' + evt.data;
            resetMeters();
        }
    });

    source.addEventListener('done', () => {
        if (!gotResult){
            meterStatus.textContent = 'Timeout';
            meterBoard.style.display = 'none';
            meterNote.style.display = 'block';
            meterNote.textContent = 'Processing finished but no structured result was received.';
        }
        source.close();
        if (currentSource === source){
            currentSource = null;
            setRunButtonsDisabled(false);
        }
    });

    source.onerror = () => {
        if (!gotResult){
            meterStatus.textContent = 'Processing error';
            meterBoard.style.display = 'none';
            meterNote.style.display = 'block';
            meterNote.textContent = 'Connection closed before results were returned. Check server logs and retry.';
        }
        source.close();
        if (currentSource === source){
            currentSource = null;
            setRunButtonsDisabled(false);
        }
    };
}
document.getElementById('liveForm').addEventListener('submit', e => { e.preventDefault(); runForm(e.target); });
document.getElementById('vodForm').addEventListener('submit', e => { e.preventDefault(); runForm(e.target); });
window.addEventListener('resize', () => {
    if (lastResultPayload){
        renderMeters(lastResultPayload);
    }
});
</script>
</body>
</html>'''.replace("__APP_VERSION__", APP_VERSION)


def application(environ, start_response):
    """WSGI application entry point."""
    try:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path_info = environ.get("PATH_INFO", "") or ""
        form = parse_form_data(environ)

        stream = (getfirst(form, "stream") or "").strip()

        static_response = serve_static_asset(path_info, start_response)
        if static_response is not None:
            return static_response

        if method == "GET" and stream == "1":
            # Stream analysis output as SSE
            try:
                cmd = build_command(form)
            except ValueError as exc:
                start_response("200 OK", [("Content-Type", "text/event-stream")])
                return [f"data: Input error: {exc}\n\n".encode("utf-8")]

            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache, no-transform"),
                    ("Pragma", "no-cache"),
                    ("Connection", "keep-alive"),
                    ("X-Accel-Buffering", "no"),
                    ("Content-Encoding", "identity"),
                ],
            )

            def sse_encode(text: str):
                for line in text.splitlines():
                    yield f"data: {line}\n\n".encode("utf-8")

            def sse_event(event_name: str, data: str):
                for line in data.splitlines() or [""]:
                    yield f"event: {event_name}\ndata: {line}\n\n".encode("utf-8")

            def generate():
                events = stream_generator(cmd)
                # Initial SSE padding helps defeat intermediary/browser buffering.
                yield b":" + (b" " * 4096) + b"\n\n"
                yield b"data: Starting...\n\n"
                yield from sse_encode(f"Running command:\n  {' '.join(cmd)}\n")
                try:
                    for event in events:
                        if event["type"] == "result":
                            yield from sse_event("result", event["json"])
                        elif event["type"] == "keepalive":
                            yield b": keepalive\n\n"
                        else:
                            yield from sse_encode(event["text"])
                    yield b"event: done\ndata: done\n\n"
                finally:
                    events.close()

            return generate()
        else:
            # Return HTML page
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
                    ("Pragma", "no-cache"),
                    ("Expires", "0"),
                ],
            )
            return [render_html().encode("utf-8")]

    except Exception as e:
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        import traceback
        return [
            f"Error: {str(e)}\n\n{traceback.format_exc()}".encode("utf-8")
        ]
