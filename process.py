"""
TikTok part maker: runs on GitHub Actions (your PC can stay off).

Google Drive layout (inside your "TikTok automation" folder):
    To edit/        <- drop videos here (and an optional .srt with the same name)
    Ready to post/  <- finished parts appear here, one subfolder per video
    Done/           <- originals are moved here after processing
    Failed/         <- anything that couldn't be processed

For each video in "To edit" it:
  1. Finds cut points about every PART_SECONDS, choosing a quiet moment
     (a pause in speech) near each target so parts don't end mid-sentence.
  2. Makes each part vertical 9:16 (1080x1920): the video in the middle,
     a blurred copy filling the background.
  3. Adds "Part X" at the top and burns in subtitles if a matching .srt exists.
  4. Uploads parts to "Ready to post/<Video name>/" and moves the original to "Done".
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REMOTE = "gdrive:"
TO_EDIT = "To edit"
READY = "Ready to post"
DONE = "Done"
FAILED = "Failed"

PART_SECONDS = float(os.environ.get("PART_SECONDS", "120"))
EARLIEST = float(os.environ.get("CUT_EARLIEST", "25"))  # may cut up to this many seconds early...
LATEST = float(os.environ.get("CUT_LATEST", "15"))      # ...or this many seconds late, to land on a pause
MIN_LAST_PART = 30                                      # a shorter leftover gets added to the previous part
FONT = os.environ.get("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
WORK = Path("work")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".webm", ".mpg", ".mpeg", ".ts"}
JUNK = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|10bit|bluray|blu ray|brrip|bdrip|webrip|web dl|web|"
    r"hdtv|dvdrip|hdrip|x264|x265|h264|h265|hevc|aac|ac3|dts|proper|repack|extended|remastered)\b.*$",
    re.I,
)


# ---------------------------------------------------------------- helpers
def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


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
    return re.sub(r'[\\/:*?"<>|%]', "", name).strip()


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


# ---------------------------------------------------------------- cut points
def quiet_moments(path):
    """Middle of every pause (>= 0.4s below -35dB) in the audio, in seconds."""
    if not has_audio(path):
        return []
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", res.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def subtitle_gaps(srt_path):
    """Gaps between subtitle lines are also good places to cut."""
    if not srt_path:
        return []
    cues = parse_srt(srt_path)
    return [(a[1] + b[0]) / 2 for a, b in zip(cues, cues[1:]) if b[0] - a[1] >= 0.3]


def choose_cuts(total, candidates):
    """Returns [(start, end), ...]"""
    candidates = sorted(candidates)
    cuts, pos = [], 0.0
    while total - pos > PART_SECONDS + LATEST:
        target = pos + PART_SECONDS
        window = [c for c in candidates if target - EARLIEST <= c <= target + LATEST]
        cut = min(window, key=lambda c: abs(c - target)) if window else target
        cuts.append((pos, cut))
        pos = cut
    if cuts and total - pos < MIN_LAST_PART:
        cuts[-1] = (cuts[-1][0], total)
    else:
        cuts.append((pos, total))
    return cuts


# ---------------------------------------------------------------- subtitles
def ts_to_sec(ts):
    h, m, rest = ts.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def sec_to_ts(s):
    s = max(0.0, s)
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def parse_srt(path):
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


# ---------------------------------------------------------------- render
def render_part(src, start, end, number, srt_cues, out_path, workdir):
    filters = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "scale=270:480,boxblur=10:2,scale=1080:1920,eq=brightness=-0.08[bgb];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
    sub_file = workdir / "part.srt"
    if srt_cues and write_part_srt(srt_cues, start, end, sub_file):
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
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
        "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src.resolve()),
        "-filter_complex", filters, "-map", "[v]", "-map", "0:a:0?",
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    run(cmd, cwd=workdir)
    sub_file.unlink(missing_ok=True)


# ---------------------------------------------------------------- drive
def list_to_edit():
    out = subprocess.run(
        ["rclone", "lsf", REMOTE + TO_EDIT, "--files-only"],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()


def rc_move(src_name, dest_folder):
    subprocess.run(["rclone", "moveto", f"{REMOTE}{TO_EDIT}/{src_name}", f"{REMOTE}{dest_folder}/{src_name}"])


def process_local(src, name, srt, outdir):
    """Does all the video work on files already on disk. Returns list of part files."""
    total = duration(src)
    cues = parse_srt(srt) if srt else []
    candidates = quiet_moments(src) + subtitle_gaps(srt)
    parts = choose_cuts(total, candidates)
    print(f"{name}: {total/60:.1f} min -> {len(parts)} parts "
          f"({len(candidates)} quiet moments found{', with subtitles' if cues else ''})", flush=True)
    outdir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(len(parts))))
    files = []
    for i, (a, b) in enumerate(parts, 1):
        out = outdir / f"{name} - Part {str(i).zfill(width)}.mp4"
        print(f"  Part {i}: {a:.1f}s -> {b:.1f}s ({b - a:.0f}s)", flush=True)
        render_part(src, a, b, i, cues, out, WORK)
        files.append(out)
    return files


def install_tools():
    run(["sudo", "apt-get", "update", "-qq"])
    run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg", "fonts-dejavu-core"])
    for d in ["/usr/share/dotnet", "/usr/local/lib/android", "/opt/ghc", "/opt/hostedtoolcache/CodeQL"]:
        subprocess.run(["sudo", "rm", "-rf", d])
    run(["df", "-h", "/"])


def main():
    files = list_to_edit()
    videos = [f for f in files if Path(f).suffix.lower() in VIDEO_EXT]
    if not videos:
        print("Nothing in 'To edit'.")
        return
    print(f"Found {len(videos)} video(s).")
    install_tools()

    for video in videos:
        stem = Path(video).stem
        srt_name = next((f for f in files if f.lower() == f"{stem.lower()}.srt"), None)
        name = clean_name(video)
        print(f"\n=== {video} -> {name} ===", flush=True)
        shutil.rmtree(WORK, ignore_errors=True)
        (WORK / "in").mkdir(parents=True)
        try:
            run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{video}", str(WORK / "in"), "--stats-one-line", "--stats", "30s"])
            srt = None
            if srt_name:
                run(["rclone", "copy", f"{REMOTE}{TO_EDIT}/{srt_name}", str(WORK / "in")])
                srt = WORK / "in" / srt_name
            process_local(WORK / "in" / video, name, srt, WORK / "out")
            run(["rclone", "copy", str(WORK / "out"), f"{REMOTE}{READY}/{name}", "--stats-one-line", "--stats", "30s"])
            rc_move(video, DONE)
            if srt_name:
                rc_move(srt_name, DONE)
            print(f"Done: {name}", flush=True)
        except Exception as e:
            print(f"FAILED: {video}: {e}", flush=True)
            rc_move(video, FAILED)
            if srt_name:
                rc_move(srt_name, FAILED)
        finally:
            shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    # Local test: python3 process.py --local video.mp4 [subs.srt]
    if len(sys.argv) > 2 and sys.argv[1] == "--local":
        src = Path(sys.argv[2])
        srt = Path(sys.argv[3]) if len(sys.argv) > 3 else None
        WORK.mkdir(exist_ok=True)
        process_local(src, clean_name(src.name), srt, Path("out"))
    else:
        main()
