#!/usr/bin/env python3
"""
shorts_bot.py - Split a long video into 1-minute vertical clips and
auto-upload them to YouTube Shorts and Instagram Reels (all free tools).

SETUP
  1. Install FFmpeg (https://ffmpeg.org) and make sure `ffmpeg` works in a terminal.
  2. pip install google-api-python-client google-auth-oauthlib requests
  3. YouTube: put your OAuth file from Google Cloud Console next to this script
     as `client_secret.json` (Desktop app type, YouTube Data API v3 enabled).
  4. Instagram: create `config.json` next to this script:
        {
          "ig_user_id": "1784xxxxxxxxxxxx",
          "ig_access_token": "EAAG...long-lived token...",
          "graph_version": "v21.0"
        }

USAGE
  # Split + upload everything that fits today's limits
  python shorts_bot.py my_video.mp4 --title "My Video Title"

  # Burn "Part N" + title text onto each clip (like your screenshot)
  python shorts_bot.py my_video.mp4 --title "Zombie Outbreak: Trapped in Rooftop" --overlay

  # Blurred background (default) + auto captions (needs: pip install faster-whisper)
  python shorts_bot.py my_video.mp4 --title "My Title" --overlay --captions

  # Upload at fixed times (YouTube schedules them, Instagram posts when due) + link
  python shorts_bot.py my_video.mp4 --title "My Title" --use-existing --loop \\
      --post-times "09:00,18:00" --link "https://youtu.be/XXXX"
  # Preview without uploading anything:  add --dry-run

  # Only split (no upload)
  python shorts_bot.py my_video.mp4 --split-only

  # Only YouTube, 6 per run (quota-safe), run again tomorrow to continue
  python shorts_bot.py my_video.mp4 --title "My Video" --platforms youtube

  # Automate daily: cron (Linux/Mac) or Task Scheduler (Windows) running the
  # same command once per day. Already-uploaded clips are skipped (state.json).
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
VERSION = "v8-login-link"
STATE_FILE = BASE_DIR / "state.json"
CONFIG_FILE = BASE_DIR / "config.json"
CLIENT_SECRET = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / "token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


# --------------------------------------------------------------------------
# State (remembers which clips are already uploaded)
# --------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Step 1: split with FFmpeg
# --------------------------------------------------------------------------
def split_video(video, out_dir, seconds=60, crop=True):
    out_dir.mkdir(parents=True, exist_ok=True)
    if list(out_dir.glob("clip_*.mp4")):
        print(f"[split] Clips already exist in {out_dir}, skipping split.")
        return
    cmd = ["ffmpeg", "-y", "-i", str(video)]
    if crop:  # center-crop landscape to 9:16, scale to 1080x1920
        cmd += ["-vf", "crop='min(iw,ih*9/16)':ih,scale=1080:1920"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-force_key_frames", f"expr:gte(t,n_forced*{seconds})",
        "-f", "segment", "-segment_time", str(seconds),
        "-reset_timestamps", "1",
        str(out_dir / "clip_%03d.mp4"),
    ]
    print("[split] Running FFmpeg (this can take a few minutes)...")
    subprocess.run(cmd, check=True)
    print(f"[split] Done: {len(list(out_dir.glob('clip_*.mp4')))} clips in {out_dir}")


def probe(video):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-show_entries", "format=duration",
        "-of", "json", str(video)])
    d = json.loads(out)
    return d["streams"][0]["width"], d["streams"][0]["height"], float(d["format"]["duration"])


def check_drawtext():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    if "drawtext" not in out:
        sys.exit("[error] Your FFmpeg has no 'drawtext' filter (needs libfreetype). "
                 "Install a full build, e.g. gyan.dev 'full' build on Windows or "
                 "'brew install ffmpeg' on Mac, then try again.")
    print("[info] FFmpeg drawtext filter: available")


def find_font(user_font=None):
    if user_font:
        return user_font
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    sys.exit("No font found. Pass one with --font path/to/font.ttf")


def ff_path(p):
    # escape a path for use inside an FFmpeg filter (needed for C:/ on Windows)
    return str(p).replace("\\", "/").replace(":", "\\:")


def detect_content_box(video, dur):
    """Find the real picture area (ignores black bars baked into the video)."""
    boxes = []
    for k in range(8):
        t = dur * (k + 0.5) / 8
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", f"{t:.2f}", "-t", "2", "-i", str(video),
             "-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if m:
            cw, ch, cx, cy = map(int, m[-1])
            boxes.append((cx, cy, cx + cw, cy + ch))
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    x0 -= x0 % 2
    y0 -= y0 % 2
    cw, ch = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if cw < 64 or ch < 64:
        return None
    return cw, ch, x0, y0


# ---------------------------- captions (Whisper) ----------------------------
def transcribe(video, model_size, lang):
    """Transcribe once with faster-whisper (free, local). Result is cached."""
    cache = BASE_DIR / "captions" / f"{video.stem}.{model_size}.{lang or 'auto'}.json"
    if cache.exists():
        print(f"[captions] Using cached transcript: {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("[error] Captions need faster-whisper. Run:  pip install faster-whisper")
    print(f"[captions] Loading Whisper '{model_size}' model (first run downloads it)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(video), language=lang, word_timestamps=True,
                                      vad_filter=True)
    print(f"[captions] Language: {info.language}. Transcribing {int(info.duration // 60)} min "
          "of audio (this can take several minutes on CPU)...")
    words, last = [], 0
    for seg in segments:
        for wd in (seg.words or []):
            txt = wd.word.strip()
            if txt:
                words.append({"w": txt, "start": wd.start, "end": wd.end})
        if seg.end - last >= 120:
            print(f"[captions]   ...{int(seg.end // 60)} min done")
            last = seg.end
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(words), encoding="utf-8")
    print(f"[captions] Transcript saved: {cache} ({len(words)} words)")
    return words


def make_chunks(words, max_chars=38, max_dur=3.4, gap=0.7):
    """Group words into short, readable caption blocks."""
    chunks, cur = [], []

    def flush():
        if cur:
            chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                           "text": " ".join(x["w"] for x in cur)})

    for w in words:
        if cur:
            length = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            if (w["start"] - cur[-1]["end"] > gap or length > max_chars
                    or w["end"] - cur[0]["start"] > max_dur):
                flush()
                cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            flush()
            cur = []
    flush()
    return chunks


def wrap2(text):
    """Split a caption into at most 2 balanced lines (ASS line break = \\N)."""
    text = text.replace("{", "").replace("}", "")
    if len(text) <= 22:
        return text
    ws = text.split()
    best, best_diff = 1, 10 ** 6
    for i in range(1, len(ws)):
        diff = abs(len(" ".join(ws[:i])) - len(" ".join(ws[i:])))
        if diff < best_diff:
            best, best_diff = i, diff
    return " ".join(ws[:best]) + "\\N" + " ".join(ws[best:])


def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def font_family(path):
    """Read family/style name from a TTF/OTF so libass can find the font we copied."""
    try:
        data = Path(path).read_bytes()
        num = struct.unpack(">H", data[4:6])[0]
        off = None
        for i in range(num):
            tag, _, o, _ = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            if tag == b"name":
                off = o
                break
        _, count, str_off = struct.unpack(">HHH", data[off:off + 6])
        names = {}
        for i in range(count):
            pid, _, _, nid, ln, so = struct.unpack(">6H", data[off + 6 + 12 * i:off + 18 + 12 * i])
            raw = data[off + str_off + so: off + str_off + so + ln]
            s = raw.decode("utf-16-be") if pid in (0, 3) else raw.decode("latin-1")
            if nid not in names or pid == 3:
                names[nid] = s
        return names.get(1, "Arial"), names.get(2, "Bold")
    except Exception:
        return "Arial", "Bold"


def build_ass(events, family, bold, size, margin_v):
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,{bold},0,0,0,"
        f"100,100,0,0,1,4,1,8,70,70,{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{wrap2(t)}"
             for s, e, t in events]
    return head + "\n".join(lines) + "\n"


def clip_events(chunks, t0, t1):
    ev = []
    for c in chunks or []:
        if c["end"] <= t0 or c["start"] >= t1:
            continue
        s, e = max(c["start"], t0) - t0, min(c["end"], t1) - t0
        if e - s >= 0.15:
            ev.append((s, e, c["text"]))
    return ev


def split_with_overlay(video, out_dir, seconds, title, font, args, chunks=None):
    """Vertical 1080x1920 clips: blurred background + sharp picture + 'Part N' + title (+ captions)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if list(out_dir.glob("clip_*.mp4")):
        print(f"[split] Clips already exist in {out_dir}, skipping split (use --resplit to redo).")
        return
    w, h, dur = probe(video)
    total = math.ceil(dur / seconds)

    # 1) real picture area (removes black bars that are baked into the video)
    if args.crop_box:
        cw, ch, cx, cy = [int(v) for v in args.crop_box.split(":")]
    elif args.no_autocrop:
        cw, ch, cx, cy = w, h, 0, 0
    else:
        print("[split] Detecting picture area ...")
        box = detect_content_box(video, dur)
        cw, ch, cx, cy = box if box else (w, h, 0, 0)
    print(f"[split] Picture area: {cw}x{ch} at x={cx}, y={cy}  (source {w}x{h})")

    scale = min(1080 / cw, 1920 / ch)
    fg_w, fg_h = round(cw * scale / 2) * 2, round(ch * scale / 2) * 2
    fx, fy = (1080 - fg_w) // 2, (1920 - fg_h) // 2
    top = args.content_top if args.content_top is not None else fy

    # 2) text positions (title block sits just above the picture)
    lines = textwrap.fill(title, width=26).split("\n")
    title_size, part_size = 52, 96
    line_h = int(title_size * 1.3)
    title_y = top - 50 - len(lines) * line_h
    part_y = title_y - part_size - 30
    if part_y < 40:
        shift = 40 - part_y
        part_y += shift
        title_y += shift
        print("[split] Warning: picture is tall/title is long, text shifted down to stay on screen.")
    ff = ff_path(font)

    # 3) background
    crop = f"crop={cw}:{ch}:{cx}:{cy}"
    if args.bg == "blur":
        base = (f"[0:v]{crop},split=2[a][b];"
                "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                "gblur=sigma=12,scale=1080:1920,eq=brightness=-0.08[bg];"
                f"[b]scale={fg_w}:{fg_h}[fg];[bg][fg]overlay={fx}:{fy}[base];")
    else:
        base = (f"[0:v]{crop},scale={fg_w}:{fg_h},"
                f"pad=1080:1920:{fx}:{fy}:black[base];")

    family, style = font_family(font)
    bold = -1 if "bold" in style.lower() else 0
    cap_margin = args.caption_y if args.caption_y is not None else fy + fg_h + 130

    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copy(font, tmp / ("capfont" + Path(font).suffix))
        for n, line in enumerate(lines):
            (tmp / f"line{n}.txt").write_text(line, encoding="utf-8")
        video_abs = str(Path(video).resolve())
        for i in range(total):
            (tmp / "part.txt").write_text(f"Part {i + 1}", encoding="utf-8")
            vf = (f"drawtext=fontfile='{ff}':textfile=part.txt:fontcolor={args.part_color}"
                  f":fontsize={part_size}:borderw=3:bordercolor=black"
                  f":x=(w-text_w)/2:y={part_y}")
            for n in range(len(lines)):
                vf += (f",drawtext=fontfile='{ff}':textfile=line{n}.txt:fontcolor={args.title_color}"
                       f":fontsize={title_size}:borderw=3:bordercolor=black"
                       f":x=(w-text_w)/2:y={title_y + n * line_h}")
            n_caps = 0
            if chunks is not None:
                ev = clip_events(chunks, i * seconds, (i + 1) * seconds)
                n_caps = len(ev)
                (tmp / "cap.ass").write_text(
                    build_ass(ev, family, bold, args.caption_size, cap_margin), encoding="utf-8")
                if ev:
                    vf += ",subtitles=cap.ass:fontsdir=."
            graph = base + f"[base]{vf}[v]"
            out_file = str((out_dir / f"clip_{i:03d}.mp4").resolve())
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
                   "-ss", str(i * seconds), "-i", video_abs, "-t", str(seconds),
                   "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                   "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                   "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", out_file]
            if i == 0:
                print(f"[split] Font: {font} (caption font family: {family})")
            print(f"[split] Rendering Part {i + 1}/{total}"
                  + (f" with {n_caps} captions" if chunks is not None else "") + " ...")
            subprocess.run(cmd, check=True, cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[split] Done: {total} clips in {out_dir}")


# --------------------------------------------------------------------------
# Step 2: YouTube Shorts upload (YouTube Data API v3)
# --------------------------------------------------------------------------
def youtube_service(url_callback=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), YT_SCOPES)

            # Do NOT auto-open the default browser (it may not be the one where your Google
            # account is signed in). Show the login link instead, so you can open it in
            # Chrome (or any browser) on this computer.
            class _Prompt(str):
                def format(self, *args, **kwargs):
                    url = kwargs.get("url", "")
                    if url_callback:
                        url_callback(url)
                    return ("\n[youtube] Open this link in the browser where your Google account "
                            f"is signed in (for example Chrome):\n\n{url}\n")

            creds = flow.run_local_server(port=0, open_browser=False,
                                          authorization_prompt_message=_Prompt("login"))
        TOKEN_FILE.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload_youtube(yt, clip, title, description, tags, privacy, publish_at=None):
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    if publish_at:  # scheduled publishing: upload as private, YouTube flips it public at that time
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    media = MediaFileUpload(str(clip), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


# --------------------------------------------------------------------------
# Step 3: Instagram Reels upload (Instagram Graph API, resumable upload,
# so no public hosting of the video is needed)
# --------------------------------------------------------------------------
def upload_instagram(clip, caption, cfg):
    token = cfg["ig_access_token"]
    user_id = cfg["ig_user_id"]
    ver = cfg.get("graph_version", "v21.0")
    base = f"https://graph.facebook.com/{ver}"

    # 1) create a resumable container
    r = requests.post(
        f"{base}/{user_id}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=60,
    )
    _check(r)
    container_id = r.json()["id"]
    upload_uri = r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{ver}/{container_id}"

    # 2) upload the file bytes
    size = os.path.getsize(clip)
    with open(clip, "rb") as f:
        up = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
            },
            data=f,
            timeout=600,
        )
    _check(up)

    # 3) wait until Instagram finishes processing
    for _ in range(40):  # up to ~6-7 minutes
        s = requests.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=60,
        )
        _check(s)
        code = s.json().get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {s.json()}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")

    # 4) publish
    p = requests.post(
        f"{base}/{user_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    _check(p)
    return p.json()["id"]


def _check(resp):
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def make_slots(clips, post_times, start_date=None):
    """Assign one posting time (stored as UTC) to every clip, e.g. post_times='09:00,18:00'."""
    times = sorted((int(t.split(":")[0]), int(t.split(":")[1])) for t in post_times.split(","))
    now = datetime.now()
    day = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else now.date()
    out = []
    while len(out) < len(clips):
        for hh, mm in times:
            dt = datetime(day.year, day.month, day.day, hh, mm)
            if dt > now + timedelta(minutes=30):  # first slot must be in the future
                out.append(dt)
                if len(out) == len(clips):
                    break
        day += timedelta(days=1)
    return {c.name: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for c, dt in zip(clips, out)}


def slot_to_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_local(dt):
    return dt.astimezone().strftime("%a %d %b %Y, %H:%M")


def main():
    ap = argparse.ArgumentParser(description="Split a video and auto-post Shorts/Reels")
    ap.add_argument("video", help="Path to your long video")
    ap.add_argument("--title", default=None, help="Base title (default: file name)")
    ap.add_argument("--description", default="", help="Extra description text")
    ap.add_argument("--hashtags", default="#shorts #reels #viral", help="Hashtags for captions")
    ap.add_argument("--clip-seconds", type=int, default=60)
    ap.add_argument("--no-crop", action="store_true", help="Video is already vertical")
    ap.add_argument("--split-only", action="store_true")
    ap.add_argument("--overlay", action="store_true",
                    help="Burn 'Part N' (red) + title (white) onto each clip, video fitted on black 9:16 canvas")
    ap.add_argument("--overlay-title", default=None, help="Text shown under 'Part N' (default: --title)")
    ap.add_argument("--bg", choices=["blur", "black"], default="blur",
                    help="Background behind the picture in --overlay mode (default: blur)")
    ap.add_argument("--captions", action="store_true",
                    help="Auto-generate captions with Whisper (free, local) and burn them in")
    ap.add_argument("--whisper-model", default="small",
                    help="tiny / base / small / medium (bigger = more accurate, slower)")
    ap.add_argument("--lang", default=None, help="Audio language code, e.g. en, hi (default: auto-detect)")
    ap.add_argument("--caption-size", type=int, default=58)
    ap.add_argument("--caption-y", type=int, default=None,
                    help="Pixels from top where captions start (default: just below the picture)")
    ap.add_argument("--part-color", default="red", help="Color of 'Part N' text")
    ap.add_argument("--title-color", default="white", help="Color of the title text")
    ap.add_argument("--crop-box", default=None,
                    help="Manual picture area W:H:X:Y of the source (default: auto-detect)")
    ap.add_argument("--no-autocrop", action="store_true", help="Don't remove baked-in black bars")
    ap.add_argument("--content-top", type=int, default=None,
                    help="Y pixel (on 1920 tall canvas) where your picture starts; text is placed above it")
    ap.add_argument("--font", default=None, help="Path to a .ttf font for the overlay text")
    ap.add_argument("--resplit", action="store_true", help="Delete existing clips and cut again")
    ap.add_argument("--platforms", nargs="+", default=["youtube", "instagram"],
                    choices=["youtube", "instagram"])
    ap.add_argument("--yt-per-run", type=int, default=50,
                    help="YouTube uploads per run (free daily quota is about 100 uploads)")
    ap.add_argument("--ig-per-run", type=int, default=25,
                    help="Instagram uploads per run (API limit is 100/day)")
    ap.add_argument("--yt-privacy", default="public", choices=["public", "private", "unlisted"])
    ap.add_argument("--gap-seconds", type=int, default=30, help="Pause between uploads")
    ap.add_argument("--use-existing", action="store_true",
                    help="Use the clips already in the clips folder as they are (never re-cut)")
    ap.add_argument("--link", default=None, help="Link to your full video, added to every post")
    ap.add_argument("--link-text", default="Watch the full video:", help="Text shown before the link")
    ap.add_argument("--ig-bio-link", action="store_true",
                    help="On Instagram write 'link in bio' instead of the URL (links in captions are not clickable)")
    ap.add_argument("--post-times", default=None,
                    help="Daily posting times, e.g. '09:00,18:00' (your local time). One clip per slot.")
    ap.add_argument("--start-date", default=None, help="First posting day YYYY-MM-DD (default: today)")
    ap.add_argument("--reschedule", action="store_true", help="Recalculate posting slots")
    ap.add_argument("--loop", action="store_true",
                    help="Keep running and post each clip when its slot is due, until all are done")
    ap.add_argument("--check-minutes", type=int, default=10, help="How often --loop checks (minutes)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be uploaded/scheduled without uploading anything")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"Video not found: {video}")

    if args.captions:
        args.overlay = True  # captions use the overlay layout
    clips_dir = BASE_DIR / "clips" / video.stem
    print(f"[info] shorts_bot {VERSION}")
    print(f"[info] Script file : {Path(__file__).resolve()}")
    print(f"[info] Overlay mode: {'ON' if args.overlay else 'OFF (no text will be drawn!)'}")
    print(f"[info] Clips folder: {clips_dir.resolve()}")
    if args.overlay and not args.use_existing:
        check_drawtext()
    if args.captions:
        args.overlay = True  # captions use the overlay layout
    if args.use_existing:
        if not list(clips_dir.glob("clip_*.mp4")):
            sys.exit(f"[error] --use-existing but no clips found in {clips_dir}")
        print("[split] Using the existing clips as they are.")
    else:
        overlay_text = args.overlay_title or args.title or video.stem
        if args.overlay:
            mode = "|".join(str(x) for x in [
                "overlay", overlay_text, args.clip_seconds, args.content_top, args.bg,
                args.captions, args.whisper_model, args.lang, args.caption_size, args.caption_y,
                args.part_color, args.title_color, args.crop_box, args.no_autocrop, "v6"])
        else:
            mode = f"plain|crop={not args.no_crop}|{args.clip_seconds}"
        mode_file = clips_dir / ".mode"
        if clips_dir.exists() and list(clips_dir.glob("clip_*.mp4")):
            old_mode = mode_file.read_text(encoding="utf-8") if mode_file.exists() else None
            if args.resplit or old_mode != mode:
                print("[split] Existing clips were made with different settings, cutting again.")
                print("[split] NOTE: clips already uploaded are remembered in state.json. "
                      "Delete state.json if you want the new clips uploaded again.")
                shutil.rmtree(clips_dir)
        if args.overlay:
            chunks = None
            if args.captions and not list(clips_dir.glob("clip_*.mp4")):
                chunks = make_chunks(transcribe(video, args.whisper_model, args.lang))
            split_with_overlay(video, clips_dir, args.clip_seconds, overlay_text,
                               find_font(args.font), args, chunks)
        else:
            split_video(video, clips_dir, args.clip_seconds, crop=not args.no_crop)
        mode_file.write_text(mode, encoding="utf-8")
        print(f"[split] Finished. Open the clips here: {clips_dir.resolve()}")
    if args.split_only:
        return

    clips = sorted(clips_dir.glob("clip_*.mp4"))
    total = len(clips)
    base_title = args.title or video.stem
    state = load_state()
    key = video.stem
    state.setdefault(key, {})

    # ---- link to the full video ----
    link_line = f"{args.link_text} {args.link}" if args.link else ""
    if args.link and args.ig_bio_link:
        ig_link_line = f"{args.link_text.rstrip(':')}: link in bio"
    else:
        ig_link_line = link_line

    # ---- posting schedule (one time slot per clip, stored so it stays stable) ----
    slots = {}
    if args.post_times:
        slots = state[key].get("_slots", {})
        if args.reschedule or set(slots) != {c.name for c in clips}:
            slots = make_slots(clips, args.post_times, args.start_date)
            state[key]["_slots"] = slots
            if not args.dry_run:
                save_state(state)
        print("[schedule] Posting slots (your local time):")
        names = [c.name for c in clips]
        for n in names[:4] + (["..."] if len(names) > 8 else []) + names[-3:] if len(names) > 8 else names:
            if n == "...":
                print("[schedule]   ...")
            else:
                print(f"[schedule]   {n}: {fmt_local(slot_to_dt(slots[n]))}")

    yt_on = "youtube" in args.platforms
    ig_on = "instagram" in args.platforms
    yt = ig_cfg = None
    if yt_on and not args.dry_run:
        yt = youtube_service()
    if ig_on and not args.dry_run:
        if not CONFIG_FILE.exists():
            sys.exit("config.json missing (needed for Instagram). See script header.")
        ig_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    yt_day = {"date": None, "n": 0}
    yt_block = {"until": None}

    def run_pass():
        now = datetime.now(timezone.utc)
        today = datetime.now().date()
        if yt_day["date"] != today:
            yt_day["date"], yt_day["n"] = today, 0
        yt_done = ig_done = 0
        for i, clip in enumerate(clips, start=1):
            rec = state[key].setdefault(clip.name, {})
            title = f"{base_title} - Part {i}/{total}"
            slot = slot_to_dt(slots[clip.name]) if clip.name in slots else None

            # ---------------- YouTube ----------------
            yt_open = yt_block["until"] is None or now >= yt_block["until"]
            if yt_on and "youtube" not in rec and yt_day["n"] < args.yt_per_run and yt_open:
                publish_at = slot if slot and slot > now + timedelta(minutes=15) else None
                if slot and not publish_at:
                    print(f"[youtube] Slot for {clip.name} already passed, publishing right away.")
                when = f"scheduled for {fmt_local(publish_at)}" if publish_at else "publishing now"
                desc = "\n\n".join(x for x in [args.description, link_line,
                                               f"{args.hashtags} #Shorts"] if x)
                tags = ["shorts"] + [h.strip("#") for h in args.hashtags.split()]
                if args.dry_run:
                    print(f"[dry-run] YouTube: {clip.name} -> {when} | {title}")
                    yt_day["n"] += 1
                    yt_done += 1
                else:
                    try:
                        print(f"[youtube] Uploading {clip.name} ({when}) ...")
                        rec["youtube"] = upload_youtube(yt, clip, title, desc, tags,
                                                        args.yt_privacy, publish_at)
                        yt_day["n"] += 1
                        yt_done += 1
                        save_state(state)
                        print(f"[youtube]   -> https://youtube.com/shorts/{rec['youtube']}")
                        time.sleep(args.gap_seconds)
                    except Exception as e:
                        print(f"[youtube] FAILED on {clip.name}: {e}")
                        if "quota" in str(e).lower():
                            print("[youtube] Daily quota reached. Will retry later.")
                            yt_block["until"] = now + timedelta(hours=6)

            # ---------------- Instagram (posts only when its slot is due) ----------------
            ig_due = slot is None or slot <= now
            if ig_on and "instagram" not in rec and ig_done < args.ig_per_run and ig_due:
                caption = "\n\n".join(x for x in [title, args.description, ig_link_line,
                                                  args.hashtags] if x)
                if args.dry_run:
                    print(f"[dry-run] Instagram: {clip.name} -> posting now | {title}")
                    ig_done += 1
                else:
                    try:
                        print(f"[instagram] Uploading {clip.name} ...")
                        rec["instagram"] = upload_instagram(clip, caption, ig_cfg)
                        ig_done += 1
                        save_state(state)
                        print(f"[instagram]   -> media id {rec['instagram']}")
                        time.sleep(args.gap_seconds)
                    except Exception as e:
                        print(f"[instagram] FAILED on {clip.name}: {e}")
        return yt_done, ig_done

    def remaining():
        out = {}
        if yt_on:
            out["YouTube"] = sum("youtube" not in state[key].get(c.name, {}) for c in clips)
        if ig_on:
            out["Instagram"] = sum("instagram" not in state[key].get(c.name, {}) for c in clips)
        return out

    while True:
        y, g = run_pass()
        rem = remaining()
        print(f"\nThis pass: YouTube {y}, Instagram {g}.  Remaining: "
              + ", ".join(f"{k} {v}" for k, v in rem.items()))
        if args.dry_run or not args.loop or not any(rem.values()):
            break
        print(f"[loop] Checking again in {args.check_minutes} min (press Ctrl+C to stop)...")
        time.sleep(args.check_minutes * 60)
    if args.loop and not any(remaining().values()):
        print("[loop] Everything is uploaded. Done.")


if __name__ == "__main__":
    main()