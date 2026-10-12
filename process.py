"""
TikTok part maker: runs on GitHub Actions (your PC can stay off).

Google Drive layout (inside your "TikTok automation" folder):
    To edit/        <- drop videos here (and an optional .srt with the same name),
                       OR a .txt file containing an archive.org link
    Ready to post/  <- finished parts appear here, one subfolder per video
    Done/           <- originals are moved here after processing
    Failed/         <- anything that couldn't be processed

Two modes (set MODE in the workflow file):
  highlights  Scores the whole video for "interesting" moments (loud audio,
              sudden sound spikes, fast scene cuts, lots of movement, busy
              dialogue) and keeps only the best stretches as Part 1, Part 2...
  all         Cuts the whole video into ~PART_SECONDS parts.

Either way, every cut lands on a quiet moment (a pause) where possible, each part
is made vertical 9:16 (1080x1920) with a blurred background, gets "Part X" at the
top, and gets burned-in subtitles if a matching .srt exists.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REMOTE = "gdrive:"
TO_EDIT = "To edit"
READY = "Ready to post"
DONE = "Done"
FAILED = "Failed"

MODE = os.environ.get("MODE", "highlights").strip().lower()
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


def choose_cuts(total, candidates):
    """'all' mode: back-to-back parts of ~PART_SECONDS. Returns [(start, end, None)]."""
    candidates = sorted(candidates)
    cuts, pos = [], 0.0
    while total - pos > PART_SECONDS + LATEST:
        target = pos + PART_SECONDS
        cut = snap(target, candidates, EARLIEST, LATEST)
        cuts.append((pos, cut, None))
        pos = cut
    if cuts and total - pos < MIN_LAST_PART:
        cuts[-1] = (cuts[-1][0], total, None)
    else:
        cuts.append((pos, total, None))
    return cuts


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


def render_part(src, start, end, number, cues, out_path, workdir):
    # the flip happens first, so "Part X" and subtitles still read the right way round
    filters = (
        f"{flip_filter(src, start, end)}split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "scale=270:480,boxblur=10:2,scale=1080:1920,eq=brightness=-0.08[bgb];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    sub_file = workdir / "part.srt"
    if cues and write_part_srt(cues, start, end, sub_file):
        filters += (
            ",subtitles=part.srt:original_size=1080x1920:force_style="
            "'FontName=DejaVu Sans,Bold=1,Fontsize=12,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=60'"
        )
    filters += (
        f",drawtext=fontfile={FONT}:text='Part {number}':fontsize=96:fontcolor=white:"
        "borderw=7:bordercolor=black:x=(w-text_w)/2:y=230"
        "[v]"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src.resolve()),
        "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0?",
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ], cwd=workdir)
    sub_file.unlink(missing_ok=True)


def process_local(src, name, srt, outdir, workdir, on_plan=None, on_part=None):
    """
    Does all the video work on files already on disk. Returns list of output files.
    on_plan(count, list_file) runs once the parts are chosen; on_part(i, count, file, line)
    runs right after each part is finished, so it can be uploaded straight away.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    reset_flip()
    cues = parse_srt(srt)
    if MODE == "all":
        total = duration(src)
        candidates = (quiet_moments(src) if has_audio(src) else []) + subtitle_gaps(cues)
        parts = choose_cuts(total, candidates)
    else:
        total, loud, motion, cuts, quiet = analyse(src, workdir)
        candidates = quiet + subtitle_gaps(cues)
        parts = choose_highlights(total, interest_scores(loud, motion, cuts, cues), candidates)

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
    list_file.write_text("\n".join([name, f"Mode: {MODE}", ""] + lines) + "\n", encoding="utf-8")
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
    rc_move(f, DONE if ok else FAILED)
    shutil.rmtree(WORK / "dl", ignore_errors=True)
    return got


# ---------------------------------------------------------------- drive
def is_video(entry):
    return Path(entry["Name"]).suffix.lower() in VIDEO_EXT or entry.get("MimeType", "").startswith("video/")


def is_link_file(entry):
    return entry["Name"].lower().endswith(".txt")


def list_to_edit():
    """Files in 'To edit', oldest first (first come, first served)."""
    out = subprocess.run(
        ["rclone", "lsjson", REMOTE + TO_EDIT, "--files-only"],
        capture_output=True, text=True, check=True,
    ).stdout
    return sorted(json.loads(out or "[]"), key=lambda e: e.get("ModTime", ""))


def rc_move(src_name, dest_folder):
    subprocess.run(["rclone", "moveto", f"{REMOTE}{TO_EDIT}/{src_name}", f"{REMOTE}{dest_folder}/{src_name}"])


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

    def on_part(i, count, out, line):
        run(["rclone", "copyto", str(out), f"{dest}/{out.name}"])
        out.unlink(missing_ok=True)  # free up disk space straight away
        notify(f"{name}: Part {i} of {count} ready",
               f"{line}\nIt's in {READY}/{name}.", ["white_check_mark"] if i == count else ["film_frames"])

    try:
        run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{video}", str(WORK / "in"), "--stats-one-line", "--stats", "30s"])
        srt = None
        if srt_name:
            run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{srt_name}", str(WORK / "in")])
            srt = WORK / "in" / srt_name
        files = process_local(WORK / "in" / video, name, srt, WORK / "out", WORK, on_plan, on_part)
        rc_move(video, DONE)
        if srt_name:
            rc_move(srt_name, DONE)
        print(f"Done: {name}", flush=True)
        notify(f"✅ Finished: {name}", f"{len(files)} part{'s' if len(files) != 1 else ''} in {READY}/{name}.", ["tada"])
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
    Keeps going until 'To edit' is empty, always taking the oldest file next,
    so movies added while this is running get picked up in the same run.
    """
    tools_ready, tried = False, set()
    while True:
        entries = [e for e in list_to_edit() if e["Name"] not in tried and (is_video(e) or is_link_file(e))]
        if not entries:
            if not tools_ready:
                print("Nothing in 'To edit'.")
            return
        if (time.time() - STARTED) / 60 > MAX_RUN_MINUTES:
            print("Time limit reached; the rest will be done in the next run.")
            notify("Paused for now", f"{len(entries)} file(s) still waiting. They'll be done in the next run.")
            return
        if not tools_ready:
            install_tools()
            tools_ready = True

        nxt = entries[0]
        tried.add(nxt["Name"])
        all_names = [e["Name"] for e in list_to_edit()]
        if is_link_file(nxt):
            for video in fetch_link_file(nxt["Name"]):  # a link's movie goes next, keeping its place in line
                tried.add(video)
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
