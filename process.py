"""
TikTok part maker: runs on GitHub Actions (your PC can stay off).

Google Drive layout (inside your "TikTok automation" folder). Which folder you drop a
movie into decides the mode, and its parts go to the matching "Ready to post" folder:

    To edit/                  -> Ready to post/                  highlights mode
    To edit (All)/            -> Ready to post (All)/            all mode
    To edit (Best stretch)/   -> Ready to post (Best stretch)/   best stretch mode
    Failed/                   <- anything that couldn't be processed

Drop videos (and an optional .srt with the same name), OR a .txt file containing an
archive.org link, into a "To edit" folder. Finished originals are DELETED (to Drive's
bin) and listed in "Finished.txt" in the folder they came from. Missing folders are
created automatically.

Modes:
  highlights  Scores the whole video for "interesting" moments (loud audio,
              sudden sound spikes, fast scene cuts, lots of movement, busy
              dialogue) and keeps only the best stretches as Part 1, Part 2...
  all         Cuts the whole video into back-to-back ~PART_SECONDS parts.
  stretch     Finds the most interesting STRETCH_MINUTES of the movie and cuts just
              that section into back-to-back ~STRETCH_PART_SECONDS parts.

Either way, every cut lands on a quiet moment (a pause) where possible, each part
is made vertical 9:16 (1080x1920) with a blurred background, gets "Part X" at the
top, and gets burned-in English subtitles: from a matching .srt if you added one,
otherwise made automatically from the clip's own audio with Whisper (speech
recognition), translated into English if the movie is in another language.
"""
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REMOTE = "gdrive:"
TO_EDIT = "To edit"
READY = "Ready to post"
DONE = "Done"
FAILED = "Failed"
FINISHED_LOG = "Finished.txt"

MODE = os.environ.get("MODE", "highlights").strip().lower()   # mode for the plain "To edit" folder
STRETCH_MINUTES = float(os.environ.get("STRETCH_MINUTES", "12"))           # "stretch" mode: how much of the movie
STRETCH_PART_SECONDS = float(os.environ.get("STRETCH_PART_SECONDS", "75"))  # "stretch" mode: part length
MODE_NAMES = {"highlights": "Highlights", "all": "All", "stretch": "Best stretch"}
# (input folder, output folder, mode). The first one's mode comes from MODE above.
FOLDERS = [
    ("To edit", "Ready to post", MODE),
    ("To edit (All)", "Ready to post (All)", "all"),
    ("To edit (Best stretch)", "Ready to post (Best stretch)", "stretch"),
]
PART_SECONDS = float(os.environ.get("PART_SECONDS", "120"))      # "all" mode: part length
EARLIEST = float(os.environ.get("CUT_EARLIEST", "25"))           # "all" mode: may cut this early...
LATEST = float(os.environ.get("CUT_LATEST", "15"))               # ...or this late, to land on a pause
HIGHLIGHT_SECONDS = float(os.environ.get("HIGHLIGHT_SECONDS", "75"))  # "highlights" mode: clip length
HIGHLIGHT_COUNT = int(os.environ.get("HIGHLIGHT_COUNT", "0"))    # 0 = automatic (about 1 per 10 min)
MIN_LAST_PART = 30
FLIP = os.environ.get("FLIP", "none").strip().lower()             # none / mirror / upsidedown / both
FLIP_FILTERS = {"mirror": "hflip", "upsidedown": "vflip", "both": "hflip,vflip"}
FLIP_EVERY = float(os.environ.get("FLIP_EVERY", "0"))  # >0: switch on scene cuts, at most once per this many seconds; 0: always flipped
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()               # phone notifications (ntfy app)
MAX_RUN_MINUTES = float(os.environ.get("MAX_RUN_MINUTES", "300"))  # don't start a new movie after this
STARTED = time.time()
COCKROACH = os.environ.get("COCKROACH", "on").strip().lower() not in ("off", "no", "0", "false")
COCKROACH_CHANCE = float(os.environ.get("COCKROACH_CHANCE", "1"))   # chance a part gets a cockroach run
COCKROACH_SIZE = int(os.environ.get("COCKROACH_SIZE", "100"))         # length in pixels (screen is 1080 wide)
LINE = os.environ.get("LINE", "on").strip().lower() not in ("off", "no", "0", "false")
LINE_WIDTH = int(os.environ.get("LINE_WIDTH", "360"))       # pixels (screen is 1080 wide)
LINE_THICKNESS = int(os.environ.get("LINE_THICKNESS", "5"))
ASSETS = Path(__file__).resolve().parent / "assets"
AUTO_SUBS = os.environ.get("AUTO_SUBTITLES", "on").strip().lower() not in ("off", "no", "0", "false")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
FONT = os.environ.get("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
WORK = Path("work")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".mpg", ".mpeg", ".ts", ".ogv"}
JUNK = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|10bit|bluray|blu ray|brrip|bdrip|webrip|web dl|web|"
    r"hdtv|dvdrip|hdrip|x264|x265|h264|h265|hevc|aac|ac3|dts|proper|repack|extended|remastered|"
    r"512kb|archive|ia)\b.*$",
    re.I,
)


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def notify(title, message, tags=None):
    """Sends a phone notification through the ntfy app. Never stops the run if it fails."""
    if not NTFY_TOPIC:
        return
    title = f"[{MODE_NAMES.get(MODE, MODE)}] {title}"
    body = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message, "tags": tags or []}).encode()
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"(notification not sent: {e})", flush=True)


