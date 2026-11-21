#!/usr/bin/env python3
"""Cricket clip bot (separate from the recorder).
A video sent to the group (with an optional caption) is downloaded from Telegram (up to 2 GB), the score on the screen is read,
and a clip is cut for every 4, 6 and wicket and sent back into the same group as a reply to the video.

Caption words (all optional):  pre 30   wpre 30   post 15     (seconds before a 4/6, before a wicket, after the ball)
"""
import os, sys, re, json, time, glob, shutil, subprocess, threading, queue, urllib.request, urllib.parse, urllib.error, asyncio


def E(k, d=""):
    return os.environ.get(k, d) or d


#!/usr/bin/env python3
"""Cricket clip bot (separate from the recorder).
A video sent to the group (with an optional caption) is downloaded from Telegram (up to 2 GB), the score on the screen is read,
and a clip is cut for every 4, 6 and wicket and sent back into the same group as a reply to the video.

Caption words (all optional):  pre 30   wpre 30   post 15     (seconds before a 4/6, before a wicket, after the ball)
"""
import os, sys, re, json, time, glob, shutil, subprocess, threading, queue, urllib.request, urllib.parse, urllib.error, asyncio


def E(k, d=""):
    return os.environ.get(k, d) or d


TOKEN = E("TG_TOKEN"); CHAT = E("CHAT"); MSG = E("MSG"); FILE_ID = E("FILE_ID"); FILE_SIZE = int(E("FILE_SIZE") or 0)
CAPTION = E("CAPTION"); API_ID = E("API_ID"); API_HASH = E("API_HASH")
TG_API = E("TG_API") or "https://api.telegram.org"
LOCAL_FILE = E("LOCAL_FILE")                      # test only: use this file instead of downloading
OUT = E("OUT_DIR") or "/mnt/rec"
DRY = E("DRY") == "1"
stop = threading.Event()
ARCHIVE = True                                    # files are read with key frames only (fast)
GOFILE_URL = E("GOFILE_URL") or "https://upload.gofile.io/uploadfile"
CLIP_CHAT = E("CLIP_CHAT") or CHAT
CLIP_MAX = int(float(E("CLIP_MAX_MB") or 49) * 1024 * 1024)
CLIP = {"on": True, "pre": 30, "wpre": 30, "post": 15, "after": 2, "maxlen": 180, "box": None, "tries": 0, "next_try": 0.0}
CLIP_LOCK = threading.RLock()
CLIP_EV = []
CLIP_PART = {}
CLIP_ST = {"conf": None, "pend": None, "t_ov": None, "n": 0, "last": {}, "sent": 0}
SC_RE = re.compile(r"(?<![\d.])(\d{1,3})\s*[/\-]\s*(10|\d)(?![\d]*\.\d{2})")
OV_RE = re.compile(r"(?<![\d.])(\d{1,2})\.([0-5])(?![\d.])")
TESS_OK = [None]


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mmss(s):
    s = int(max(0, s or 0)); return "%d:%02d" % (s // 60, s % 60) if s < 3600 else "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def human(b):
    b = float(b)
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return ("%.0f %s" % (b, u)) if u == "B" else ("%.1f %s" % (b, u))
        b /= 1024


def mask(t):
    return str(t).replace(TOKEN, "***") if TOKEN else str(t)


def media_info(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "json", path], capture_output=True, text=True, timeout=60)
        j = json.loads(r.stdout or "{}"); st = (j.get("streams") or [{}])[0]
        return float((j.get("format") or {}).get("duration") or 0), int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0.0, 0, 0


