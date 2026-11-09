#!/usr/bin/env python3
"""Render a vivektraces Instagram carousel from output/<slug>/slides.json.

    python scripts/render_carousel.py <slug> [--scale 2] [--slide N]
                                      [--out-root output] [--logo PATH]

slides.json is the only source of copy. Each slide is rendered from
template/slide.html in headless Chromium, with every http(s) request blocked.
Stops with a message on any failure. Exit code 0 means the set rendered and passed
the fit and drift checks.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from PIL import Image
from playwright.sync_api import sync_playwright

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = SKILL_DIR / "template"
FONT_DIR = SKILL_DIR / "resources" / "fonts"
DEFAULT_LOGO = SKILL_DIR / "resources" / "logo.png"
FONT_FILES = {
    "instrument_regular": "InstrumentSerif-Regular.ttf",
    "instrument_italic": "InstrumentSerif-Italic.ttf",
    "manrope": "Manrope-Variable.ttf",
    "jetbrains_mono": "JetBrainsMono-Variable.ttf",
    "handwriting": "Caveat-Variable.ttf",
}
# Faces the page needs. Each must report "loaded" before any shot is taken.
REQUIRED_FACES = [
    '400 88px "Instrument Serif"',
    'italic 400 88px "Instrument Serif"',
    '500 30px "Manrope"',
    '500 12px "JetBrains Mono"',
    '400 88px "Caveat"',
]
W, H = 1080, 1350
LOGO_HEIGHT_PX = 28            # must match .logo in template/slide.html
THUMB_W = 360
ACCENT_RE = re.compile(r"\*([^*]+)\*")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHROME_ARGS = ["--force-color-profile=srgb", "--font-render-hinting=none", "--disable-lcd-text"]
TEXTURES = [1, 2, 3, 4, 5]          # tx0 is retired; 1 fine grain, 2 streaks, 3 mesh, 4 speckle, 5 wood rings
BACKGROUND_KEYS = {"texture", "tone"}   # optional on every slide; defaults come from the slide position
NARRATION_KEYS = {"narration", "keywords"}   # the spoken explanation and the Reel's on-screen keywords; not shown on the carousel
ALLOWED_KEYS = {
    "cover": {"role", "headline", "subtitle"} | BACKGROUND_KEYS | NARRATION_KEYS,
    "body": {"role", "heading", "body", "layout", "figure"} | BACKGROUND_KEYS | NARRATION_KEYS,
    "closing": {"role", "headline", "cta"} | BACKGROUND_KEYS | NARRATION_KEYS,
}
MAX_BODY_STRINGS = 3                 # body strings per middle slide
FIGURE_EXTS = {".svg", ".png", ".jpg", ".jpeg"}
FIGURE_RE = re.compile(r"""(href|src)\s*=\s*["']\s*https?:""", re.IGNORECASE)


class Stop(Exception):
    """A run-stopping problem. The message says what to fix."""


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- copy checks

def heading_markup(slide: dict, field: str, i: int) -> tuple[Markup, int]:
    """Turn *accent* markup into an italic accent span. Returns (html, accent count)."""
    text = slide.get(field)
    if not isinstance(text, str) or not text.strip():
        raise Stop(f"slide {i}: '{field}' must be a non-empty string")
    text = text.strip()
    if text.count("*") % 2:
        raise Stop(f"slide {i}: unbalanced asterisks in '{field}'")
    parts = ACCENT_RE.split(text)  # odd indexes are the accent phrases
    out = []
    for idx, part in enumerate(parts):
        if idx % 2:
            out.append(f'<span class="accent">{html.escape(part)}</span>')
        else:
            if "*" in part:
                raise Stop(f"slide {i}: empty or nested accent markup in '{field}'")
            out.append(html.escape(part))
    return Markup("".join(out)), len(parts) // 2


def check_figure(rel: str, out_dir: Path, i: int) -> str:
    """A figure is a file under output/<slug>/ (SVG, PNG or JPEG). Returns its file:// URI."""
    if not isinstance(rel, str) or not rel.strip():
        raise Stop(f"slide {i}: 'figure' must be a path string")
    path = (out_dir / rel).resolve()
    if out_dir.resolve() not in path.parents:
        raise Stop(f"slide {i}: figure {rel!r} must sit inside {out_dir}")
    if not path.exists():
        raise Stop(f"slide {i}: figure not found at {path}")
    if path.suffix.lower() not in FIGURE_EXTS:
        raise Stop(f"slide {i}: figure must be .svg, .png or .jpg, got {path.suffix!r}")
    if path.suffix.lower() == ".svg" and FIGURE_RE.search(path.read_text(encoding="utf-8", errors="replace")):
        raise Stop(f"slide {i}: SVG figure {path.name} links to a web address; figures must be self-contained")
    return path.as_uri()


