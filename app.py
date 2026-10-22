#!/usr/bin/env python3
"""
Shorts Uploader - local web app (backend + page) that posts your clips to
YouTube Shorts on a timer. No command line options needed.

RUN
    python app.py              -> opens http://127.0.0.1:8765 in your browser

NEEDS
    pip install flask
    (plus what shorts_bot.py already needs: google-api-python-client google-auth-oauthlib requests)
    Keep app.py, index.html and shorts_bot.py in the SAME folder, with
    client_secret.json next to them and your clips in  clips/<name>/clip_000.mp4 ...
    (shorts_bot.py creates those clips; use --split-only to just cut them).

NOTE: the app only posts while it is running. Closing the window/terminal pauses
      everything; start it again later and it continues where it stopped.
"""

import argparse
import collections
import json
import logging
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from flask import Flask, abort, jsonify, request, send_from_directory
except ImportError:
    sys.exit("Flask is missing. Run:  pip install flask")

import shorts_bot as sb  # reuses the YouTube login + upload code

BASE = sb.BASE_DIR
CLIPS_DIR = BASE / "clips"
DATA_FILE = BASE / "ui_data.json"
DAILY_UPLOAD_CAP = 100  # Google's docs: default quota is about 100 uploads per day (check your Cloud Console > Quotas)

app = Flask(__name__)
LOCK = threading.Lock()

DEFAULTS = {
    "title": "",
    "add_part": True,
    "description": "",
    "tags": "",
    "link": "",
    "link_text": "Watch the full video:",
    "privacy": "public",
    "mode": "live",          # live = upload at the slot time | scheduled = upload ahead, YouTube publishes later
    "interval_min": 360,
    "start": None,
}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # treat naive time as local time
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_str(dt):
    return dt.astimezone().strftime("%a %d %b, %H:%M")


def load_data():
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            d.setdefault("settings", {})
            d.setdefault("uploads", [])
            return d
        except Exception:
            pass
    return {"settings": {}, "uploads": []}


def save_data(d):
    DATA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def project_clips(name):
    d = CLIPS_DIR / Path(name).name  # .name blocks path tricks like ../
    if not d.is_dir():
        return d, []
    return d, sorted(d.glob("clip_*.mp4"))


def clean_cfg(raw):
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in (raw or {}):
            cfg[k] = raw[k]
    try:
        cfg["interval_min"] = max(1, int(cfg["interval_min"]))
    except Exception:
        cfg["interval_min"] = DEFAULTS["interval_min"]
    if cfg["mode"] not in ("live", "scheduled"):
        cfg["mode"] = "live"
    if cfg["privacy"] not in ("public", "unlisted", "private"):
        cfg["privacy"] = "public"
    cfg["add_part"] = bool(cfg["add_part"])
    for k in ("title", "description", "tags", "link", "link_text"):
        cfg[k] = str(cfg[k] or "")
    return cfg


def save_settings(project, cfg):
    with LOCK:
        data = load_data()
        old = data["settings"].get(project, {})
        data["settings"][project] = {**clean_cfg(cfg), "slots": old.get("slots", {})}
        save_data(data)


def auth_info():
    if not sb.CLIENT_SECRET.exists():
        return "no_secret", "client_secret.json not found next to app.py"
    if AUTH["state"] == "working":
        return "working", AUTH["msg"]
    if AUTH["state"] == "error":
        return "error", AUTH["msg"]
    if sb.TOKEN_FILE.exists():
        return "connected", ""
    return "not_connected", ""


AUTH = {"state": "idle", "msg": "", "url": ""}


def _on_url(url):
    """Called by the login code with the Google link - the page shows it to the user."""
    AUTH.update(state="working", url=url,
                msg="Open the login link below in the browser where your Google account is signed in.")


def _connect():
    AUTH.update(state="working", msg="Preparing the Google login link...", url="")
    try:
        sb.youtube_service(url_callback=_on_url)
        AUTH.update(state="ok", msg="", url="")
    except Exception as e:
        AUTH.update(state="error", msg=str(e)[:300], url="")


