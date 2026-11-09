#!/usr/bin/env python3
"""Build a 9:16 Instagram Reel (MP4) from output/<slug>/slides.json, with a spoken narration.

    python scripts/render_reel.py <slug> [--voice "Microsoft Mark"] [--rate 0] [--workers 4]

Narration: each slide is read aloud by the Windows voice (scripts/speak.ps1). The text is the
slide's headline or heading, then its body (or subtitle, or call to action). A slide can set its
own spoken text with a "narration" string in slides.json. No network, no keys.

Narration is matched line by line: each text line on a slide (the headline or heading, then each body,
subtitle or CTA line) must be exactly one sentence of its narration, in the same order. The renderer
stops if the counts differ. Each line is written on during its own sentence.

Timing follows the narration:
- The narration's length is split across its sentences by character count. Each line is written on
  over 90% of its sentence's share, so each line lands as its sentence is said. This is an estimate:
  the voice gives no word timestamps, so lines may still drift a little within a sentence.
- The slide lasts the spoken length plus 0.8 s, with a 4 s floor. There is no upper limit on length.
- Motion: the finished slide zooms in slowly, fades in over 0.6 s, and rises 60 px into place.
- Slides cross-fade over 0.5 s. Each slide's voice starts with its own slide.

Slides are rendered in parallel: each worker process runs its own headless Chromium, so several
slides are screenshotted at once.

Output: output/<slug>/reel.mp4 (H.264, AAC, 1080x1920, 30 fps). Frames and speech files are kept in
output/<slug>/html/reel/ (gitignored). Needs ffmpeg on PATH, or the winget install.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, wait
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import render_carousel as rc  # noqa: E402  (shared copy checks, fit routine and fonts)

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from markupsafe import Markup  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROJECT_ROOT = SCRIPT_DIR.parents[3]          # scripts -> skill -> skills -> .claude -> project
OUTPUT_ROOT = PROJECT_ROOT / "output"
FPS = 30
W, H = 1080, 1920
WRITE_SHARE = 0.9   # each line is finished at 90% of its own sentence's share of the narration
TAIL = 0.8          # seconds a finished slide stays after the voice stops
MIN_SEC = 4.0       # floor only: a slide is never shorter than 4 s. There is no upper limit on length.
DEFAULT_RATE = 0    # normal speaking speed (Kokoro speed 1.0)
FADE = 0.5          # cross-fade between slides
RISE = 0.8          # seconds for the slide to rise into place
PAPER = "0x0B0B0B"
DEFAULT_VOICE = "Microsoft Mark"
DEFAULT_WORKERS = min(4, os.cpu_count() or 2)   # each worker runs its own Chromium and uses a lot of memory; 4 is the default cap
Stop = rc.Stop


def find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    pattern = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                           "Gyan.FFmpeg*", "*", "bin", f"{name}.exe")
    hits = sorted(glob.glob(pattern))
    if hits:
        return hits[-1]
    raise Stop(f"{name} not found. Install ffmpeg (winget install Gyan.FFmpeg) or add it to PATH.")


# ------------------------------------------------------------------ narration

def plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "")).strip()


def narration_for(slide: dict) -> str:
    """The explanation spoken over a slide: the slide's 'narration' field, written as a spoken explainer.

    The slide text is not read out. Every slide must carry its own narration, so the voice explains
    the idea instead of reading the words on screen.
    """
    text = slide.get("narration")
    if not isinstance(text, str) or not text.strip():
        return ""
    return plain(text)


def sentences(text: str) -> list[str]:
    """Split narration into sentences at . ! or ? followed by a space or the end."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def text_lines(slide: dict) -> list[str]:
    """The on-screen text blocks in reading order: the headline or heading, then its lines."""
    role = slide.get("role")
    if role == "cover":
        return [slide.get("headline", "")] + list(slide.get("subtitle") or [])
    if role == "closing":
        return [slide.get("headline", "")] + ([slide["cta"]] if slide.get("cta") else [])
    return [slide.get("heading", "")] + list(slide.get("body") or [])