def clean_name(filename):
    """'Nosferatu.1922.1080p.mkv' -> 'Nosferatu (1922)'"""
    stem = Path(filename).stem
    base = re.sub(r"[._]+", " ", stem)
    base = re.sub(r"[\[\]\(\)\{\}]", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    name = None
    for m in re.finditer(r"\b(1[89]\d{2}|20\d{2})\b", base):
        if m.start() > 0:
            name = f"{base[:m.start()].strip(' -')} ({m.group(1)})"
            break
    if not name:
        name = JUNK.sub("", base).strip(" -") or stem
    return safe(name)


def safe(name):
    return re.sub(r'[\\/:*?"<>|%]', "", name).strip()


def fmt_time(s):
    s = int(round(s))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


# ---------------------------------------------------------------- subtitles
def ts_to_sec(ts):
    h, m, rest = ts.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def sec_to_ts(s):
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def parse_srt(path):
    if not path:
        return []
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"\s*([\d:,.]+)\s*-->\s*([\d:,.]+)", line)
            if m:
                body = "\n".join(lines[i + 1:]).strip()
                if body:
                    cues.append((ts_to_sec(m.group(1)), ts_to_sec(m.group(2)), body))
                break
    return cues


def write_part_srt(cues, start, end, out_path):
    """Keeps only lines inside this part and shifts them to start at 0."""
    n, out = 0, []
    for a, b, body in cues:
        if b <= start or a >= end:
            continue
        n += 1
        out.append(f"{n}\n{sec_to_ts(a - start)} --> {sec_to_ts(min(b, end) - start)}\n{body}\n")
    if not out:
        return False
    Path(out_path).write_text("\n".join(out), encoding="utf-8")
    return True


# ---------------------------------------------------------------- captions (free, no AI)
CAPTIONS_ON = os.environ.get("CAPTIONS", "on").strip().lower() not in ("off", "no", "0", "false")

CAPTION_HOOKS = [
    "Wait for it… 😳",
    "Nobody talks about this part 👀",
    "I wasn't ready for this 😭",
    "Watch till the end 🤯",
    "This scene lives in my head rent free",
    "POV: you finally found something worth watching 🍿",
    "Tell me you saw that coming 👇",
    "The way I gasped 😮",
    "Bro did NOT think this through 💀",
    "Rate this scene 1-10 👇",
    "Sound on for this one 🔊",
    "I've watched this 10 times already 🔁",
    "Things you notice on the second watch 👀",
    "This is why I don't trust anyone 😶",
    "Okay but why is this so good",
    "Stop scrolling, you need to see this ✋",
    "Not me staying up till 3am for this 🌙",
    "Plot twist incoming 🌀",
    "Tag someone who needs to see this 👇",
    "The ending of this part 😳",
    "Me pretending I understood everything 🤡",
    "Who else remembers this? 🙋",
    "This deserves way more attention",
    "Comment if you know what happens next 👇",
    "Hardest scene ever? 🔥",
    "The tension in this one 😬",
    "Save this for later 📌",
    "I need answers 😩",
    "Just one more part… said everyone 😅",
    "Bet you can't watch just one 😏",
    "Late night watch 🍿🌙",
    "No way this actually happened 😮",
    "The vibes are unmatched ✨",
    "Lowkey the best part 🤫",
    "Pause at the right moment 😂",
    "Main character energy 😎",
    "Can't believe I almost skipped this",
    "This hits different at night 🌌",
    "Rewatching this hits even harder",
    "Drop a 🔥 if you're still watching",
]
CAPTION_ENDINGS = [
    "Part {next} is up next 👀",
    "Follow so you don't miss Part {next} 🔔",
    "Part {next} coming soon ⏳",
    "Should I post Part {next}? 👇",
    "Comment 'more' for Part {next} 👇",
]
CAPTION_LAST = [
    "That's the last part. Follow for more 🍿",
    "Final part! What did you think? 👇",
    "And that's a wrap 🎬 Follow for the next one",
]
HASHTAG_SETS = [
    "#fyp #foryou #viral #movie #movieclips",
    "#fyp #foryoupage #movies #film #mustwatch",
    "#viral #fyp #movietok #filmtok #scene",
    "#foryou #trending #movie #cinema #fypシ",
    "#fyp #movieclip #filmclips #viralvideo #watch",
]


def make_captions(count):
    """One caption per part: a random hook, a 'next part' line, and hashtags. No repeats within a movie."""
    hooks = random.sample(CAPTION_HOOKS, k=min(count, len(CAPTION_HOOKS)))
    while len(hooks) < count:
        hooks.append(random.choice(CAPTION_HOOKS))
    out = []
    for i in range(1, count + 1):
        end = (random.choice(CAPTION_ENDINGS).format(next=i + 1) if i < count else random.choice(CAPTION_LAST))
        out.append(f"{hooks[i - 1]}\nPart {i} 🎬 · {end}\n{random.choice(HASHTAG_SETS)}")
    return out