def background_index(slide: dict, key: str, default: int, allowed, i: int) -> int:
    """Optional texture or tone override; otherwise the shuffled default. `allowed` is a list of values."""
    if key not in slide:
        return default
    value = slide[key]
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        raise Stop(f"slide {i}: '{key}' must be one of {list(allowed)}")
    return value


def post_shuffle(slug: str) -> tuple[list[int], list[int], int]:
    """Per-post order of textures and tones, plus a noise seed. Same slug, same result, so re-renders match."""
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    textures = list(TEXTURES)
    tones = list(range(4))
    rng.shuffle(textures)
    rng.shuffle(tones)
    return textures, tones, rng.randrange(1, 10000)


def build_view(copy: dict, out_dir: Path) -> tuple[list[dict], list[str]]:
    slides = copy["slides"]
    total = len(slides)
    textures, tones, seed = post_shuffle(str(copy.get("slug", "")))
    views: list[dict] = []
    warnings: list[str] = []
    body_index = 0
    for i, s in enumerate(slides, 1):
        role = s.get("role")
        if role not in ALLOWED_KEYS:
            raise Stop(f"slide {i}: unknown role {role!r}")
        extra = set(s) - ALLOWED_KEYS[role]
        if extra:
            raise Stop(f"slide {i}: unexpected keys for a {role} slide: {sorted(extra)}")
        if role == "cover" and i != 1:
            raise Stop("only slide 1 may be the cover")
        if role == "closing" and i != total:
            raise Stop("only the last slide may be the closing slide")
        if role == "body" and i in (1, total):
            raise Stop("slides 1 and last must be cover and closing")

        view = {"role": role, "index": i, "total": total, "layout": None,
                "headline_html": "", "heading_html": "", "body_html": [],
                "cta_html": "", "rule_accent": False,
                "texture": background_index(s, "texture", textures[(i - 1) % len(textures)], TEXTURES, i),
                "tone": background_index(s, "tone", tones[(i - 1) % 4], range(4), i),
                "seed": seed}

        if role == "cover":
            view["headline_html"], accents = heading_markup(s, "headline", i)
            sketch = out_dir / "assets" / "cover-sketch.svg"
            if sketch.exists():
                # Per-post Reel cover drawing, drawn from the topic. Checked like any figure.
                view["sketch_uri"] = check_figure("assets/cover-sketch.svg", out_dir, i)
            if "subtitle" in s:
                sub = s["subtitle"]
                if not isinstance(sub, list) or not 1 <= len(sub) <= 2:
                    raise Stop(f"slide {i}: 'subtitle' must be a list of 1 or 2 strings")
                for line in sub:
                    if not isinstance(line, str) or not line.strip():
                        raise Stop(f"slide {i}: subtitle lines must be non-empty strings")
                    if "*" in line:
                        raise Stop(f"slide {i}: asterisks are only allowed in headings")
                view["subtitle_html"] = [line.strip() for line in sub]

        elif role == "body":
            body_index += 1
            view["heading_html"], accents = heading_markup(s, "heading", i)
            lines = s.get("body")
            if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_BODY_STRINGS:
                raise Stop(f"slide {i}: 'body' must be a list of 1 to {MAX_BODY_STRINGS} strings")
            for line in lines:
                if not isinstance(line, str) or not line.strip():
                    raise Stop(f"slide {i}: body lines must be non-empty strings")
                if "*" in line:
                    raise Stop(f"slide {i}: asterisks are only allowed in headings")
            if "figure" in s:
                view["figure_uri"] = check_figure(s["figure"], out_dir, i)
            layout = str(s.get("layout") or ("A" if body_index % 2 else "B")).upper()
            if layout not in ("A", "B"):
                raise Stop(f"slide {i}: layout must be A or B")
            view["layout"] = layout
            view["body_html"] = [line.strip() for line in lines]
            if layout == "B" and accents == 0:
                # Layout B with no accent word: the short hairline above the heading is the accent.
                view["rule_accent"] = True
                accents = 1

        else:  # closing
            view["headline_html"], accents = heading_markup(s, "headline", i)
            cta = s.get("cta")
            if not isinstance(cta, str) or not cta.strip():
                raise Stop(f"slide {i}: 'cta' must be a non-empty string")
            if "*" in cta:
                raise Stop(f"slide {i}: asterisks are only allowed in headings")
            view["cta_html"] = cta.strip()
            accents += 1  # the end mark is this slide's accent

        if accents > 1:
            raise Stop(f"slide {i} has {accents} accents; the limit is one per slide")
        if accents == 0:
            pass  # an accent is optional: a slide with none is fine
        view["accent_count"] = accents
        views.append(view)
    return views, warnings