def line_windows(narr: float, sents: list[str]) -> list[tuple[float, float]]:
    """When each line is written, in seconds from the slide start: its own sentence's share of the narration."""
    lengths = [max(1, len(s)) for s in sents]
    total = sum(lengths)
    windows, cum = [], 0.0
    for length in lengths:
        share = narr * length / total
        windows.append((cum, cum + share * WRITE_SHARE))
        cum += share
    return windows


MAX_SECONDS = 90.0   # the Reel may run up to 90 seconds; the opening hook must state a time it fits
MAX_KEYWORDS = 4   # keywords per slide: enough to follow the voice, few enough to read
KEYWORD_LEAD = 1.5 # seconds a keyword and its picture are on screen before the voice says the word


def keyword_times(slide: dict, n: int, narr: float, staging_post: Path) -> list[tuple[str, float, str]]:
    """Each keyword appears when the voice says it.

    A keyword must be a word or phrase from the slide's own narration. Its time is the point in the
    narration where it is spoken, taken as its share of the spoken length. Its picture must exist under
    the post's assets/. Returns (word, start seconds, figure path), in spoken order.
    """
    text = narration_for(slide)
    keys = slide.get("keywords") or []
    if len(keys) > MAX_KEYWORDS:
        raise Stop(f"slide {n:02d}: {len(keys)} keywords; at most {MAX_KEYWORDS} per slide.")
    out = []
    for k in keys:
        word = str(k.get("word", "")).strip()
        rel = k.get("figure")
        if not word or not rel:
            raise Stop(f"slide {n:02d}: each keyword needs a \"word\" and a \"figure\".")
        pos = text.lower().find(word.lower())
        if pos < 0:
            raise Stop(f"slide {n:02d}: keyword \"{word}\" is not in this slide's narration. "
                       "Pick keywords the voice actually says.")
        rc.check_figure(rel, staging_post, n)   # stops if the picture is missing or broken
        spoken = narr * pos / max(1, len(text))
        start = max(0.0, spoken - KEYWORD_LEAD)   # on screen a moment before the voice says it
        out.append((word, start, rel))
    out.sort(key=lambda item: item[1])
    return out


def speak(text: str, wav: Path, engine: str, voice: str, rate: int) -> None:
    txt = wav.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    if engine == "kokoro":
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "speak_kokoro.py"), "--text-file", str(txt),
                            "--out", str(wav), "--voice", voice, "--speed", str(1.0 + rate / 20)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not wav.exists():
            raise Stop(f"Kokoro speech failed for {wav.name}: {r.stdout[-300:]} {r.stderr[-300:]}")
        return
    script = SCRIPT_DIR / "speak.ps1"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                        "-TextFile", str(txt), "-OutFile", str(wav), "-Voice", voice, "-Rate", str(rate)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not wav.exists():
        raise Stop(f"speech failed for {wav.name}: {r.stderr[-400:] or r.stdout[-400:]}")


def wav_seconds(wav: Path, ffprobe: str) -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", str(wav)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise Stop(f"could not read the length of {wav.name}")


# ------------------------------------------------------------------ frames

def cover_title_for(copy: dict) -> str:
    """The Reel cover text: the topic only, e.g. 'Understand LLM in 1 min'. slides.json may set 'cover_title' instead."""
    custom = copy.get("cover_title")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    topic = str(copy.get("topic", "")).strip()
    if not topic:
        raise Stop("slides.json needs a 'topic' (a short name, e.g. LLM) for the Reel cover")
    return f"Understand {topic} in 1 min"