def api(method, fields=None, files=None, timeout=120):
    """Telegram Bot API call (curl handles the file upload)."""
    if not TOKEN or DRY:
        print("[tg]", method, {k: str(v)[:80] for k, v in (fields or {}).items()}, list((files or {}).keys())); return {"ok": True, "result": {}}
    cmd = ["curl", "-sS", "-m", str(timeout), "-X", "POST", "%s/bot%s/%s" % (TG_API, TOKEN, method)]
    for k, v in (fields or {}).items():
        cmd += ["--form-string", "%s=%s" % (k, v)]
    for k, v in (files or {}).items():
        cmd += ["-F", "%s=@%s" % (k, v)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        return json.loads(r.stdout or "{}")
    except Exception as e:
        return {"ok": False, "description": repr(e)[:100]}


def say(text):
    f = {"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if MSG:
        f["reply_to_message_id"] = MSG; f["allow_sending_without_reply"] = "true"
    r = api("sendMessage", f)
    if not r.get("ok", True):
        print("sendMessage failed:", r.get("description"), flush=True)
    return r


def clip_words(t):
    w = (t or "").lower()
    for key in ("pre", "wpre", "post"):
        m = re.search(r"(?<![\w])%s\s*[:=]?\s*(\d{1,3})\b" % key, w)
        if m:
            CLIP[key] = max(5, min(180, int(m.group(1))))
    m = re.search(r"(?<![\w])after\s*[:=]?\s*(\d)\b", w)
    if m:
        CLIP["after"] = max(0, min(6, int(m.group(1))))


def gofile_upload(path):
    for attempt in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "3600", "-F", "file=@" + path, GOFILE_URL], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            d = j.get("data") or {}
            if j.get("status") == "ok":
                return True, d.get("downloadPage") or "", ""
        except Exception:
            pass
        time.sleep(3 + attempt * 3)
    return False, "", "upload failed"


def ensure_tesseract():
    if TESS_OK[0] is not None:
        return TESS_OK[0]
    if not shutil.which("tesseract"):
        print("Installing tesseract (for the cricket clips)...", flush=True)
        for cmd in (["sudo", "-n", "apt-get", "update", "-qq"], ["sudo", "-n", "apt-get", "install", "-y", "-qq", "--no-install-recommends", "tesseract-ocr", "tesseract-ocr-eng"]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=180)
            except Exception:
                pass
    TESS_OK[0] = bool(shutil.which("tesseract"))
    return TESS_OK[0]


def _ocr(png, tsv=False, neg=False):
    cmd = ["nice", "-n", "10", "tesseract", png, "stdout", "--psm", "6" if not tsv else "11"]
    if not tsv:
        cmd += ["-c", "tessedit_char_whitelist=0123456789/-.()"]
    else:
        cmd += ["tsv"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        return r.stdout or ""
    except Exception:
        return ""


def _grab(path, ss, dur, box, full, outdir, width=1200):
    """Frames of the score area: [(t, png)]. full=True: one picture per second, False: key frames only (fast)."""
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        try: os.remove(os.path.join(outdir, f))
        except OSError: pass
    x, y, w, h = box
    vf = "showinfo,"
    if full:
        vf = "fps=1,showinfo,"
    vf += "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=%d:-2:flags=lanczos,format=gray" % (w, h, x, y, width)
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "info", "-y"]
    if not full:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", "%.2f" % max(0, ss), "-t", "%.2f" % dur, "-i", path, "-an", "-sn", "-vf", vf, "-fps_mode", "passthrough", os.path.join(outdir, "%06d.png")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(120, dur * 2))
    except Exception:
        return []
    ts = [float(m) for m in re.findall(r"pts_time:\s*([\d.]+)", r.stderr or "")]
    files = sorted(os.listdir(outdir))
    out = []
    for i, f in enumerate(files):
        if i < len(ts):
            out.append((max(0, ss) + ts[i], os.path.join(outdir, f)))
    return out


def _find_box(path, avail):
    """Find where the score is on the screen. -> (x, y, w, h) as fractions, or None."""
    tmp = os.path.join(OUT, "_clipdet")
    times = [t for t in (10, 25, 45, 70, 100, 150, 220, 300, 450, 700, 1000, 1500) if t < avail - 5][-5:]
    if len(times) < 3:
        return None
    frames = []
    for t in times:
        fr = _grab(path, t, 1.0, (0, 0, 1, 1), True, tmp + "_f", 1280)
        if fr:
            frames.append(fr[0][1])
            try: shutil.copy(fr[0][1], tmp + "_%d.png" % len(frames))
            except OSError: pass
    hits = []                                   # (cx, cy, x0, y0, x1, y1, frame no.)
    for n, png in enumerate(frames):
        W, H = 1280, None
        try:
            from PIL import Image
            W, H = Image.open(png).size
        except Exception:
            continue
        rows = {}
        for ln in _ocr(png, tsv=True).splitlines()[1:]:
            p = ln.split("\t")
            if len(p) < 12 or not p[11].strip():
                continue
            try:
                key = (p[2], p[3], p[4])
                rows.setdefault(key, []).append((int(p[6]), int(p[7]), int(p[8]), int(p[9]), p[11].strip()))
            except ValueError:
                continue
        # words are sparse (psm 11), so also join words on the same horizontal band
        words = [w for v in rows.values() for w in v]
        for (l, t_, wd, hh, txt) in words:
            if SC_RE.fullmatch(txt.replace(" ", "")) and re.search(r"\d", txt):
                m = SC_RE.search(txt)
                if int(m.group(2)) <= 10 and int(m.group(1)) <= 800:
                    hits.append((l / W, t_ / H, (l + wd) / W, (t_ + hh) / H, n))
    if not hits:
        return None
    best, bn = None, 0
    for h in hits:
        near = {x[4] for x in hits if abs(x[0] - h[0]) < 0.12 and abs(x[1] - h[1]) < 0.04}
        if len(near) > bn:
            best, bn = h, len(near)
    if best is None or bn < 2:
        return None
    x0, y0, x1, y1 = best[0], best[1], best[2], best[3]
    hh = max(0.03, y1 - y0)
    bx = max(0.0, x0 - 0.30); bw = min(1.0, x1 + 0.30) - bx
    by = max(0.0, y0 - 0.9 * hh); bh = min(1.0, y1 + 0.9 * hh) - by
    return (bx, by, bw, bh)


def _pick(text, prev):
    """Text of the score area -> (runs, wickets, balls) using what was seen before to ignore wrong numbers."""
    text = text.replace("(", " ").replace(")", " ")
    sc = [(int(a), int(b)) for a, b in SC_RE.findall(text) if int(b) <= 10 and int(a) <= 800]
    rest = SC_RE.sub(" ", text)
    ov = [int(a) * 6 + int(b) for a, b in OV_RE.findall(rest) if int(a) <= 60]
    if not sc:
        return None
    s = None
    if prev:
        good = [c for c in sc if 0 <= c[0] - prev[0] <= 8 and 0 <= c[1] - prev[1] <= 1]
        if good:
            s = min(good, key=lambda c: (c[0] - prev[0]) + 10 * (c[1] - prev[1]))
    if s is None:
        s = sc[0]
    o = None
    if prev and prev[2] is not None:
        good = [c for c in ov if 0 <= c - prev[2] <= 1]
        if good:
            o = good[0]
    elif ov:
        o = ov[0]
    return (s[0], s[1], o)


def _sample(path, t, rd):
    """One reading (t, (runs, wk, balls)) is judged; returns an event dict or None."""
    st = CLIP_ST
    conf = st["conf"]
    if rd is None:
        return None
    if conf is not None and rd[2] is None:
        rd = (rd[0], rd[1], conf[2])
    if conf is not None and rd == conf:
        st["pend"] = None
        return None
    pend = st["pend"]
    if pend and pend[0] == rd:
        pend[2] += 1
    else:
        pend = st["pend"] = [rd, (path, t), 1]
    if pend[2] < 2:
        return None
    st["pend"] = None
    t0path, t0 = pend[1]
    old, st["conf"] = conf, rd
    if old is None:
        return None
    dr, dw = rd[0] - old[0], rd[1] - old[1]
    db = (rd[2] - old[2]) if (rd[2] is not None and old[2] is not None) else None
    if dr < 0 or dw < 0 or dr > 12 or dw > 1:
        st["t_ov"] = None                       # new innings or a wrong reading: start again
        return None
    ov_txt = ("%d.%d" % divmod(rd[2], 6)) if rd[2] is not None else ""
    ev = None
    if dw == 1:
        gap = (t0 - st["t_ov"][1]) if (st["t_ov"] and st["t_ov"][0] == t0path) else None
        if gap is not None and 6 < gap < 240 and db == 0:
            start = st["t_ov"][1] - 15           # review: the ball was bowled long before the score changed
        else:
            start = t0 - CLIP["wpre"]
        ev = {"kind": "W", "path": t0path, "t": t0, "start": start, "end": t0 + CLIP["post"], "ov": ov_txt, "score": "%d/%d" % (rd[0], rd[1]), "ball": rd[2]}
    elif dw == 0 and ((dr in (4, 6) and (db is None or db <= 1)) or (dr in (5, 7) and db == 0)):
        k = "4" if dr in (4, 5) else "6"
        ev = {"kind": k, "path": t0path, "t": t0, "start": t0 - CLIP["pre"], "end": t0 + CLIP["post"], "ov": ov_txt, "score": "%d/%d" % (rd[0], rd[1]), "ball": rd[2]}
    if db:
        st["t_ov"] = (t0path, t0)
    if rd[2] is not None:
        with CLIP_LOCK:
            for e in CLIP_EV:
                if e["path"] == t0path and e.get("until_ball") is not None and not e.get("end_known") and rd[2] >= e["until_ball"]:
                    e["end"] = max(t0, e["t"] + 5); e["end_known"] = True
    if ev:
        last = st["last"].get((ev["path"], ev["kind"]))
        if last is not None and abs(last - ev["t"]) < 20:
            return None
        st["last"][(ev["path"], ev["kind"])] = ev["t"]
        if CLIP["after"] > 0 and ev.get("ball") is not None:
            ev["until_ball"] = ev["ball"] + CLIP["after"]            # the clip ends when the over counter shows this ball (1.1 + 2 = 1.3)
            ev["end_known"] = False
            ev["maxend"] = ev["t"] + CLIP["maxlen"]
    return ev


def clip_analyze(path, t_from, t_to, full, final=False):
    """Reads the score between two moments of a part file and queues clips for 4 / 6 / wicket."""
    if t_to - t_from < 3 or not CLIP["box"]:
        return
    tmp = os.path.join(OUT, "_clipfr")
    step = 90 if full else 240
    t = t_from
    while t < t_to and (final or not stop.is_set()):
        d = min(step, t_to - t)
        frames = _grab(path, t, d, CLIP["box"], full, tmp)
        prev = CLIP_ST["conf"]
        for ft, png in frames:
            txt = _ocr(png)
            rd = _pick(txt, CLIP_ST["conf"] or CLIP_ST["pend"] and CLIP_ST["pend"][0])
            ev = _sample(path, ft, rd)
            if ev:
                with CLIP_LOCK:
                    CLIP_EV.append(ev)
                print("Clip event: %s at %s (%s)" % (ev["kind"], mmss(ev["t"]), ev["score"]), flush=True)
        t += d


def clip_flush(path, avail, final=False):
    with CLIP_LOCK:
        for e in CLIP_EV:
            if e["path"] == path and e.get("until_ball") is not None and not e.get("end_known") and (final or avail >= e["maxend"]):
                e["end"] = min(e["maxend"], avail) if final else e["maxend"]; e["end_known"] = True      # the counter never got there: stop at the maximum
        ready = [e for e in CLIP_EV if e["path"] == path and (final or (e["end"] <= avail and e.get("end_known", True)))]
        for e in ready:
            CLIP_EV.remove(e)
    for e in ready:
        clip_cut(e, avail)


def tg_send_video(chat, path, caption, secs):
    """Send a clip as a Telegram video (Bot API: up to 50 MB). The clip stays in the channel/group for good."""
    if DRY or not TOKEN:
        print("[clips chat] video", os.path.basename(path), caption); return True
    for attempt in range(3):
        cmd = ["curl", "-sS", "-m", "600", "-X", "POST", "%s/bot%s/sendVideo" % (E("TG_API") or "https://api.telegram.org", TOKEN),
               "--form-string", "chat_id=" + str(chat), "--form-string", "caption=" + caption, "--form-string", "duration=%d" % int(secs),
               "--form-string", "supports_streaming=true", "-F", "video=@" + path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=620)
            j = json.loads(r.stdout or "{}")
        except Exception as e:
            j = {"description": repr(e)[:80]}
        if j.get("ok"):
            return True
        wait = (j.get("parameters") or {}).get("retry_after")
        print("sendVideo to the clips chat failed:", mask(str(j.get("description") or r.stderr if 'r' in dir() else j)[:140]), flush=True)
        time.sleep(min(int(wait), 60) if wait else 3 + attempt * 4)
    return False


def tg_send_big(chat, path, caption, secs):
    """Original quality, up to 2 GB, as a normal video: through Telegram's own protocol (needs API_ID + API_HASH and telethon).
    Works for a public channel/group (@name) and for chats the bot can see; otherwise it returns False."""
    api_id, api_hash = E("API_ID"), E("API_HASH")
    if DRY or not (api_id and api_hash and TOKEN):
        return False
    try:
        from telethon import TelegramClient
        from telethon.sessions import MemorySession
        from telethon.tl.types import DocumentAttributeVideo
    except Exception:
        print("Big clips: telethon is not installed", flush=True)
        return False
    import asyncio
    w, h = media_info(path)[1:3]

    async def go():
        c = TelegramClient(MemorySession(), int(api_id), api_hash)
        await c.start(bot_token=TOKEN)
        try:
            ent = chat if str(chat).startswith("@") else int(chat)
            peer = await c.get_input_entity(ent)
            await c.send_file(peer, path, caption=caption, supports_streaming=True, mime_type="video/mp4",
                              attributes=[DocumentAttributeVideo(duration=int(secs), w=w or 1280, h=h or 720, supports_streaming=True)])
        finally:
            await c.disconnect()
    try:
        asyncio.run(asyncio.wait_for(go(), 1800))
        return True
    except Exception as e:
        print("Big clip upload failed:", mask(repr(e))[:160], flush=True)
        return False


def shrink_clip(path, secs):
    """Last way to keep it a video: re-encode to fit the 50 MB limit."""
    out = path[:-4] + "_small.mp4"
    vb = max(300_000, int(CLIP_MAX * 8 / max(secs, 1) * 0.92) - 110_000)
    vf = ["-vf", "scale=-2:720"] if vb < 1_200_000 else []
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", str(vb), "-maxrate", str(int(vb * 1.3)), "-bufsize", str(vb * 2)] + vf + ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    except Exception as e:
        print("Shrinking the clip failed:", repr(e)[:100], flush=True); return None
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) > CLIP_MAX:
        print("Shrinking the clip failed:", (r.stderr or "")[-120:], flush=True)
        try: os.remove(out)
        except OSError: pass
        return None
    return out