# ----------------------------------------------------------------------------
# background worker
# ----------------------------------------------------------------------------
class Worker:
    def __init__(self):
        self.thread = None
        self.stop_evt = threading.Event()
        self.log = collections.deque(maxlen=300)
        self.phase = "Idle"
        self.next_time = None
        self.project = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def say(self, msg):
        self.log.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")

    def start(self, project, raw_cfg):
        if self.running:
            return False, "Already running"
        d, clips = project_clips(project)
        if not clips:
            return False, "No clips found in that folder"
        cfg = clean_cfg(raw_cfg)
        state = sb.load_state().get(d.name, {})
        pending = [c for c in clips if "youtube" not in state.get(c.name, {})]
        if not pending:
            return False, "All clips of this project are already uploaded"

        start = now_utc() + timedelta(minutes=1)
        if cfg["start"]:
            try:
                start = max(parse_iso(cfg["start"]), start)
            except Exception:
                pass
        with LOCK:
            data = load_data()
            slots = dict(data["settings"].get(d.name, {}).get("slots", {}))
            for j, c in enumerate(pending):
                slots[c.name] = iso(start + timedelta(minutes=cfg["interval_min"] * j))
            cfg["start"] = iso(start)
            data["settings"][d.name] = {**cfg, "slots": slots}
            save_data(data)

        self.project = d.name
        self.stop_evt.clear()
        self.log.clear()
        self.phase = "Starting..."
        self.next_time = None
        self.thread = threading.Thread(target=self._run, args=(d.name, cfg, slots), daemon=True)
        self.thread.start()
        return True, ""

    def stop(self):
        if self.running:
            self.stop_evt.set()
            self.phase = "Stopping (finishing the current step)..."

    # -- helpers --------------------------------------------------------------
    def _cap_wait(self):
        """Seconds to wait before another upload is allowed (rolling 24h cap)."""
        with LOCK:
            data = load_data()
        cutoff = now_utc() - timedelta(hours=24)
        recent = sorted(parse_iso(t) for t in data["uploads"] if parse_iso(t) > cutoff)
        if len(recent) >= DAILY_UPLOAD_CAP:
            return max(60, (recent[0] + timedelta(hours=24, minutes=1) - now_utc()).total_seconds())
        return 0

    def _record_upload(self, project, clip, video_id):
        with LOCK:
            st = sb.load_state()
            st.setdefault(project, {}).setdefault(clip.name, {})["youtube"] = video_id
            sb.save_state(st)
            data = load_data()
            cutoff = now_utc() - timedelta(hours=48)
            data["uploads"] = [t for t in data["uploads"] if parse_iso(t) > cutoff] + [iso(now_utc())]
            save_data(data)

    # -- main loop ------------------------------------------------------------
    def _run(self, project, cfg, slots):
        try:
            self.say("Connecting to YouTube (if a login link appears at the top of the page, open it in Chrome)...")
            yt = sb.youtube_service(url_callback=_on_url)
            AUTH.update(state="ok", msg="", url="")
        except Exception as e:
            AUTH.update(state="error", msg=str(e)[:300], url="")
            self.say(f"Could not connect to YouTube: {e}")
            self.phase = "Stopped (YouTube login failed)"
            return

        d, clips = project_clips(project)
        total = len(clips)
        base_title = cfg["title"].strip() or project
        tags = ["shorts"]
        for t in cfg["tags"].split(","):
            t = t.strip().lstrip("#")
            if t and t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
        desc_parts = [cfg["description"].strip()]
        if cfg["link"].strip():
            desc_parts.append(f"{cfg['link_text'].strip()} {cfg['link'].strip()}".strip())
        if "#shorts" not in " ".join(desc_parts).lower():
            desc_parts.append("#Shorts")
        description = "\n\n".join(p for p in desc_parts if p)

        fails = 0
        self.say(f"Started: {cfg['mode']} mode, every {cfg['interval_min']} min.")
        while not self.stop_evt.is_set():
            done = sb.load_state().get(project, {})
            pending = [(i, c) for i, c in enumerate(clips, 1) if "youtube" not in done.get(c.name, {})]
            if not pending:
                self.say("All clips are uploaded. Finished!")
                self.phase, self.next_time = "Finished - all clips uploaded", None
                return
            i, clip = pending[0]
            slot = parse_iso(slots[clip.name]) if clip.name in slots else now_utc()
            now = now_utc()
            publish_at = None

            if cfg["mode"] == "live":
                if slot > now:
                    self.phase = f"Waiting to post Part {i} at {local_str(slot)}"
                    self.next_time = iso(slot)
                    self.stop_evt.wait(min(30, (slot - now).total_seconds()))
                    continue
            else:
                wait = self._cap_wait()
                if wait > 0:
                    self.phase = "Upload limit for today reached - waiting"
                    self.next_time = iso(now + timedelta(seconds=wait))
                    self.stop_evt.wait(min(60, wait))
                    continue
                if slot > now + timedelta(minutes=15):
                    publish_at = slot
                else:
                    self.say(f"Time slot for Part {i} already passed - it will be public right away.")

            title = f"{base_title} - Part {i}/{total}" if cfg["add_part"] else base_title
            self.phase = f"Uploading Part {i}/{total} ({clip.name})..."
            self.next_time = None
            self.say(f"Uploading {clip.name} as: {title[:100]}")
            try:
                vid = sb.upload_youtube(yt, clip, title[:100], description, tags,
                                        cfg["privacy"], publish_at)
            except Exception as e:
                msg = str(e)
                low = msg.lower().replace(" ", "")
                if "quota" in low or "uploadlimit" in low:
                    self.say("YouTube's daily limit was reached. Pausing 1 hour, then trying again.")
                    self.phase = "Paused: YouTube daily limit"
                    self.next_time = iso(now_utc() + timedelta(hours=1))
                    self.stop_evt.wait(3600)
                    continue
                fails += 1
                self.say(f"Upload failed ({fails}/3): {msg[:300]}")
                if fails >= 3:
                    self.phase = "Stopped: repeated upload errors (see log)"
                    return
                self.phase = "Upload failed - retrying in 5 minutes"
                self.stop_evt.wait(300)
                continue
            fails = 0
            self._record_upload(project, clip, vid)
            extra = f" (goes public {local_str(publish_at)})" if publish_at else ""
            self.say(f"Done: https://youtube.com/shorts/{vid}{extra}")

        self.phase = "Stopped"
        self.next_time = None
        self.say("Stopped by you.")