def render_one_slide(job: tuple) -> tuple[str, float]:
    """Worker: screenshot every frame of one slide while each line is written on in its own sentence.

    Runs in its own process with its own Chromium, so several slides render at once.
    Returns (frame folder, seconds shown).
    """
    staging_post_s, n, windows, seconds, kws = job
    staging_post = Path(staging_post_s)
    copy = rc.load_copy(staging_post / "slides.json")
    views, _ = rc.build_view(copy, staging_post)
    view = views[n - 1]
    if n == 1:
        # The Reel cover text is the topic only, e.g. "Understand LLM in 1 min".
        view = {**view, "headline_html": Markup(html.escape(cover_title_for(copy)))}
    # Keywords: each one's picture is checked like any figure, then passed to the template.
    # Keywords sit in a row across the stage, in spoken order. Each one links to the one before it.
    ny = 110 if copy["slides"][n - 1].get("role") == "cover" else 150
    xs = [540.0] if len(kws) == 1 else [190 + i * (700 / (len(kws) - 1)) for i in range(len(kws))]
    kw_view = []
    for i, ((w, _, rel), x) in enumerate(zip(kws, xs)):
        prev = xs[i - 1] if i else None
        kw_view.append({"word": w, "figure_uri": rc.check_figure(rel, staging_post, n), "x": round(x),
                        "link_from": None if prev is None else round(prev),
                        "link_len": 0 if prev is None else round(x - prev)})
    kw_starts = [start for _, start, _ in kws]
    fonts = rc.check_fonts()
    logo_path = rc.DEFAULT_LOGO
    aspect = rc.check_logo(logo_path)
    logo_uri = logo_path.resolve().as_uri()
    logo_w = round(rc.LOGO_HEIGHT_PX * aspect)
    env = Environment(loader=FileSystemLoader(rc.TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    tpl = env.get_template("slide.html")

    blocked: list[str] = []
    frames_dir = staging_post / "frames" / f"s{n:02d}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=rc.CHROME_ARGS)
        try:
            context = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)

            def guard(route):
                url = route.request.url
                if url.startswith(("http://", "https://")):
                    blocked.append(url)
                    return route.abort()
                return route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            html_text = tpl.render(slide=view, fonts=fonts, logo_uri=logo_uri, logo_w=logo_w, H=H, kw=kw_view, kw_ny=ny)
            html_path = staging_post / f"{n:02d}.html"
            html_path.write_text(html_text, encoding="utf-8")
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            rc.wait_for_fonts(page)
            rc.wait_for_images(page, n)
            if blocked:
                raise Stop(f"slide {n:02d} tried to load from the network ({blocked[0]}).")
            # Fit check runs with the full copy on the page, so the copy is still checked.
            report = page.evaluate("window.__fit()")
            if report["problems"]:
                raise Stop(f"slide {n:02d}: " + "; ".join(report["problems"]))
            # Reel: the voice carries the explanation. On-screen body, subtitle and CTA text is hidden,
            # so nothing is typed out at the bottom like captions. The headline and figure stay.
            # The root .slide element also carries the class "body", so the selector is scoped to the text stack.
            page.add_style_tag(content=".stack .body { display: none !important; }")
            beats = [start for start, _ in windows]
            for f in range(round(seconds * FPS)):
                t = f / FPS
                page.evaluate(f"window.__frame({t:.4f}, {json.dumps(beats)}, {json.dumps(kw_starts)})")
                page.screenshot(path=str(frames_dir / f"{f:04d}.png"), type="png")
        finally:
            browser.close()
    return str(frames_dir), seconds


# ------------------------------------------------------------------ progress

# The progress page (progress.html, copied next to the frames) reads progress.js, which this writes.
# Weights say how much of the whole run each stage is worth, so the overall bar moves steadily.
PROGRESS_WEIGHTS = {"voice": 15, "frames": 70, "encode": 10, "qa": 5}
_LAST: dict = {}
_LAST_DIR: Path | None = None


def overall_percent(p: dict) -> float:
    if p.get("state") == "done":
        return 100.0
    total = 0.0
    v = p.get("voice") or {}
    if v.get("total"):
        total += PROGRESS_WEIGHTS["voice"] * v["done"] / v["total"]
    slides = p.get("slides") or []
    if slides:
        done = sum(s["frames_done"] for s in slides)
        all_frames = max(1, sum(s["frames_total"] for s in slides))
        total += PROGRESS_WEIGHTS["frames"] * done / all_frames
    if p.get("encode") == "done":
        total += PROGRESS_WEIGHTS["encode"]
    elif p.get("encode") == "running":
        total += PROGRESS_WEIGHTS["encode"] / 2
    total += PROGRESS_WEIGHTS["qa"] * min(1, len(p.get("qa") or []) / max(1, len(p.get("slides") or [1])))
    return round(min(100.0, total), 1)


def write_progress(staging_post: Path, p: dict) -> None:
    """Write progress.js beside progress.html. The page reloads it every 2 seconds."""
    global _LAST, _LAST_DIR
    staging_post.mkdir(parents=True, exist_ok=True)
    page = staging_post / "progress.html"
    if not page.exists():
        shutil.copy2(rc.TEMPLATE_DIR / "progress.html", page)
    p = dict(p)
    p["updated"] = int(time.time() * 1000)
    p["overall"] = overall_percent(p)
    _LAST, _LAST_DIR = p, staging_post
    # Progress must never stop the render. The browser may hold progress.js open for a moment, which blocks
    # a write on Windows, so retry briefly and then carry on; the next update will try again.
    text = "window.REEL_PROGRESS = " + json.dumps(p) + ";\n"
    for attempt in range(5):
        try:
            (staging_post / "progress.js").write_text(text, encoding="utf-8")
            return
        except OSError:
            time.sleep(0.2 * (attempt + 1))
    print("warning: could not update progress.js (the page may be holding it); the render continues")


def mark_failed(message: str) -> None:
    """Show the failure on the progress page, with the message that stopped the run."""
    if _LAST and _LAST_DIR is not None:
        _LAST["state"] = "failed"
        _LAST["message"] = "Stopped"
        _LAST["error"] = message
        write_progress(_LAST_DIR, _LAST)


def render_slides(staging_post: Path, timing: list[tuple[list[tuple[float, float]], float, list]],
                  workers: int, progress: dict) -> list[tuple[Path, float]]:
    """Render all slides, several at a time. Results come back in slide order.

    While the workers run, the main process counts the frames each slide has written and updates
    progress.js once a second, so the progress page can show how far each slide has got.
    """
    copy = rc.load_copy(staging_post / "slides.json")
    _, warnings = rc.build_view(copy, staging_post)
    for w in warnings:
        print(f"warning: {w}")
    jobs = [(str(staging_post), n, windows, seconds, kws)
            for n, (windows, seconds, kws) in enumerate(timing, 1)]
    progress["stage"] = "frames"
    progress["slides"] = [{"n": n, "frames_done": 0, "frames_total": round(seconds * FPS)}
                          for n, (_, seconds, _) in enumerate(timing, 1)]
    progress["message"] = f"Rendering {len(jobs)} slides, {workers} at a time"
    write_progress(staging_post, progress)

    futures = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(render_one_slide, job) for job in jobs]
        pending = set(futures)
        while pending:
            _, pending = wait(pending, timeout=1.0)
            for i, s in enumerate(progress["slides"]):
                folder = staging_post / "frames" / f"s{i + 1:02d}"
                counted = len(list(folder.glob("*.png"))) if folder.exists() else 0
                s["frames_done"] = min(s["frames_total"], counted)
            write_progress(staging_post, progress)
        results: list[tuple[Path, float]] = []
        for n, fut in enumerate(futures, 1):
            frames, seconds = fut.result()   # re-raises a Stop from the worker
            print(f"slide {n:02d}: rendered {seconds:.1f} s of frames")
            results.append((Path(frames), seconds))
    for s in progress["slides"]:
        s["frames_done"] = s["frames_total"]
    write_progress(staging_post, progress)
    return results