# ---------------------------------------------------------------- automatic subtitles (Whisper)
_WHISPER = None
SUB_INFO = {"lang": None, "note": None}   # per movie: spoken language, and what kind of subs were used


def whisper_model():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        print(f"Loading the Whisper '{WHISPER_MODEL}' speech model...", flush=True)
        _WHISPER = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _WHISPER


def words_to_lines(words, max_chars=42, max_secs=3.5, max_gap=0.7):
    """Groups Whisper's timed words into short, readable subtitle lines."""
    cues, cur = [], []
    def flush():
        if cur:
            text = "".join(w.word for w in cur).strip()
            if text:
                cues.append((cur[0].start, max(cur[-1].end, cur[0].start + 0.6), text))
            cur.clear()
    for w in words:
        if cur and (len("".join(x.word for x in cur) + w.word) > max_chars
                    or w.end - cur[0].start > max_secs or w.start - cur[-1].end > max_gap):
            flush()
        cur.append(w)
        if w.word.strip()[-1:] in ".?!":
            flush()
    flush()
    return cues


def auto_subs(src, start, end, workdir):
    """English subtitle lines for one part, timed from 0 = start of the part. [] if no speech."""
    wav = workdir / "part.wav"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
         "-i", str(src.resolve()), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    try:
        import numpy as np
        import wave
        with wave.open(str(wav)) as wf:   # read the audio ourselves (avoids a PyAV version clash)
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        model = whisper_model()
        opts = dict(vad_filter=True, vad_parameters=dict(threshold=0.3, min_speech_duration_ms=150),
                    word_timestamps=True, condition_on_previous_text=False, beam_size=5)
        lang = SUB_INFO["lang"]
        if not lang:
            segs, info = model.transcribe(audio, **opts)
            if info.language_probability >= 0.6:
                lang = SUB_INFO["lang"] = info.language
                print(f"  spoken language: {lang} ({info.language_probability:.0%} sure)", flush=True)
        if lang and lang != "en":
            segs, _ = model.transcribe(audio, language=lang, task="translate", **opts)
        else:
            segs, _ = model.transcribe(audio, language="en", **opts)
        words = [w for seg in segs for w in (seg.words or [])]
        cues = words_to_lines(words)
        SUB_INFO["note"] = SUB_INFO["note"] or (
            f"translated to English from '{lang}'" if lang and lang != "en" else "made automatically (English)")
        print(f"  subtitles: {len(cues)} lines", flush=True)
        for a, b, text in cues:
            print(f"    {fmt_time(start + a)}  {text}", flush=True)
        return cues
    finally:
        wav.unlink(missing_ok=True)


# ---------------------------------------------------------------- analysis
def analyse(path, workdir):
    """
    One pass over the video (at low resolution, so it's quick).
    Returns per-second lists: loudness (dB), motion (0-1), cuts (count),
    plus the middle of every pause in the audio.
    """
    total = duration(path)
    n = int(total) + 1
    scene_file, audio_file = workdir / "scene.txt", workdir / "audio.txt"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path.resolve()),
           "-map", "0:v:0",
           "-vf", f"fps=5,scale=192:-2,select='gte(scene,0)',metadata=print:file={scene_file.name}"]
    audio = has_audio(path)
    if audio:
        cmd += ["-map", "0:a:0",
                "-af", f"aresample=16000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
                       f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={audio_file.name}"]
    cmd += ["-f", "null", "-"]
    print("Analysing the whole video for interesting moments...", flush=True)
    run(cmd, cwd=workdir)

    motion = [0.0] * n
    motion_count = [0] * n
    cuts = [0] * n
    t = None
    for line in scene_file.read_text().splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None and int(t) < n:
            s = float(m.group(1))
            motion[int(t)] += s
            motion_count[int(t)] += 1
            if s > 0.35:
                cuts[int(t)] += 1
    motion = [motion[i] / motion_count[i] if motion_count[i] else 0.0 for i in range(n)]

    loud = [-90.0] * n
    if audio and audio_file.exists():
        sums, counts = [0.0] * n, [0] * n
        t = None
        for line in audio_file.read_text().splitlines():
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1))
                continue
            m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
            if m and t is not None and int(t) < n:
                v = -90.0 if m.group(1) == "-inf" else max(-90.0, float(m.group(1)))
                sums[int(t)] += v
                counts[int(t)] += 1
        loud = [sums[i] / counts[i] if counts[i] else -90.0 for i in range(n)]

    return total, loud, motion, cuts, quiet_moments(path) if audio else []


def quiet_moments(path):
    """Middle of every pause (>= 0.4s below -35dB) in the audio, in seconds."""
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", res.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def subtitle_gaps(cues):
    return [(a[1] + b[0]) / 2 for a, b in zip(cues, cues[1:]) if b[0] - a[1] >= 0.3]