def load_copy(path: Path) -> dict:
    if not path.exists():
        raise Stop(f"{path} not found. Write the copy first (SKILL.md, step 1).")
    try:
        copy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Stop(f"{path} is not valid JSON: {e}")
    slides = copy.get("slides")
    if not isinstance(slides, list):
        raise Stop("slides.json needs a top-level 'slides' list")
    if not 3 <= len(slides) <= 7:
        raise Stop(f"{len(slides)} slides; a carousel needs 3 to 7")
    slug = copy.get("slug", "")
    if not SLUG_RE.match(str(slug)):
        raise Stop(f"slug {slug!r} must be lowercase words joined by hyphens")
    return copy


# --------------------------------------------------------------- asset checks

def check_fonts() -> dict[str, str]:
    missing = [f for f in FONT_FILES.values() if not (FONT_DIR / f).exists()]
    if missing:
        raise Stop(f"missing fonts in resources/fonts: {missing}. Run scripts/setup.sh.")
    return {k: (FONT_DIR / v).resolve().as_uri() for k, v in FONT_FILES.items()}


def check_logo(path: Path) -> float:
    """Logo must be a transparent PNG at least 200px wide. Returns width/height."""
    if not path.exists():
        raise Stop(f"logo not found at {path}. Drop the transparent PNG in resources/logo.png.")
    img = Image.open(path)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if not has_alpha:
        raise Stop(f"logo has no alpha channel (mode {img.mode}). Export a transparent PNG; "
                   "do not key the background out.")
    if img.width < 200:
        raise Stop(f"logo is {img.width}px wide; it needs to be at least 200px.")
    say(f"logo: {path.name}, mode {img.mode}, {img.width}x{img.height}px, alpha present: OK")
    return img.width / img.height


# -------------------------------------------------------------------- render

def wait_for_fonts(page) -> None:
    faces = page.evaluate(
        """async (specs) => {
            await document.fonts.ready;
            await Promise.all(specs.map(s => document.fonts.load(s)));
            return Array.from(document.fonts).map(f => ({family: f.family, style: f.style, status: f.status}));
        }""",
        REQUIRED_FACES,
    )
    bad = [f for f in faces if f["status"] != "loaded"]
    if len(faces) < len(REQUIRED_FACES) or bad:
        raise Stop(f"font fallback detected, faces not loaded: {bad or 'missing'}")


def wait_for_images(page, n: int) -> None:
    """Every figure must decode before the shot. A broken figure stops the run."""
    broken = page.evaluate(
        """async () => {
            const imgs = Array.from(document.images);
            await Promise.all(imgs.map(i => i.complete ? null : new Promise(r => {
                i.addEventListener('load', r, {once: true}); i.addEventListener('error', r, {once: true});
            })));
            return imgs.filter(i => !(i.naturalWidth > 0)).map(i => i.getAttribute('src'));
        }"""
    )
    if broken:
        raise Stop(f"slide {n:02d}: figure did not load: {broken[0]}")


def process_slide(page, tpl, ctx, n: int, shoot: bool, blocked: list[str]) -> dict:
    view = ctx["views"][n - 1]
    html_text = tpl.render(slide=view, fonts=ctx["fonts"], logo_uri=ctx["logo_uri"], logo_w=ctx["logo_w"], H=ctx["H"])
    html_path = ctx["html_dir"] / f"{n:02d}.html"
    html_path.write_text(html_text, encoding="utf-8")

    page.goto(html_path.resolve().as_uri(), wait_until="load")
    wait_for_fonts(page)
    wait_for_images(page, n)
    if blocked:
        raise Stop(f"slide {n:02d} tried to load from the network ({blocked[0]}). "
                   "A font or asset is falling back; the run stopped.")

    report = page.evaluate("window.__fit()")
    if report["problems"]:
        raise Stop(f"slide {n:02d}: " + "; ".join(report["problems"]) +
                   ". The fix is the copy, not smaller type.")
    for b in report["blocks"]:
        say(f"  slide {n:02d} {b['id']}: {b['size']}px, {b['lines']} line(s)")

    rects = page.evaluate("window.__rects()")
    if shoot:
        jpg = ctx["out_dir"] / f"{n:02d}.jpg"
        page.screenshot(path=str(jpg), type="jpeg", quality=92,
                        clip={"x": 0, "y": 0, "width": W, "height": ctx["H"]})
        with Image.open(jpg) as im:
            expect = (W * ctx["scale"], ctx["H"] * ctx["scale"])
            if im.size != expect:
                raise Stop(f"{jpg.name} is {im.size}, expected {expect}")
        say(f"slide {n:02d}: written {jpg.name} ({expect[0]}x{expect[1]})")
    return rects