# ------------------------------------------------------------------ encode

def encode_clips(post_dir: Path, slides: list[tuple[Path, float]], wavs: list[Path], ffmpeg: str,
                 progress: dict | None = None, staging_post: Path | None = None) -> Path:
    """Encode one clip per slide, then join the clips without re-encoding.

    One slide at a time keeps memory low: only one slide's frames are open in ffmpeg at once (joining
    all seven in one job ran out of memory). Each clip fades in and out, so slides meet in a short fade
    through the background colour. A slide's voice lives in its own clip, so it cannot drift into the next.
    """
    clips_dir = post_dir / "html" / "reel" / "clips"   # gitignored with html/
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, (frames_dir, seconds) in enumerate(slides):
        clip = clips_dir / f"clip{i + 1:02d}.mp4"
        fade_at = max(0.0, seconds - FADE)
        fc = (f"[0:v]zoompan=z='min(zoom+0.00035,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d=1:s={W}x{H}:fps={FPS},format=rgba,fade=t=in:st=0:d=0.6:alpha=1[fg];"
              f"color=c={PAPER}:s={W}x{H}:r={FPS}:d={seconds:.3f}[bg];"
              f"[bg][fg]overlay=0:'if(lt(t,{RISE}),60*(1-t/{RISE}),0)':eval=frame,"
              f"format=yuv420p,fade=t=out:st={fade_at:.3f}:d={FADE}[v];"
              f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
              f"afade=t=out:st={fade_at:.3f}:d={FADE},apad[a]")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(FPS), "-i", str(frames_dir / "%04d.png"), "-i", str(wavs[i]),
               "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{seconds:.3f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "192k", str(clip)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not clip.exists():
            raise Stop(f"ffmpeg failed on slide {i + 1:02d}:\n{r.stderr[-800:]}")
        clips.append(clip)
        if progress is not None and staging_post is not None:
            progress["message"] = f"Encoded slide {i + 1} of {len(slides)}"
            write_progress(staging_post, progress)

    listing = clips_dir / "clips.txt"
    listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    out_path = post_dir / "reel.mp4"
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out_path)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out_path.exists():
        raise Stop(f"ffmpeg failed while joining the clips:\n{r.stderr[-800:]}")
    return out_path