def clip_to_chat(path, cap, secs):
    """A clip always goes as a real video: small = Bot API, big = original quality over MTProto, else shrunk to 50 MB."""
    if os.path.getsize(path) <= CLIP_MAX:
        return tg_send_video(CLIP_CHAT, path, cap, secs)
    if tg_send_big(CLIP_CHAT, path, cap, secs):
        return True
    small = shrink_clip(path, secs)
    if small:
        print("The clip was shrunk to %s to fit 50 MB" % human(os.path.getsize(small)), flush=True)
        ok = tg_send_video(CLIP_CHAT, small, cap + "  (compressed)", secs)
        if not DRY:
            try: os.remove(small)
            except OSError: pass
        return ok
    return False

def clip_cut(ev, avail):
    """Cut one clip and send it as a video to the chat."""
    path = ev["path"]
    start = max(0.0, ev["start"]); end = min(avail, ev["end"]) if avail else ev["end"]
    if end - start < 3 or not os.path.exists(path):
        return
    CLIP_ST["n"] += 1
    nm = {"4": "FOUR", "6": "SIX", "W": "WICKET"}[ev["kind"]]
    os.makedirs(os.path.join(OUT, "_clips"), exist_ok=True)
    out = os.path.join(OUT, "_clips", "%s_%02d_over_%s.mp4" % (nm, CLIP_ST["n"], (ev["ov"] or "x").replace(".", "-")))
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.2f" % start, "-i", path, "-t", "%.2f" % (end - start),
           "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        print("Clip cut failed:", repr(e)[:100], flush=True); return
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 20000:
        print("Clip cut failed:", (r.stderr or "")[-150:], flush=True); return
    icon = {"4": "🏏 FOUR", "6": "🚀 SIX", "W": "☝️ WICKET"}[ev["kind"]]
    cap = "%s  ·  over %s  ·  %s  ·  video %s" % (icon, ev["ov"] or "?", ev["score"], mmss(ev["t"]))
    if clip_to_chat(out, cap, end - start):
        CLIP_ST["sent"] += 1
        CLIP_ST.setdefault("kinds", []).append(ev["kind"])
    else:
        ok, link, err = gofile_upload(out)
        say("%s\n⬇️ %s" % (cap, ('<a href="%s">Clip</a>' % link) if ok else "could not be sent: " + esc(err)))
        if ok:
            CLIP_ST["sent"] += 1; CLIP_ST.setdefault("kinds", []).append(ev["kind"])
    if not DRY:
        try: os.remove(out)
        except OSError: pass