def check_drift(baseline: dict, rects: dict, n: int) -> None:
    for key in ("logo", "folio", "rule", "footer"):
        if rects[key] != baseline[key]:
            raise Stop(f"drift on slide {n:02d}: {key} is at {rects[key]}, slide 01 has {baseline[key]}. "
                       "The set no longer matches; stopping.")


def contact_sheet(out_dir: Path, total: int) -> Path:
    files = [out_dir / f"{i:02d}.jpg" for i in range(1, total + 1)]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise Stop(f"cannot build contact sheet, missing {missing}")
    thumbs = []
    for f in files:
        with Image.open(f) as im:
            im = im.convert("RGB")
            thumbs.append(im.resize((THUMB_W, round(THUMB_W * im.height / im.width)), Image.LANCZOS))
    gap, pad = 16, 24
    width = pad * 2 + sum(t.width for t in thumbs) + gap * (len(thumbs) - 1)
    height = pad * 2 + thumbs[0].height
    sheet = Image.new("RGB", (width, height), (40, 40, 40))
    x = pad
    for t in thumbs:
        sheet.paste(t, (x, pad))
        x += t.width + gap
    path = out_dir / "contact-sheet.jpg"
    sheet.save(path, quality=92)
    return path


def run(args: argparse.Namespace) -> None:
    out_root = Path(args.out_root).resolve()
    out_dir = out_root / args.slug
    if not SLUG_RE.match(args.slug):
        raise Stop(f"slug {args.slug!r} must be lowercase words joined by hyphens")
    copy = load_copy(out_dir / "slides.json")
    if copy.get("slug") != args.slug:
        raise Stop(f"slides.json has slug {copy.get('slug')!r} but was called with {args.slug!r}")
    views, warnings = build_view(copy, out_dir)
    for w in warnings:
        say(f"warning: {w}")
    total = len(views)
    if args.slide and not 1 <= args.slide <= total:
        raise Stop(f"--slide must be between 1 and {total}")

    fonts = check_fonts()
    logo = Path(args.logo).resolve()
    aspect = check_logo(logo)
    logo_w = round(LOGO_HEIGHT_PX * aspect)

    html_dir = out_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
    tpl = env.get_template("slide.html")
    ctx = {"views": views, "fonts": fonts, "logo_uri": logo.as_uri(), "logo_w": logo_w,
           "html_dir": html_dir, "out_dir": out_dir, "scale": args.scale, "H": args.canvas_height}

    targets = [args.slide] if args.slide else list(range(1, total + 1))
    # Slide 01 is the drift baseline, so measure it even when re-rendering one slide.
    plan = [(1, False)] if 1 not in targets else []
    plan += [(n, True) for n in targets]

    blocked: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=CHROME_ARGS)
        try:
            context = browser.new_context(viewport={"width": W, "height": ctx["H"]},
                                          device_scale_factor=args.scale)

            def guard(route):
                url = route.request.url
                if url.startswith(("http://", "https://")):
                    blocked.append(url)
                    return route.abort()
                return route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            baseline = None
            for n, shoot in plan:
                say(f"rendering slide {n:02d} of {total:02d}")
                rects = process_slide(page, tpl, ctx, n, shoot, blocked)
                if baseline is None:
                    baseline = rects
                else:
                    check_drift(baseline, rects, n)
        finally:
            browser.close()

    sheet = contact_sheet(out_dir, total)
    say(f"contact sheet: {sheet}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="topic slug; folder output/<slug>/ must hold slides.json")
    ap.add_argument("--scale", type=float, default=2, help="device scale factor (default 2 → 2160x2700)")
    ap.add_argument("--slide", type=int, help="re-render one slide (1-based) after a copy edit")
    ap.add_argument("--out-root", default=str(Path.cwd() / "output"), help="output root (default ./output)")
    ap.add_argument("--logo", default=str(DEFAULT_LOGO), help="transparent PNG logo (default resources/logo.png)")
    ap.add_argument("--canvas-height", type=int, default=H, help="slide height in px: 1350 (4:5 carousel, default) or 1920 (9:16 reel)")
    args = ap.parse_args()
    try:
        run(args)
    except Stop as e:
        say(f"STOP: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