def rank01(values):
    """Turns raw numbers into 0..1 by rank, so loud films and quiet films score fairly."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    denom = max(1, len(values) - 1)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = ((i + j) / 2) / denom
        for k in range(i, j + 1):
            out[order[k]] = r
        i = j + 1
    return out


def moving_avg(values, radius):
    out, s = [], 0.0
    pref = [0.0]
    for v in values:
        s += v
        pref.append(s)
    n = len(values)
    for i in range(n):
        a, b = max(0, i - radius), min(n, i + radius + 1)
        out.append((pref[b] - pref[a]) / (b - a))
    return out


def interest_scores(loud, motion, cuts, cues):
    n = len(loud)
    # sudden sound: how much louder this second is than the 30s around it
    background = moving_avg(loud, 15)
    spike = [max(0.0, loud[i] - background[i]) for i in range(n)]
    cut_rate = moving_avg([float(c) for c in cuts], 3)
    dialogue = [0.0] * n
    for a, b, body in cues:
        chars = len(body)
        for s in range(int(a), min(n, int(b) + 1)):
            dialogue[s] += chars / max(1.0, b - a)

    features = [(rank01(loud), 0.30), (rank01(spike), 0.20), (rank01(motion), 0.20), (rank01(cut_rate), 0.15)]
    if cues:
        features.append((rank01(dialogue), 0.15))
    total_w = sum(w for _, w in features)
    score = [sum(f[i] * w for f, w in features) / total_w for i in range(n)]
    # silence is never interesting
    score = [0.0 if loud[i] < -60 else score[i] for i in range(n)]
    return score


def snap(t, candidates, before, after):
    window = [c for c in candidates if t - before <= c <= t + after]
    return min(window, key=lambda c: abs(c - t)) if window else t


def choose_highlights(total, score, candidates):
    length = min(HIGHLIGHT_SECONDS, total)
    count = HIGHLIGHT_COUNT or max(1, min(12, round(total / 600)))
    L = int(length)
    # ignore the first 1% (logos) and the last 4% (end credits) of long videos
    lo = int(total * 0.01) if total > 1200 else 0
    hi = int(total * 0.96) if total > 1200 else int(total)
    pref = [0.0]
    for v in score:
        pref.append(pref[-1] + v)
    windows = []
    for s in range(lo, max(lo + 1, hi - L + 1)):
        e = min(len(score), s + L)
        windows.append(((pref[e] - pref[s]) / max(1, e - s), s))
    windows.sort(reverse=True)

    picked = []
    gap = 10
    for avg, s in windows:
        if len(picked) >= count:
            break
        if all(s + L + gap <= ps or s >= ps + L + gap for _, ps in picked):
            picked.append((avg, s))
    clips = []
    for avg, s in sorted(picked, key=lambda p: p[1]):
        a = snap(float(s), candidates, 8, 3)
        b = snap(float(s + L), candidates, 5, 8)
        clips.append((max(0.0, a), min(total, b), avg))
    return clips


def choose_cuts(total, candidates, part=None, begin=0.0):
    """Back-to-back parts of ~part seconds from begin to total. Returns [(start, end, None)]."""
    part = part or PART_SECONDS
    early, late = min(EARLIEST, part * 0.3), min(LATEST, part * 0.2)
    candidates = sorted(candidates)
    cuts, pos = [], begin
    while total - pos > part + late:
        target = pos + part
        cut = snap(target, candidates, early, late)
        cuts.append((pos, cut, None))
        pos = cut
    if cuts and total - pos < min(MIN_LAST_PART, part * 0.5):
        cuts[-1] = (cuts[-1][0], total, None)
    else:
        cuts.append((pos, total, None))
    return cuts


def choose_stretch(total, score, candidates):
    """'stretch' mode: the most interesting STRETCH_MINUTES, cut into back-to-back parts."""
    L = int(min(STRETCH_MINUTES * 60, total))
    lo = int(total * 0.01) if total > 1200 else 0
    hi = int(total * 0.96) if total > 1200 else int(total)
    pref = [0.0]
    for v in score:
        pref.append(pref[-1] + v)
    best, best_s = -1.0, lo
    for s in range(lo, max(lo + 1, hi - L + 1)):
        e = min(len(score), s + L)
        avg = (pref[e] - pref[s]) / max(1, e - s)
        if avg > best:
            best, best_s = avg, s
    a = max(0.0, snap(float(best_s), candidates, 20, 5))
    b = min(total, snap(float(best_s + L), candidates, 5, 20))
    print(f"Best stretch: {fmt_time(a)} to {fmt_time(b)} (interest score {best * 100:.0f}/100)", flush=True)
    return choose_cuts(b, candidates, STRETCH_PART_SECONDS, a)


# ---------------------------------------------------------------- render
FLIP_STATE = {"on": False, "last": -1e9}   # carried from part to part within one movie


def reset_flip():
    FLIP_STATE.update(on=False, last=-1e9)


def scene_cuts(src, start, length):
    """Exact times (seconds from the part's start) where the shot changes."""
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
         "-i", str(src.resolve()), "-an", "-sn",
         "-vf", "scale=192:-2,select='gt(scene,0.35)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return sorted(float(x) for x in re.findall(r"showinfo.*?pts_time:([\d.]+)", res.stderr))


def flip_filter(src, start, end):
    """
    Video filter for the flip. With FLIP_EVERY > 0 the picture switches between normal and
    flipped only on a scene cut, and at most once every FLIP_EVERY seconds of movie time.
    """
    f = FLIP_FILTERS.get(FLIP)
    if not f:
        return "[0:v]"
    if FLIP_EVERY <= 0 and FLIP_EVERY != -1:
        return f"[0:v]{f},"
    length = end - start
    spans, on, since = [], FLIP_STATE["on"], 0.0
    for c in scene_cuts(src, start, length):
        if c < 0.2 or start + c - FLIP_STATE["last"] < max(FLIP_EVERY, 0):
            continue
        if on:
            spans.append((since, c))
        on, since = not on, c
        FLIP_STATE.update(on=on, last=start + c)
    if on:
        spans.append((since, length + 1))
    if not spans:
        return "[0:v]"
    if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] > length:
        return f"[0:v]{f},"
    when = "+".join(f"between(t,{a:.3f},{b - 0.001:.3f})" for a, b in spans)
    print(f"  flipped stretches: {[(round(a, 1), round(min(b, length), 1)) for a, b in spans]}", flush=True)
    return f"[0:v]split=2[n][m];[m]{f}[mf];[n][mf]overlay=0:0:enable='{when}',"