# ---------------------------- getting the video ----------------------------

def telethon_download(dest):
    """A video sent to the bot (up to 2 GB) through Telegram's own protocol; the Bot API itself stops at 20 MB.
    The message is fetched by its number (works in the private chat with the bot); the old file-id way is the backup."""
    if not (API_ID and API_HASH):
        raise RuntimeError("API_ID / API_HASH secrets are not set")
    try:
        from telethon import TelegramClient, utils
        from telethon.sessions import MemorySession
    except Exception:
        raise RuntimeError("telethon is not installed")

    async def go():
        c = TelegramClient(MemorySession(), int(API_ID), API_HASH)
        await c.start(bot_token=TOKEN)
        try:
            media, size, why = None, FILE_SIZE, []
            if MSG:
                try:
                    m = await c.get_messages(None, ids=int(MSG))
                    if m is not None and getattr(m, "media", None):
                        media = m.media
                        size = getattr(getattr(m, "document", None), "size", 0) or size
                    else:
                        why.append("message not found")
                except Exception as e:
                    why.append("message: " + repr(e)[:80])
            if media is None and FILE_ID:
                try:
                    media = utils.resolve_bot_file_id(FILE_ID)
                    if media is None:
                        why.append("file id not understood")
                except Exception as e:
                    why.append("file id: " + repr(e)[:80])
            if media is None:
                raise RuntimeError("could not find the video through Telegram (%s). Send it in a private chat with the bot" % "; ".join(why))
            got, last = 0, time.time()
            with open(dest, "wb") as f:
                async for chunk in c.iter_download(media, request_size=1024 * 1024, file_size=size or None):
                    f.write(chunk); got += len(chunk)
                    if time.time() - last > 30:
                        last = time.time(); print("Downloaded %s of %s" % (human(got), human(size or got)), flush=True)
            if got < 100000:
                raise RuntimeError("Telegram sent an empty file")
        finally:
            await c.disconnect()
    asyncio.run(go())


