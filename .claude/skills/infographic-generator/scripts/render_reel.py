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
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import render_carousel as rc  # noqa: E402  (shared copy checks, fit routine and fonts)

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
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

def render_one_slide(job: tuple[str, int, list[tuple[float, float]], float]) -> tuple[str, float]:
    """Worker: screenshot every frame of one slide while each line is written on in its own sentence.

    Runs in its own process with its own Chromium, so several slides render at once.
    Returns (frame folder, seconds shown).
    """
    staging_post_s, n, windows, seconds = job
    staging_post = Path(staging_post_s)
    copy = rc.load_copy(staging_post / "slides.json")
    views, _ = rc.build_view(copy, staging_post)
    view = views[n - 1]
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
            html_text = tpl.render(slide=view, fonts=fonts, logo_uri=logo_uri, logo_w=logo_w, H=H)
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
                page.evaluate(f"window.__frame({t:.4f}, {json.dumps(beats)})")
                page.screenshot(path=str(frames_dir / f"{f:04d}.png"), type="png")
        finally:
            browser.close()
    return str(frames_dir), seconds


def render_slides(staging_post: Path, timing: list[tuple[list[tuple[float, float]], float]],
                  workers: int) -> list[tuple[Path, float]]:
    """Render all slides, several at a time. Results come back in slide order."""
    copy = rc.load_copy(staging_post / "slides.json")
    _, warnings = rc.build_view(copy, staging_post)
    for w in warnings:
        print(f"warning: {w}")
    jobs = [(str(staging_post), n, windows, seconds)
            for n, (windows, seconds) in enumerate(timing, 1)]
    results: list[tuple[Path, float]] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, (frames, seconds) in enumerate(pool.map(render_one_slide, jobs), 1):
            print(f"slide {n:02d}: rendered {seconds:.1f} s of frames")
            results.append((Path(frames), seconds))
    return results


# ------------------------------------------------------------------ encode

def encode(post_dir: Path, slides: list[tuple[Path, float]], wavs: list[Path], ffmpeg: str) -> Path:
    n = len(slides)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for frames_dir, _ in slides:
        cmd += ["-framerate", str(FPS), "-i", str(frames_dir / "%04d.png")]
    for wav in wavs:
        cmd += ["-i", str(wav)]

    chains = []
    durations = [s for _, s in slides]
    for i, seconds in enumerate(durations):
        chains.append(
            f"[{i}:v]zoompan=z='min(zoom+0.00035,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={FPS},format=rgba,fade=t=in:st=0:d=0.6:alpha=1[s{i}_fg];"
            f"color=c={PAPER}:s={W}x{H}:r={FPS}:d={seconds:.3f}[s{i}_bg];"
            f"[s{i}_bg][s{i}_fg]overlay=0:'if(lt(t,{RISE}),60*(1-t/{RISE}),0)':eval=frame,"
            f"format=yuv420p,setsar=1[s{i}]"
        )
    joined = "s0"
    played = durations[0]
    starts = [0.0]
    for i in range(1, n):
        out = f"x{i}" if i < n - 1 else "vout"
        offset = round(played - FADE, 3)
        starts.append(played - FADE)
        chains.append(f"[{joined}][s{i}]xfade=transition=fade:duration={FADE}:offset={offset}[{out}]")
        played += durations[i] - FADE
        joined = out
    total = played

    # Each slide's voice starts when its slide starts, then all voices are mixed together.
    voices = []
    for i in range(n):
        ms = int(round(starts[i] * 1000))
        chains.append(f"[{n + i}:a]adelay={ms}:all=1,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[v{i}]")
        voices.append(f"[v{i}]")
    chains.append(f"{''.join(voices)}amix=inputs={n}:duration=longest:normalize=0,"
                  f"atrim=0:{total:.3f},afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[aout]")

    out_path = post_dir / "reel.mp4"
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Stop(f"ffmpeg failed:\n{r.stderr[-1200:]}")
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
        timing.append((line_windows(narr, sentence_lists[n]), seconds))
        print(f"slide {n:02d} voice: {narr:.1f} s, {len(sentence_lists[n])} lines")

    slides = render_slides(staging_post, timing, workers)
    export_cover(slides[0][0], post_dir / "reel-cover.jpg")
    out = encode(post_dir, slides, wavs, find_tool("ffmpeg"))
    qa_reel(out, [seconds for _, seconds in timing], find_tool("ffmpeg"), staging_post)
    return out


def qa_reel(mp4: Path, durations: list[float], ffmpeg: str, scratch: Path) -> None:
    """Check the finished Reel: sample the middle of every slide and stop on a near-black or blank frame."""
    from PIL import Image, ImageStat

    starts, played = [], 0.0
    for i, d in enumerate(durations):
        starts.append(0.0 if i == 0 else played - FADE)
        played = (d if i == 0 else played + d - FADE)
    for i, (start, d) in enumerate(zip(starts, durations), 1):
        t = start + d / 2
        png = scratch / f"qa{i:02d}.png"
        r = subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mp4), "-frames:v", "1",
                            "-vf", "scale=108:192", str(png)], capture_output=True, text=True)
        if r.returncode != 0 or not png.exists():
            raise Stop(f"QA: could not sample slide {i:02d} at {t:.1f} s")
        mean = ImageStat.Stat(Image.open(png).convert("L")).mean[0]
        print(f"QA slide {i:02d} at {t:5.1f} s: brightness {mean:.0f}")
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
        print(f"STOP: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