CROP = {"filter": "", "w": None, "h": None}   # black bars to cut off, worked out once per movie


def detect_black_bars(src, total):
    """
    Finds black bars that are part of the video itself (e.g. a widescreen film inside a
    16:9 file) by sampling 9 moments. Keeps the biggest picture area seen, so dark
    scenes can't make it cut into the picture.
    """
    CROP.update(filter="", w=None, h=None)
    boxes = []
    for k in range(1, 10):
        t = total * k / 10
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{t:.1f}", "-t", "4", "-i", str(src),
             "-an", "-sn", "-vf", "fps=2,cropdetect=limit=0.1:round=2:reset=0", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", res.stderr)
        if found:
            w, h, x, y = map(int, found[-1])
            if w > 0 and h > 0:
                boxes.append((x, y, x + w, y + h))
    if not boxes:
        return
    st = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "json", str(src)], capture_output=True, text=True).stdout)["streams"][0]
    W, H = int(st["width"]), int(st["height"])
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w * h < 0.97 * W * H and w > W * 0.5 and h > H * 0.3:
        CROP.update(filter=f"crop={w}:{h}:{x0}:{y0},", w=w, h=h)
        print(f"Cutting off black bars: picture is {w}x{h} of {W}x{H}", flush=True)


def movie_bottom(src):
    """Where the bottom edge of the movie sits on the 1080x1920 screen."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,sample_aspect_ratio", "-of", "json", str(src)],
        capture_output=True, text=True,
    ).stdout
    try:
        st = json.loads(out)["streams"][0]
        w, h = float(CROP["w"] or st["width"]), float(CROP["h"] or st["height"])
        sar = st.get("sample_aspect_ratio", "1:1")
        if sar and ":" in sar and not sar.startswith("0"):
            a, b = sar.split(":")
            w *= float(a) / float(b)
        fh = min(1920.0, 1080.0 * h / w)
    except Exception:
        fh = 608.0                     # assume a normal widescreen movie
    return (1920 + fh) / 2


def line_filter(src, length):
    """A short thin black line on the movie, near its bottom, that slowly slides left and right."""
    if not LINE:
        return ";[txt0]null[txt]"
    y = min(1860.0, movie_bottom(src) - 40)
    period = random.uniform(5, 8)
    x = f"(W-w)/2+(W-w)/2*0.75*sin(2*PI*t/{period:.2f}+{random.uniform(0, 6.28):.2f})"
    return (f";color=c=black:s={LINE_WIDTH}x{LINE_THICKNESS}:r=30:d={length:.3f},format=rgba[ln];"
            f"[txt0][ln]overlay=x='{x}':y={y:.0f}:eval=frame[txt]")


def cockroach_filter(length):
    """
    Picks a random moment and a random path across the whole 1080x1920 screen.
    Returns (extra ffmpeg inputs, filter text from [txt] to [v]) or None.
    """
    if not COCKROACH or random.random() > COCKROACH_CHANCE:
        return None
    W, H, margin = 1080, 1920, COCKROACH_SIZE
    def edge_point(edge):
        return {0: (random.uniform(100, W - 100), -margin), 1: (W + margin, random.uniform(300, H - 300)),
                2: (random.uniform(100, W - 100), H + margin), 3: (-margin, random.uniform(300, H - 300))}[edge]
    e = random.randrange(4)
    x0, y0 = edge_point(e)
    x1, y1 = edge_point((e + random.choice([1, 2, 2, 3])) % 4)
    dist = math.hypot(x1 - x0, y1 - y0)
    secs = dist / random.uniform(170, 260)
    if length < secs + 2:
        return None
    t0 = random.uniform(min(1.0, length * 0.15), length - secs - 0.5)
    t1 = t0 + secs
    vx, vy = (x1 - x0) / secs, (y1 - y0) / secs
    ang = math.atan2(y1 - y0, x1 - x0)
    px, py = -math.sin(ang) * 8, math.cos(ang) * 8      # little side-to-side wobble
    wob = f"sin(2*PI*2.2*(t-{t0:.3f}))"
    x = f"{x0:.1f}+{vx:.2f}*(t-{t0:.3f})+{px:.2f}*{wob}-w/2"
    y = f"{y0:.1f}+{vy:.2f}*(t-{t0:.3f})+{py:.2f}*{wob}-h/2"
    when = f"between(t,{t0:.3f},{t1:.3f})"
    sprite = (f"scale={COCKROACH_SIZE}:-1,format=rgba,"
              f"rotate={ang:.4f}:c=none:ow=rotw({ang:.4f}):oh=roth({ang:.4f})")
    filt = (f";[1:v]{sprite}[ra];[2:v]{sprite}[rb];"
            f"[txt][ra]overlay=x='{x}':y='{y}':eval=frame:enable='{when}*lt(mod(t*16,2),1)'[t1];"
            f"[t1][rb]overlay=x='{x}':y='{y}':eval=frame:enable='{when}*gte(mod(t*16,2),1)'[v]")
    inputs = []
    for name in ("roach_a.png", "roach_b.png"):
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{length:.3f}", "-i", str(ASSETS / name)]
    print(f"  cockroach runs across at {t0:.1f}s-{t1:.1f}s of this part", flush=True)
    return inputs, filt


SUB_SIZE = int(os.environ.get("SUBTITLE_SIZE", "54"))   # subtitle text size in pixels


def wrap_line(text, width=30):
    """Splits subtitle text into up to 3 evenly sized lines of about `width` characters."""
    words = text.replace("\n", " ").split()
    total = len(" ".join(words))
    n = min(3, max(1, math.ceil(total / width)))
    target = math.ceil(total / n)
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > target and len(lines) < n - 1:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return [l for l in lines if l]


def subtitle_filters(part_cues, length, bottom, workdir):
    """Burns subtitles in, centred just under the picture (or near the bottom for tall videos)."""
    out = []
    y0 = bottom + 30 if bottom < 1650 else 1920 - 2 * (SUB_SIZE + 14) - 120
    n = 0
    for a, b, text in part_cues:
        a, b = max(0.0, a), min(length, b)
        if b <= a:
            continue
        for k, line in enumerate(wrap_line(text)):
            n += 1
            f = workdir / f"sub{n}.txt"
            f.write_text(line, encoding="utf-8")
            out.append(
                f",drawtext=fontfile={FONT}:textfile={f.name}:expansion=none:fontsize={SUB_SIZE}:"
                f"fontcolor=white:borderw=5:bordercolor=black:x=(w-text_w)/2:"
                f"y={y0 + k * (SUB_SIZE + 14):.0f}:enable='between(t,{a:.3f},{b:.3f})'"
            )
    return "".join(out)


def render_part(src, start, end, number, cues, out_path, workdir):
    # the flip happens first, so "Part X" and subtitles still read the right way round
    first = flip_filter(src, start, end).replace("[0:v]", f"[0:v]{CROP['filter']}", 1)
    filters = (
        f"{first}split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "scale=270:480,boxblur=10:2,scale=1080:1920,eq=brightness=-0.08[bgb];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    # subtitles: your .srt if there is one, otherwise made from this part's audio
    part_cues = [(a - start, b - start, t) for a, b, t in cues if b > start and a < end]
    if not cues and AUTO_SUBS:
        try:
            part_cues = auto_subs(src, start, end, workdir)
        except Exception as e:
            print(f"  (no automatic subtitles for this part: {e})", flush=True)
    filters += subtitle_filters(part_cues, end - start, movie_bottom(src), workdir)
    filters += (
        f",drawtext=fontfile={FONT}:text='Part {number}':fontsize=96:fontcolor=white:"
        "borderw=7:bordercolor=black:x=(w-text_w)/2:y=230"
        "[txt0]"
    )
    filters += line_filter(src, end - start)
    roach = cockroach_filter(end - start)
    extra_inputs = roach[0] if roach else []
    filters += roach[1] if roach else ";[txt]null[v]"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src.resolve()),
        *extra_inputs,
        "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0?",
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ], cwd=workdir)
    for f in workdir.glob("sub*.txt"):
        f.unlink(missing_ok=True)


def process_local(src, name, srt, outdir, workdir, on_plan=None, on_part=None):
    """
    Does all the video work on files already on disk. Returns list of output files.
    on_plan(count, list_file) runs once the parts are chosen; on_part(i, count, file, line)
    runs right after each part is finished, so it can be uploaded straight away.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    reset_flip()
    detect_black_bars(src, duration(src))
    SUB_INFO.update(lang=None, note="from your .srt file" if srt else None)
    cues = parse_srt(srt)
    if MODE == "all":
        total = duration(src)
        candidates = (quiet_moments(src) if has_audio(src) else []) + subtitle_gaps(cues)
        parts = choose_cuts(total, candidates)
    else:
        total, loud, motion, cuts, quiet = analyse(src, workdir)
        candidates = quiet + subtitle_gaps(cues)
        score = interest_scores(loud, motion, cuts, cues)
        if MODE == "stretch":
            parts = choose_stretch(total, score, candidates)
        else:
            parts = choose_highlights(total, score, candidates)

    print(f"{name}: {total/60:.1f} min -> {len(parts)} parts ({MODE} mode)", flush=True)
    outdir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(len(parts))))
    lines = []
    for i, (a, b, sc) in enumerate(parts, 1):
        line = f"Part {i}: {fmt_time(a)} to {fmt_time(b)} ({b - a:.0f}s)"
        if sc is not None:
            line += f"  interest score {sc * 100:.0f}/100"
        print("  " + line, flush=True)
        lines.append(line)
    list_file = outdir / "_parts list.txt"
    list_file.write_text("\n".join([name, f"Mode: {MODE_NAMES.get(MODE, MODE)}", ""] + lines) + "\n", encoding="utf-8")
    if on_plan:
        on_plan(len(parts), list_file)

    files = []
    for i, ((a, b, _), line) in enumerate(zip(parts, lines), 1):
        out = outdir / f"{name} - Part {str(i).zfill(width)}.mp4"
        print(f"Making part {i} of {len(parts)}...", flush=True)
        render_part(src, a, b, i, cues, out, workdir)
        files.append(out)
        if on_part:
            on_part(i, len(parts), out, line)
    return files