def gofile_resolve(url):
    """Best effort (Gofile changes this now and then): gofile.io/d/<id> -> [(direct link, name)] and the cookie the download needs."""
    import hashlib
    m = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
    if not m:
        return [], ""
    cid = m.group(1)
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

    def j(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        try:
            return json.loads(r.stdout or "{}")
        except Exception:
            return {}
    tk = j(["curl", "-sS", "-A", ua, "-X", "POST", "-H", "Content-Type: application/json", "-d", "{}", "https://api.gofile.io/accounts"])
    tok = (tk.get("data") or {}).get("token", "")
    secrets = [E("GOFILE_WT_SECRET")] if E("GOFILE_WT_SECRET") else []
    secrets += ["12af056dacea0b"]          # the value in use in the public gofile downloader tools; it can change: then set the GOFILE_WT_SECRET secret
    d, err = {}, ""
    for sec in secrets:
        wt = hashlib.sha256(("%s::en-US::%s::%d::%s" % (ua, tok, int(time.time()) // 14400, sec)).encode()).hexdigest()
        r = j(["curl", "-sS", "-A", ua, "-H", "Authorization: Bearer " + tok, "-H", "X-Website-Token: " + wt, "-H", "X-BL: en-US",
               "https://api.gofile.io/contents/%s?cache=true&sortField=createTime&sortDirection=1" % cid])
        if r.get("status") == "ok":
            d = r.get("data") or {}; break
        err = str(r.get("status") or "no answer")
        if "password" in err.lower():
            break
    if not d:
        raise RuntimeError("Gofile said: %s" % err)
    files = []

    def walk(node):
        for ch in (node.get("children") or node.get("contents") or {}).values():
            if ch.get("type") == "file" and ch.get("link") and (str(ch.get("mimetype") or "").startswith("video") or FILE_EXT.search(ch.get("name") or "")):
                files.append((ch["link"], ch.get("name") or "video.mp4"))
            elif ch.get("type") == "folder":
                walk(ch)
    if d.get("type") == "file" and d.get("link"):
        files.append((d["link"], d.get("name") or "video.mp4"))
    walk(d)
    if not files:
        raise RuntimeError("Gofile page has no video file")
    return files, "accountToken=" + tok


def link_download(url, dest):
    cookie = ""
    if "gofile.io/d/" in url:
        try:
            files, cookie = gofile_resolve(url)
        except RuntimeError as e:
            raise RuntimeError("%s. Gofile changes its download system often; send a direct video link (.mp4) instead" % e)
        url = files[0][0]
    cmd = ["curl", "-L", "-sS", "--fail", "--retry", "3", "--connect-timeout", "30", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36", "-o", dest, url]
    if cookie:
        cmd[1:1] = ["-H", "Cookie: " + cookie, "-H", "Authorization: Bearer " + cookie.split("=", 1)[1]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("download failed: " + (r.stderr or "").strip()[-150:])


FILE_EXT = re.compile(r"\.(mp4|mkv|mov|webm|avi|m4v)(\?|$)", re.I)


def is_file_link(url):
    """A plain video file (or a gofile page) is downloaded; everything else (m3u8, mpd, ts, live, archive ...) goes to the recorder."""
    return bool(FILE_EXT.search(url) or "gofile.io/d/" in url)


def run_recorder(text, chat, env_extra=None):
    """Live streams and every stream type of the recorder (HLS .m3u8, MPEG-DASH .mpd, MPEG-TS .ts, timeshift / archive, headers, proxy, ClearKey):
    the same recorder program runs, but only the clips are kept and sent."""
    here = os.path.dirname(os.path.abspath(__file__))
    rec = os.path.join(here, "cliprec.py")
    if not os.path.exists(rec):
        say("❌ cliprec.py is missing in this repository (the clip bot needs it for stream links).")
        return False
    env = dict(os.environ)
    env.update({"CLIPS": "1", "CLIPS_ONLY": "1", "TG_CHAT": str(chat), "CLIP_CHAT": E("CLIP_CHAT") or str(chat),
                "WORKFLOW_FILE": "clipbot.yml", "URL": "ask", "LINK_TEXT": text + " clips", "MINUTES": E("MINUTES") or "0"})
    env.update(env_extra or {})
    print("Starting the recorder for a stream link", flush=True)
    return subprocess.run([sys.executable, rec], env=env).returncode == 0


def reset_state():
    CLIP["box"] = None; CLIP["tries"] = 0
    CLIP_EV.clear(); CLIP_PART.clear()
    CLIP_ST.update({"conf": None, "pend": None, "t_ov": None, "n": 0, "last": {}, "sent": 0, "kinds": []})


def process_one(link=None):
    """One video (FILE_ID) or one link. Returns True when it ended well."""
    os.makedirs(OUT, exist_ok=True)
    reset_state()
    clip_words(CAPTION)
    path = LOCAL_FILE or os.path.join(OUT, "input.mp4")
    if not LOCAL_FILE:
        try:
            if os.path.exists(path):
                os.remove(path)
            if FILE_ID:
                say(("⏳ <b>Video received.</b> Downloading it (%s)..." % human(FILE_SIZE)) if FILE_SIZE else "⏳ <b>Video received.</b> Downloading it...")
                telethon_download(path)
            elif link:
                if re.search(r"\.(m3u8|mpd)(\?|$)", link, re.I):
                    say("⚠️ This is a live/stream link. Use the recording bot for it (it records and cuts the clips there).")
                    return False
                say("⏳ <b>Link received.</b> Downloading the file...")
                link_download(link, path)
            else:
                return False
        except Exception as e:
            say("❌ <b>Could not get the video.</b>\n<code>%s</code>" % esc(mask(str(e))[:300]))
            return False
    if not os.path.exists(path) or os.path.getsize(path) < 100000:
        say("❌ The video file is empty or missing."); return False
    dur = media_info(path)[0]
    if dur < 30:
        say("❌ I could not read this video (length %ss)." % int(dur)); return False
    if not ensure_tesseract():
        say("❌ The text reader (tesseract) could not be installed."); return False
    say("🔎 Video is ready (%s, %s). Looking for the score on the screen..." % (mmss(dur), human(os.path.getsize(path))))
    box = None
    for _ in range(2):
        box = _find_box(path, dur)
        if box:
            break
    if not box:
        say("❌ <b>I could not find the score on the screen.</b>\nIt must be visible as text like <code>145/3</code> and <code>12.3</code>. No clips were made.")
        return False
    CLIP["box"] = box
    clip_analyze(path, 0, dur, False, final=True)
    n = len(CLIP_EV)
    clip_flush(path, dur, final=True)
    k = CLIP_ST.get("kinds", [])
    if CLIP_ST["sent"]:
        say("✅ <b>Done.</b> %d clip(s): %d × 4, %d × 6, %d × wicket." % (CLIP_ST["sent"], k.count("4"), k.count("6"), k.count("W")))
    elif n == 0:
        say("ℹ️ Score found, but no 4, 6 or wicket was seen in this video.")
    else:
        say("⚠️ Clips were found but could not be sent.")
    if not LOCAL_FILE:
        try: os.remove(path)
        except OSError: pass
    return True


def listen():
    """No video was given to the job: wait in Telegram for videos / links from the owner (also the ones sent before the job started),
    cut the clips for each, and stop after WAIT_MIN quiet minutes. The owner can go offline after sending."""
    global CHAT, MSG, FILE_ID, FILE_SIZE, CAPTION
    owner = E("OWNER_ID") or E("TG_CHAT")
    wait = float(E("WAIT_MIN") or 20) * 60
    if not owner:
        print("::error title=Owner missing::Secret TG_CHAT (your Telegram id) is not set.", flush=True); return 1
    r = api("getUpdates", {"timeout": "0", "limit": "1"})
    if r.get("error_code") == 409:
        api("deleteWebhook", {"drop_pending_updates": "false"})
    CHAT = owner
    say("🏏 <b>Clip bot is on.</b> Send me a <b>video</b> (up to 2 GB) or a <b>link to a video file</b>. You can go offline after sending.\nI stop after %d quiet minutes.\nOptional words in the caption: <code>pre 30</code> <code>wpre 30</code> <code>post 15</code>" % int(wait / 60))
    off, last_job, t_start, jobs = 0, time.time(), time.time(), 0
    while time.time() - t_start < 5 * 3600 + 40 * 60:
        if time.time() - last_job > wait:
            break
        r = api("getUpdates", {"offset": str(off), "timeout": "25", "allowed_updates": '["message"]'}, timeout=45)
        for u in r.get("result", []) or []:
            off = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("from") or {}).get("id")) != str(owner) and str((m.get("chat") or {}).get("id")) != str(owner):
                continue
            text = (m.get("text") or m.get("caption") or "").strip()
            vid = m.get("video") or (m.get("document") if str((m.get("document") or {}).get("mime_type", "")).startswith("video/") else None) or m.get("animation")
            lk = (re.search(r"https?://\S+", text) or [None])[0]
            if not vid and not lk:
                if re.match(r"/(start|help)", text):
                    say("Send me a video or a link to a video file. I send a clip for every 4, 6 and wicket.")
                continue
            CHAT = str((m.get("chat") or {}).get("id"))
            MSG = str(m.get("message_id") or "")
            FILE_ID = vid["file_id"] if vid else ""
            FILE_SIZE = int(vid.get("file_size") or 0) if vid else 0
            CAPTION = re.sub(r"https?://\S+", " ", text)
            if lk and not vid and not is_file_link(lk):
                say("🎥 <b>Stream link received.</b> Recording it, the clips come here as they happen.\nThis run stays busy with it; for another video start the workflow again.")
                run_recorder(text, CHAT)
                jobs += 1
                break                                                    # a recording can run for hours: after it this run is done
            process_one(lk if not vid else None)
            jobs += 1
            last_job = time.time()
        if not r.get("ok", True) and not r.get("result"):
            time.sleep(3)
    CHAT = owner; MSG = ""
    say("💤 <b>Clip bot stopped</b> (%d video(s) done). Start the workflow again when you have more." % jobs)
    return 0


def main():
    global CAPTION
    os.makedirs(OUT, exist_ok=True)
    u = E("URL")
    if u.startswith("enc:") or u.startswith("http"):                      # started by a link: first start of a stream, or the next run of a long recording
        say("🔁 <b>Continuing the recording</b> (new run)." if u.startswith("enc:") else "🎥 <b>Recording the stream.</b>")
        env = {"URL": u} if u.startswith("enc:") or u.startswith("http") else {}
        return 0 if run_recorder(E("LINK_TEXT") or u, E("CHAT") or E("TG_CHAT"), env) else 1
    link = (re.search(r"https?://\S+", E("LINK") or "") or [None])[0]
    if link and not is_file_link(link):
        return 0 if run_recorder(E("LINK") + " " + CAPTION, E("CHAT") or E("TG_CHAT")) else 1
    if FILE_ID or link or LOCAL_FILE:
        return 0 if process_one(link) else 1
    return listen()


if __name__ == "__main__":
    sys.exit(main())