# ------------------------------------------------------------------ build

def build(slug: str, engine: str, voice: str, rate: int, workers: int) -> Path:
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
        raise Stop(f"slug {slug!r} must be lowercase words joined by hyphens")
    post_dir = OUTPUT_ROOT / slug
    slides_json = post_dir / "slides.json"
    if not slides_json.exists():
        raise Stop(f"{slides_json} not found. Write the copy first.")
    staging_post = post_dir / "html" / "reel" / slug
    staging_post.mkdir(parents=True, exist_ok=True)
    shutil.copy2(slides_json, staging_post / "slides.json")
    if (post_dir / "assets").exists():
        shutil.copytree(post_dir / "assets", staging_post / "assets", dirs_exist_ok=True)

    copy = json.loads(slides_json.read_text(encoding="utf-8"))
    ffprobe = find_tool("ffprobe")
    wavs, timing = [], []
    progress = {"slug": slug, "state": "running", "stage": "voice",
                "voice": {"done": 0, "total": len(copy["slides"])}, "slides": [],
                "encode": "pending", "qa": [], "message": "Writing voice for each slide", "error": None}
    write_progress(staging_post, progress)
    missing = [n for n, slide in enumerate(copy["slides"], 1) if not narration_for(slide)]
    if missing:
        raise Stop("these slides have no \"narration\" in slides.json: "
                   + ", ".join(f"{n:02d}" for n in missing)
                   + ". Write a spoken explanation for each slide, then run again.")
    # The narration is free-form: any number of sentences. Sentences only mark beats for the motion.
    sentence_lists = {}
    for n, slide in enumerate(copy["slides"], 1):
        sents = sentences(narration_for(slide)) or [narration_for(slide)]
        sentence_lists[n] = sents
    for n, slide in enumerate(copy["slides"], 1):
        text = narration_for(slide)
        wav = staging_post / f"voice{n:02d}.wav"
        speak(text, wav, engine, voice, rate)
        narr = wav_seconds(wav, ffprobe)
        seconds = max(MIN_SEC, narr + TAIL)
        wavs.append(wav)
        kws = keyword_times(slide, n, narr, staging_post)
        timing.append((line_windows(narr, sentence_lists[n]), seconds, kws))
        print(f"slide {n:02d} voice: {narr:.1f} s, {len(sentence_lists[n])} lines")
        progress["voice"]["done"] = n
        progress["message"] = f"Voice written for slide {n} of {len(copy['slides'])}"
        write_progress(staging_post, progress)

    total = sum(seconds for _, seconds, _ in timing)
    if total > MAX_SECONDS:
        raise Stop(f"the Reel would run {total:.0f} s; the limit is {MAX_SECONDS:.0f} s. "
                   "Shorten the narration of some slides, then run again.")
    print(f"reel length: {total:.1f} s (limit {MAX_SECONDS:.0f} s)")
    slides = render_slides(staging_post, timing, workers, progress)
    export_cover(slides[0][0], post_dir / "reel-cover.jpg")
    progress["stage"], progress["encode"] = "encode", "running"
    progress["message"] = "Joining slides and voice into the video"
    write_progress(staging_post, progress)
    out = encode_clips(post_dir, slides, wavs, find_tool("ffmpeg"), progress, staging_post)
    progress["encode"] = "done"
    progress["stage"] = "qa"
    progress["message"] = "Checking the video frame by frame"
    write_progress(staging_post, progress)
    qa_reel(out, [t[1] for t in timing], find_tool("ffmpeg"), staging_post, progress)
    progress["state"], progress["stage"] = "done", "done"
    progress["message"] = f"Finished: {out}"
    write_progress(staging_post, progress)
    return out