# ---------------------------------------------------------------- links (archive.org)
def http_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "movie-splitter"})) as r:
        return json.load(r)


def resolve_link(url):
    """Returns (download_url, nice_name_with_extension)."""
    url = url.strip()
    m = re.search(r"archive\.org/(?:details|download)/([^/?#]+)(?:/([^?#]+))?", url)
    if m and not (m.group(2) and Path(m.group(2)).suffix.lower() in VIDEO_EXT):
        ident = m.group(1)
        meta = http_json(f"https://archive.org/metadata/{ident}")
        vids = [f for f in meta.get("files", []) if Path(f.get("name", "")).suffix.lower() in VIDEO_EXT]
        if not vids:
            raise RuntimeError(f"No video file found on archive.org item '{ident}'")
        # prefer mp4/h.264, then the biggest file
        vids.sort(key=lambda f: (f["name"].lower().endswith(".mp4"), int(f.get("size", 0) or 0)), reverse=True)
        best = vids[0]["name"]
        title = meta.get("metadata", {}).get("title") or ident
        year = str(meta.get("metadata", {}).get("year") or meta.get("metadata", {}).get("date", ""))[:4]
        nice = safe(f"{title} ({year})" if re.fullmatch(r"\d{4}", year or "") and year not in title else title)
        return f"https://archive.org/download/{ident}/{urllib.parse.quote(best)}", nice + Path(best).suffix.lower()
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name) or "video.mp4"
    return url, name