worker = Worker()


# ----------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/clips/<project>/<path:filename>")
def clip_file(project, filename):
    d = CLIPS_DIR / Path(project).name
    if not d.is_dir():
        abort(404)
    return send_from_directory(d, filename)


@app.get("/api/projects")
def api_projects():
    state = sb.load_state()
    out = []
    if CLIPS_DIR.is_dir():
        for d in sorted(CLIPS_DIR.iterdir()):
            if not d.is_dir():
                continue
            clips = list(d.glob("clip_*.mp4"))
            if clips:
                up = sum(1 for c in clips if "youtube" in state.get(d.name, {}).get(c.name, {}))
                out.append({"name": d.name, "clips": len(clips), "uploaded": up})
    return jsonify(out)


@app.get("/api/project/<name>")
def api_project(name):
    d, clips = project_clips(name)
    if not clips:
        return jsonify(error="Project not found"), 404
    state = sb.load_state().get(d.name, {})
    saved = load_data()["settings"].get(d.name, {})
    slots = saved.get("slots", {})
    rows, last = [], None
    for i, c in enumerate(clips, 1):
        vid = state.get(c.name, {}).get("youtube")
        rows.append({"name": c.name, "n": i, "youtube": vid, "slot": slots.get(c.name)})
        if vid and slots.get(c.name):
            last = max(last or "", slots[c.name])
    cfg = clean_cfg(saved)
    return jsonify(name=d.name, clips=rows, settings=cfg, last_slot=last)


@app.post("/api/settings/<name>")
def api_settings(name):
    d, clips = project_clips(name)
    if not clips:
        return jsonify(error="Project not found"), 404
    if worker.running and worker.project == d.name:
        return jsonify(ok=True, note="running - settings apply on next start")
    save_settings(d.name, request.get_json(silent=True) or {})
    return jsonify(ok=True)


@app.post("/api/start")
def api_start():
    body = request.get_json(silent=True) or {}
    project = body.get("project", "")
    ok, msg = worker.start(project, body.get("settings") or {})
    if not ok:
        return jsonify(error=msg), 400
    return jsonify(ok=True)


@app.post("/api/stop")
def api_stop():
    worker.stop()
    return jsonify(ok=True)


@app.post("/api/connect")
def api_connect():
    if not sb.CLIENT_SECRET.exists():
        return jsonify(error="client_secret.json not found next to app.py"), 400
    if AUTH["state"] != "working":
        threading.Thread(target=_connect, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/reset/<name>")
def api_reset(name):
    d, clips = project_clips(name)
    if not clips:
        return jsonify(error="Project not found"), 404
    if worker.running:
        return jsonify(error="Stop posting first"), 400
    with LOCK:
        st = sb.load_state()
        for c in clips:
            st.get(d.name, {}).pop(c.name, None)
        sb.save_state(st)
        data = load_data()
        if d.name in data["settings"]:
            data["settings"][d.name]["slots"] = {}
        save_data(data)
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    a, amsg = auth_info()
    return jsonify(running=worker.running, project=worker.project, phase=worker.phase,
                   next_time=worker.next_time, log=list(worker.log), auth=a, auth_msg=amsg,
                   auth_url=AUTH["url"] if a == "working" else "")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Shorts Uploader web app")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    CLIPS_DIR.mkdir(exist_ok=True)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Shorts Uploader is running at {url}")
    print("Keep this window open while posting. Press Ctrl+C to quit.")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)