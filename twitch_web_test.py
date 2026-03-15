#!/usr/bin/env python3
import sys
import warnings
from urllib.parse import parse_qs, urlparse
import select
import fcntl
from threading import Thread
from queue import Queue, Empty

# Route all warnings to stderr (Apache error_log under CGI), never stdout.
def _warning_to_stderr(message, category, filename, lineno, file=None, line=None):
    sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))
    sys.stderr.flush()

warnings.showwarning = _warning_to_stderr
warnings.simplefilter("default")

import traceback

# Wrapper to catch all errors and ensure headers are sent
def safe_main():
    try:
        import os
        import re
        import subprocess

        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        ANALYZER = "/home/chirp/chirpaudio/twitch_audio_test.py"

        CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
        HHMMSS_RE = re.compile(r"^\d+:[0-5]\d:[0-5]\d$")

        def as_int(value, default, min_v=1, max_v=360):
            try:
                v = int(value)
            except Exception:
                return default
            return max(min_v, min(max_v, v))

        def parse_form_data():
            method = os.environ.get("REQUEST_METHOD", "GET").upper()
            if method == "GET":
                query = os.environ.get("QUERY_STRING", "")
                parsed = parse_qs(query, keep_blank_values=True)
                return {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed.items()}
            if method != "POST":
                return {}

            content_length_raw = os.environ.get("CONTENT_LENGTH", "0")
            try:
                content_length = int(content_length_raw)
            except Exception:
                content_length = 0

            body = sys.stdin.read(content_length) if content_length > 0 else ""
            parsed = parse_qs(body, keep_blank_values=True)
            # flatten to first value for convenience
            return {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed.items()}

        def getfirst(form, key, default=""):
            value = form.get(key, default)
            return value if value is not None else default

        def send_headers(content_type: str, sse: bool = False):
            sys.stdout.write(f"Content-Type: {content_type}\r\n")
            if sse:
                sys.stdout.write("Cache-Control: no-cache, no-transform\r\n")
                sys.stdout.write("Connection: keep-alive\r\n")
                sys.stdout.write("Content-Encoding: identity\r\n")
            else:
                sys.stdout.write("Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n")
            sys.stdout.write("Pragma: no-cache\r\n")
            if not sse:
                sys.stdout.write("Expires: 0\r\n")
            sys.stdout.write("X-Accel-Buffering: no\r\n\r\n")
            sys.stdout.flush()

        def emit(text: str, sse: bool = False):
            if not sse:
                sys.stdout.write(text)
                sys.stdout.flush()
                return
            for line in text.splitlines():
                sys.stdout.write(f"data: {line}\n\n")
            sys.stdout.flush()

        def build_command(form):
            mode = (getfirst(form, "mode") or "").strip().lower()
            sample_seconds = as_int(getfirst(form, "sample_seconds", "30"), 30)
            # No stdbuf prefix—PTY will handle line buffering
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
                try:
                    _parsed_vod = urlparse(vod_url)
                    _netloc = _parsed_vod.netloc.lower().split(":")[0]
                except Exception:
                    raise ValueError("Invalid VOD URL.")
                if _parsed_vod.scheme not in ("http", "https"):
                    raise ValueError("VOD URL must use https://.")
                if _netloc not in ("www.twitch.tv", "twitch.tv"):
                    raise ValueError("VOD URL must be a twitch.tv URL.")
                cmd += ["--vod-url", vod_url, "--sample-seconds", str(sample_seconds)]
                if start_time:
                    if not HHMMSS_RE.fullmatch(start_time):
                        raise ValueError("start-time must be HH:MM:SS.")
                    cmd += ["--start-time", start_time]
                return cmd

            raise ValueError("Invalid mode.")

        def stream_analysis(form, sse: bool = False):
            # Headers already sent by main()
            if not os.path.exists(ANALYZER):
                emit(f"Error: analyzer not found: {ANALYZER}\n", sse=sse)
                return

            try:
                cmd = build_command(form)
            except ValueError as exc:
                emit(f"Input error: {exc}\n", sse=sse)
                return

            if sse:
                # Initial padding often helps defeat intermediary buffering.
                sys.stdout.write(":" + (" " * 4096) + "\n\n")
                sys.stdout.flush()
                emit("Starting...\n", sse=True)
            emit("Running command:\n", sse=sse)
            emit("  " + " ".join(cmd) + "\n", sse=sse)

            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env.setdefault("LANG", "C.UTF-8")
                env.setdefault("LC_ALL", "C.UTF-8")
                proc = subprocess.Popen(
                    cmd,
                    cwd=BASE_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:
                emit(f"Failed to start process: {exc}\n", sse=sse)
                return

            q: Queue = Queue()

            def pump_output(pipe, queue: Queue):
                try:
                    for line in iter(pipe.readline, ""):
                        queue.put(line)
                finally:
                    queue.put(None)

            try:
                assert proc.stdout is not None
                t = Thread(target=pump_output, args=(proc.stdout, q), daemon=True)
                t.start()

                done = False
                while not done:
                    try:
                        item = q.get(timeout=0.25)
                    except Empty:
                        if proc.poll() is not None and q.empty():
                            break
                        continue

                    if item is None:
                        done = True
                    else:
                        emit(item, sse=sse)
            finally:
                rc = proc.wait()
                if rc == 0:
                    emit("Done.\n", sse=sse)
                else:
                    emit(f"\n[exit code: {rc}]\n", sse=sse)

        def render_page():
            # Headers already sent by main()
            page = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Twitch Audio Tester</title>
<style>
  :root{
    --bg:#12161d;--panel:#1b2330;--panel2:#222d3d;--text:#dbe7ff;--muted:#9fb0cc;
    --accent:#5c7cff;--accent2:#7f5af0;--border:#2d3a52;
  }
  body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:linear-gradient(180deg,#0f131a,#141b26);color:var(--text);}
    .wrap{display:grid;grid-template-columns:1fr minmax(auto,1000px) 1fr;gap:0;padding:20px;margin:30px 0;}
    .sidebar{grid-column:1;display:flex;flex-direction:column;align-items:flex-end;gap:0;padding-right:40px;}
    .main{grid-column:2;}
        .img-stack{position:relative;width:300px;overflow:hidden;}
        .img-stack video{position:absolute;top:0;left:50%;width:345px;height:100%;transform:translateX(-50%);object-fit:cover;object-position:top center;}
        .img-stack img{position:relative;z-index:1;width:300px;display:block;margin-top:20px;}
        .sidebar > img{display:block;width:300px;max-width:300px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;box-shadow:0 10px 35px rgba(0,0,0,.35);}
  h1{margin:.2rem 0 1rem;display:flex;align-items:center;gap:.6rem;}
  .logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:inline-block;}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
  .form{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:12px;}
  label{display:block;font-size:.86rem;color:var(--muted);margin:.5rem 0 .2rem;}
  input{width:100%;box-sizing:border-box;padding:9px;border-radius:8px;border:1px solid #3a4a67;background:#111824;color:var(--text);}
  button{margin-top:.8rem;background:var(--accent);color:white;border:none;border-radius:8px;padding:10px 14px;cursor:pointer}
  button:hover{filter:brightness(1.08)}
  .out{margin-top:16px}
  pre{background:#0f141d;border:1px solid var(--border);border-radius:10px;padding:12px;min-height:320px;max-height:60vh;overflow:auto;white-space:pre-wrap}
  .hint{font-size:.82rem;color:var(--muted)}
  @media (max-width:860px){.grid{grid-template-columns:1fr}}
        @media (max-width:1100px){.wrap{grid-template-columns:1fr;}.sidebar{grid-column:1;align-items:center;padding-right:0;padding-bottom:20px;}.main{grid-column:1;}}
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
      <h1><span class="logo"></span> Twitch Audio Test</h1>
      <div class="hint">Runs Chirpaudio Twitch Audio Test for a live stream or VOD.</div>

      <div class="grid">
        <form id="liveForm" class="form" method="post" action="">
          <h3>Live Channel</h3>
          <input type="hidden" name="mode" value="live" />
          <label>Channel</label>
          <input name="channel" value="willowstephens" required />
          <label>Sample Seconds (1-360)</label>
          <input name="sample_seconds" type="number" min="1" max="360" value="30" />
          <button type="submit">Run Live Test</button>
        </form>

        <form id="vodForm" class="form" method="post" action="">
          <h3>VOD</h3>
          <input type="hidden" name="mode" value="vod" />
          <label>VOD URL</label>
          <input name="vod_url" placeholder="https://www.twitch.tv/videos/123456789" />
          <label>Start Time (HH:MM:SS)</label>
          <input name="start_time" placeholder="00:00:00" />
          <label>Sample Seconds (1-360)</label>
          <input name="sample_seconds" type="number" min="1" max="360" value="30" />
          <button type="submit">Run VOD Test</button>
        </form>
      </div>

      <div class="out">
        <h3>Output</h3>
        <pre id="output">Ready.</pre>
                <noscript><p class="hint">JavaScript is required for live streaming output in this view.</p></noscript>
      </div>
    </div>
        </div>
  </div>

<script>
let currentSource = null;

async function runForm(form){
  const out = document.getElementById('output');
    out.textContent = '';
    const qs = new URLSearchParams(new FormData(form));
    qs.append('stream', '1');

    if (currentSource){
        currentSource.close();
        currentSource = null;
    }

    const source = new EventSource(window.location.pathname + '?' + qs.toString());
    currentSource = source;

    source.onmessage = (evt) => {
        out.textContent += evt.data + '\\n';
    out.scrollTop = out.scrollHeight;
    };

    source.onerror = () => {
        source.close();
        if (currentSource === source){
            currentSource = null;
        }
    };
}
document.getElementById('liveForm').addEventListener('submit', e => { e.preventDefault(); runForm(e.target); });
document.getElementById('vodForm').addEventListener('submit', e => { e.preventDefault(); runForm(e.target); });
</script>
</body>
</html>'''
            sys.stdout.write(page)
            sys.stdout.flush()

        # Main logic
        method = os.environ.get("REQUEST_METHOD", "GET").upper()
        form = parse_form_data()
        ajax = (getfirst(form, "ajax") or "").strip()
        stream = (getfirst(form, "stream") or "").strip()

        if method == "GET" and stream == "1":
            send_headers("text/event-stream; charset=utf-8", sse=True)
            stream_analysis(form, sse=True)
        elif method == "POST" and ajax == "1":
            send_headers("text/plain; charset=utf-8")
            stream_analysis(form, sse=False)
        else:
            send_headers("text/html; charset=utf-8")
            render_page()

    except Exception as e:
        try:
            sys.stdout.write("Content-Type: text/plain; charset=utf-8\r\n")
            sys.stdout.write("Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n")
            sys.stdout.write("Pragma: no-cache\r\n")
            sys.stdout.write("Expires: 0\r\n\r\n")
        except Exception:
            pass
        sys.stdout.write("CGI Script Error:\n\n")
        sys.stdout.write(str(e) + "\n\n")
        sys.stdout.write(traceback.format_exc())
        sys.stdout.flush()


if __name__ == "__main__":
    safe_main()