def fetch_link_file(f):
    """Downloads the video(s) in one .txt link file into 'To edit'. Returns their names."""
    text = subprocess.run(["rclone", "cat", f"{REMOTE}{TO_EDIT}/{f}"], capture_output=True, text=True).stdout
    urls = re.findall(r"https?://\S+", text)
    got, ok = [], bool(urls)
    for url in urls:
        try:
            dl, name = resolve_link(url)
            local = WORK / "dl" / name
            local.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading {dl}", flush=True)
            run(["curl", "-fL", "--retry", "3", "-o", str(local), dl])
            run(["rclone", "move", str(local.parent), f"{REMOTE}{TO_EDIT}"])
            got.append(name)
        except Exception as e:
            ok = False
            print(f"FAILED link {url}: {e}", flush=True)
            notify("Link failed", f"Couldn't download {url}\n{e}", ["x"])
    if ok:
        rc_delete(f)
    else:
        rc_move(f, FAILED)
    shutil.rmtree(WORK / "dl", ignore_errors=True)
    return got


# ---------------------------------------------------------------- drive
def is_video(entry):
    return Path(entry["Name"]).suffix.lower() in VIDEO_EXT or entry.get("MimeType", "").startswith("video/")


def is_link_file(entry):
    return entry["Name"].lower().endswith(".txt") and entry["Name"] != FINISHED_LOG


def use_folder(folder):
    """Switches the input/output folders and mode for the next file."""
    global TO_EDIT, READY, MODE
    TO_EDIT, READY, MODE = folder


def list_to_edit():
    """Files in the current 'To edit' folder, oldest first (first come, first served)."""
    out = subprocess.run(
        ["rclone", "lsjson", REMOTE + TO_EDIT, "--files-only"],
        capture_output=True, text=True,
    ).stdout
    return sorted(json.loads(out or "[]"), key=lambda e: e.get("ModTime", ""))


def waiting_everywhere(tried):
    """Every waiting file in every 'To edit' folder, oldest first: [(folder, entry)]."""
    found = []
    for folder in FOLDERS:
        use_folder(folder)
        for e in list_to_edit():
            if (folder[0], e["Name"]) not in tried and (is_video(e) or is_link_file(e)):
                found.append((folder, e))
    return sorted(found, key=lambda fe: fe[1].get("ModTime", ""))


def make_folders():
    for to_edit, ready, _ in FOLDERS:
        subprocess.run(["rclone", "mkdir", REMOTE + to_edit])
        subprocess.run(["rclone", "mkdir", REMOTE + ready])


def rc_move(src_name, dest_folder):
    subprocess.run(["rclone", "moveto", f"{REMOTE}{TO_EDIT}/{src_name}", f"{REMOTE}{dest_folder}/{src_name}"])


def rc_delete(name):
    """Deletes a file from 'To edit'. It goes to Google Drive's bin, so it can be restored for 30 days."""
    subprocess.run(["rclone", "deletefile", "--drive-use-trash=true", f"{REMOTE}{TO_EDIT}/{name}"])