def qa_reel(mp4: Path, durations: list[float], ffmpeg: str, scratch: Path, progress: dict | None = None) -> None:
    """Check the finished Reel: sample the middle of every slide and stop on a near-black or blank frame."""
    from PIL import Image, ImageStat

    starts, played = [], 0.0   # clips are joined back to back, so each slide starts where the last one ends
    for d in durations:
        starts.append(played)
        played += d
    for i, (start, d) in enumerate(zip(starts, durations), 1):
        t = start + d / 2
        png = scratch / f"qa{i:02d}.png"
        r = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mp4), "-frames:v", "1",
                            "-vf", "scale=108:192", str(png)], capture_output=True, text=True)
        if r.returncode != 0 or not png.exists():
            raise Stop(f"QA: could not sample slide {i:02d} at {t:.1f} s")
        mean = ImageStat.Stat(Image.open(png).convert("L")).mean[0]
        print(f"QA slide {i:02d} at {t:5.1f} s: brightness {mean:.0f}")
        if progress is not None:
            progress["qa"].append({"n": i, "brightness": round(mean)})
            write_progress(scratch, progress)
        if mean < 20:
            raise Stop(f"QA: slide {i:02d} is blank or near-black at {t:.1f} s (brightness {mean:.0f})")


def export_cover(first_slide_frames: Path, out: Path) -> None:
    """The Reel cover: the fully written first slide, as a 1080x1920 JPEG. Instagram shows it as the thumbnail."""
    from PIL import Image

    frames = sorted(first_slide_frames.glob("*.png"))
    if not frames:
        raise Stop("no frames to build the Reel cover from")
    with Image.open(frames[-1]) as im:
        im.convert("RGB").save(out, "JPEG", quality=92)
    print(f"cover: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug")
    ap.add_argument("--engine", choices=["kokoro", "windows"], default="kokoro",
                    help="voice engine: kokoro (natural, local model in C:\\kokoro) or windows (built-in)")
    ap.add_argument("--voice", default=None,
                    help="voice name. Kokoro default af_heart (also am_michael, af_bella). Windows default Microsoft Mark")
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE,
                    help=f"speaking rate, -10 (slow) to 10 (fast); default {DEFAULT_RATE} (normal)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"slides rendered at the same time (default {DEFAULT_WORKERS})")
    args = ap.parse_args()
    voice = args.voice or ("af_heart" if args.engine == "kokoro" else DEFAULT_VOICE)
    try:
        out = build(args.slug, args.engine, voice, args.rate, max(1, args.workers))
        probe = subprocess.run([find_tool("ffprobe"), "-v", "error", "-show_entries",
                                "format=duration:stream=width,height", "-of", "json", str(out)],
                               capture_output=True, text=True)
        print(f"reel: {out}")
        print(f"probe: {json.loads(probe.stdout or '{}')}")
    except Stop as e:
        mark_failed(str(e))
        print(f"STOP: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