def log_finished(line):
    """Adds a line to 'To edit/Finished.txt' (creates it the first time)."""
    remote = f"{REMOTE}{TO_EDIT}/{FINISHED_LOG}"
    old = subprocess.run(["rclone", "cat", remote], capture_output=True, text=True).stdout
    if not old.strip():
        old = "Finished movies. The original files were deleted (they're in Google Drive's bin for 30 days).\n\n"
    local = Path("finished_log.txt")
    local.write_text(old.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    subprocess.run(["rclone", "copyto", str(local), remote])
    local.unlink(missing_ok=True)


def install_tools():
    run(["sudo", "apt-get", "update", "-qq"])
    run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg", "fonts-dejavu-core"])
    for d in ["/usr/share/dotnet", "/usr/local/lib/android", "/opt/ghc", "/opt/hostedtoolcache/CodeQL"]:
        subprocess.run(["sudo", "rm", "-rf", d])
    run(["df", "-h", "/"])


def process_video(video, all_names):
    stem = Path(video).stem
    srt_name = next((f for f in all_names if f.lower() == f"{stem.lower()}.srt"), None)
    name = clean_name(video)
    dest = f"{REMOTE}{READY}/{name}"
    print(f"\n=== {video} -> {name} ===", flush=True)
    notify(f"Started: {name}",
           "Checking the movie first, then you'll get a message as each part lands in "
           f"'{READY}/{name}'.", ["clapper"])
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "in").mkdir(parents=True)

    def on_plan(count, list_file):
        run(["rclone", "copyto", str(list_file), f"{dest}/{list_file.name}"])
        notify(f"{name}: {count} part{'s' if count != 1 else ''} planned", f"Making Part 1 of {count} now.", ["scissors"])

    captions = []

    def on_part(i, count, out, line):
        run(["rclone", "copyto", str(out), f"{dest}/{out.name}"])
        out.unlink(missing_ok=True)  # free up disk space straight away
        if CAPTIONS_ON and not captions:
            captions.extend(make_captions(count))
        caption = f"\n\n📝 Caption:\n{captions[i - 1]}" if captions else ""
        notify(f"{name}: Part {i} of {count} ready",
               f"{line}\nIt's in {READY}/{name}.{caption}", ["white_check_mark"] if i == count else ["film_frames"])

    try:
        run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{video}", str(WORK / "in"), "--stats-one-line", "--stats", "30s"])
        srt = None
        if srt_name:
            run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{srt_name}", str(WORK / "in")])
            srt = WORK / "in" / srt_name
        files = process_local(WORK / "in" / video, name, srt, WORK / "out", WORK, on_plan, on_part)
        rc_delete(video)
        if srt_name:
            rc_delete(srt_name)
        when = datetime.now(ZoneInfo("Pacific/Auckland")).strftime("%d %b %Y, %I:%M %p")
        log_finished(f"{when}  -  {name}  -  {len(files)} part{'s' if len(files) != 1 else ''} in "
                     f"'{READY}/{name}'  -  subtitles: {SUB_INFO['note'] or 'none (no speech found)'}  "
                     f"(original file: {video})")
        print(f"Done: {name}", flush=True)
        notify(f"✅ Finished: {name}",
               f"{len(files)} part{'s' if len(files) != 1 else ''} in {READY}/{name}.\n"
               f"🗑️ The big movie file ({video}) was deleted to free up space. "
               f"It's in Google Drive's bin for 30 days, and listed in {TO_EDIT}/{FINISHED_LOG}.", ["tada"])
    except Exception as e:
        print(f"FAILED: {video}: {e}", flush=True)
        rc_move(video, FAILED)
        if srt_name:
            rc_move(srt_name, FAILED)
        notify(f"❌ Failed: {name}", f"Moved to the '{FAILED}' folder. Any parts already made are kept.\n{e}", ["x"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def main():
    """
    Keeps going until every 'To edit' folder is empty, always taking the oldest file next,
    so movies added while this is running get picked up in the same run.
    """
    make_folders()
    tools_ready, tried = False, set()
    while True:
        entries = waiting_everywhere(tried)
        if not entries:
            if not tools_ready:
                print("Nothing in any 'To edit' folder.")
            return
        if (time.time() - STARTED) / 60 > MAX_RUN_MINUTES:
            print("Time limit reached; the rest will be done in the next run.")
            notify("Paused for now", f"{len(entries)} file(s) still waiting. They'll be done in the next run.")
            return
        if not tools_ready:
            install_tools()
            tools_ready = True

        folder, nxt = entries[0]
        use_folder(folder)
        tried.add((folder[0], nxt["Name"]))
        all_names = [e["Name"] for e in list_to_edit()]
        if is_link_file(nxt):
            for video in fetch_link_file(nxt["Name"]):  # a link's movie goes next, keeping its place in line
                tried.add((folder[0], video))
                process_video(video, all_names + [video])
        else:
            process_video(nxt["Name"], all_names)


if __name__ == "__main__":
    # Local test: python3 process.py --local video.mp4 [subs.srt]
    if len(sys.argv) > 2 and sys.argv[1] == "--local":
        src = Path(sys.argv[2])
        srt = Path(sys.argv[3]) if len(sys.argv) > 3 else None
        process_local(src, clean_name(src.name), srt, Path("out"), WORK)
    else:
        main()
