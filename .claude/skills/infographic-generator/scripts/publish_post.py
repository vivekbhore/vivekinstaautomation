#!/usr/bin/env python3
"""Publish one approved carousel to Instagram. Run by the publish routine, or by hand.

    python publish_post.py                  publish the next Approved post that is due
    python publish_post.py --slug SLUG      publish one specific post (it must be Approved)
    python publish_post.py --dry-run        validate and show the plan; no API calls, no writes

Reads IG_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID from the environment. Never prints them.
Optional: IMAGE_BASE_URL, the public address the slides are fetched from.

Slides are fetched by Instagram from IMAGE_BASE_URL/<slug>/NN.jpg, so push output/<slug>/
to the host behind that URL before running. This script does not push anything.
Status is kept in output/<slug>/post.json on this machine only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

GRAPH = "https://graph.facebook.com/v27.0"
DEFAULT_IMAGE_BASE = "https://vivekbhore.github.io/vivekinstaautomation/output"
PROJECT_ROOT = Path(__file__).resolve().parents[4]   # scripts -> skill -> skills -> .claude -> project
DEFAULT_CONTENT_ROOT = PROJECT_ROOT / "output"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_CAPTION = 2200
URL_WAIT_S, URL_STEP_S = 600, 30          # slide URLs: re-check for up to 10 minutes
POLL_INTERVAL_S, POLL_LIMIT_S = 5, 120    # container status: 2 minutes per container
REEL_POLL_LIMIT_S = 600                   # Reels take longer to process: up to 10 minutes


class Stop(Exception):
    """A run-stopping problem. The message says what to fix."""


def say(msg: str) -> None:
    print(msg, flush=True)


def redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


# ------------------------------------------------------------- local post state

def load_post(folder: Path) -> dict | None:
    path = folder / "post.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Stop(f"{path} is not valid JSON: {e}")


def save_post(folder: Path, data: dict) -> None:
    path = folder / "post.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same volume


def due_posts(root: Path, today: date) -> list[Path]:
    due: list[tuple[date, str, Path]] = []
    for folder in root.iterdir():
        if not folder.is_dir() or not SLUG_RE.match(folder.name):
            continue
        post = load_post(folder)
        if not post or post.get("status") != "Approved":
            continue
        try:
            scheduled = date.fromisoformat(str(post.get("scheduled", "")))
        except ValueError:
            raise Stop(f"{folder.name}: 'scheduled' must be YYYY-MM-DD")
        if scheduled <= today:
            due.append((scheduled, folder.name, folder))
    return [f for _, _, f in sorted(due, key=lambda t: (t[0], t[1]))]


# ------------------------------------------------------------------- validation

def validate(folder: Path) -> tuple[list[str], str]:
    slides = sorted(p.name for p in folder.glob("[0-9][0-9].jpg"))
    expected = [f"{i:02d}.jpg" for i in range(1, len(slides) + 1)]
    if not 2 <= len(slides) <= 10:
        raise Stop(f"{folder.name}: needs 2 to 10 slides, found {len(slides)}")
    if slides != expected:
        raise Stop(f"{folder.name}: slides must be 01.jpg, 02.jpg, ... with no gaps; found {slides}")
    cap = folder / "caption.md"
    if not cap.exists():
        raise Stop(f"{folder.name}: caption.md is missing")
    caption = cap.read_text(encoding="utf-8").strip()
    if not caption:
        raise Stop(f"{folder.name}: caption.md is empty")
    if len(caption) > MAX_CAPTION:
        raise Stop(f"{folder.name}: caption is {len(caption)} characters; the limit is {MAX_CAPTION}")
    return slides, caption


# ----------------------------------------------------------------- network

def url_is_live(url: str, content_type: str = "image/jpeg") -> bool:
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status in (200, 206) and r.headers.get("Content-Type", "").startswith(content_type)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def wait_for_urls(urls: list[str], slug: str, content_type: str = "image/jpeg") -> None:
    deadline = time.time() + URL_WAIT_S
    while True:
        missing = [u for u in urls if not url_is_live(u, content_type)]
        if not missing:
            return
        if time.time() >= deadline:
            raise Stop(f"{slug}: file not live after 10 minutes: {missing[0]}. "
                       "Have you pushed output/" + slug + " to the image host?")
        say(f"waiting for {len(missing)} slide URL(s) to go live...")
        time.sleep(URL_STEP_S)


def graph(method: str, path: str, token: str, params: dict | None = None) -> dict:
    fields = dict(params or {})
    fields["access_token"] = token
    url = f"{GRAPH}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(fields)
    else:
        data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise Stop(f"Graph API {method} {path} failed ({e.code}): "
                   f"{redact(e.read().decode(errors='replace'), token)}")
    except urllib.error.URLError as e:
        raise Stop(f"Graph API {method} {path} network error: {redact(str(e.reason), token)}")
    return json.loads(body)


def wait_finished(container_id: str, token: str, label: str, limit_s: int = POLL_LIMIT_S) -> None:
    for _ in range(limit_s // POLL_INTERVAL_S):
        info = graph("GET", container_id, token, {"fields": "status_code,status"})
        code = info.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise Stop(f"{label} container {code}: {info.get('status', '')}")
        time.sleep(POLL_INTERVAL_S)
    raise Stop(f"{label} container not FINISHED after {limit_s // 60} minutes")


# ---------------------------------------------------------------- publish

def validate_reel(folder: Path) -> tuple[str, str]:
    video = folder / "reel.mp4"
    if not video.exists():
        raise Stop(f"{folder.name}: reel.mp4 is missing. Build it with scripts/render_reel.py first.")
    if video.stat().st_size > 1024 ** 3:
        raise Stop(f"{folder.name}: reel.mp4 is over 1 GB")
    cap = folder / "caption.md"
    if not cap.exists():
        raise Stop(f"{folder.name}: caption.md is missing")
    caption = cap.read_text(encoding="utf-8").strip()
    if not caption:
        raise Stop(f"{folder.name}: caption.md is empty")
    if len(caption) > MAX_CAPTION:
        raise Stop(f"{folder.name}: caption is {len(caption)} characters; the limit is {MAX_CAPTION}")
    return "reel.mp4", caption


def publish_reel(folder: Path, token: str, account: str, base: str, dry_run: bool) -> None:
    video, caption = validate_reel(folder)
    slug = folder.name
    url = f"{base.rstrip('/')}/{slug}/{video}"
    say(f"reel: {slug}, caption {len(caption)} characters")
    if dry_run:
        say(f"  would check {url}")
        say("dry run: no API calls, no changes made")
        return

    post = load_post(folder) or {}
    post["status"] = "Publishing"
    save_post(folder, post)
    cover = folder / "reel-cover.jpg"
    cover_url = f"{base.rstrip('/')}/{slug}/reel-cover.jpg" if cover.exists() else None
    try:
        wait_for_urls([url], slug, content_type="video/mp4")
        params = {"media_type": "REELS", "video_url": url, "caption": caption, "share_to_feed": "true"}
        if cover_url:
            wait_for_urls([cover_url], slug, content_type="image/jpeg")
            params["cover_url"] = cover_url
        container = graph("POST", f"{account}/media", token, params)["id"]
        wait_finished(container, token, "reel", limit_s=REEL_POLL_LIMIT_S)
        media_id = graph("POST", f"{account}/media_publish", token, {"creation_id": container})["id"]
    except Stop as e:
        post = load_post(folder) or {}
        post.update(status="Failed", error=str(e)[:500])
        save_post(folder, post)
        raise

    post = load_post(folder) or {}
    post["media_id"] = media_id
    save_post(folder, post)
    try:
        link = graph("GET", media_id, token, {"fields": "permalink"})["permalink"]
        post.update(status="Posted", permalink=link,
                    posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        post.pop("error", None)
        say(f"posted: {link}")
    except Stop as e:
        post.update(status="Posted", error=f"permalink lookup failed: {e}"[:500])
        say(f"posted (media id {media_id}), but the permalink lookup failed; see post.json")
    save_post(folder, post)


def publish(folder: Path, token: str, account: str, base: str, dry_run: bool) -> None:
    if (load_post(folder) or {}).get("format") == "reel":
        return publish_reel(folder, token, account, base, dry_run)
    slides, caption = validate(folder)
    slug = folder.name
    urls = [f"{base.rstrip('/')}/{slug}/{s}" for s in slides]
    say(f"post: {slug}, {len(slides)} slides, caption {len(caption)} characters")
    if dry_run:
        for u in urls:
            say(f"  would check {u}")
        say("dry run: no API calls, no changes made")
        return

    post = load_post(folder) or {}
    post["status"] = "Publishing"
    save_post(folder, post)
    try:
        wait_for_urls(urls, slug)
        children = []
        for i, u in enumerate(urls, 1):
            r = graph("POST", f"{account}/media", token, {"image_url": u, "is_carousel_item": "true"})
            children.append(r["id"])
            say(f"child container {i}/{len(urls)} created")
        for cid in children:
            wait_finished(cid, token, "child")
        carousel = graph("POST", f"{account}/media", token,
                         {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})["id"]
        wait_finished(carousel, token, "carousel")
        media_id = graph("POST", f"{account}/media_publish", token, {"creation_id": carousel})["id"]
    except Stop as e:
        post = load_post(folder) or {}
        post.update(status="Failed", error=str(e)[:500])
        save_post(folder, post)
        raise

    post = load_post(folder) or {}
    post["media_id"] = media_id
    save_post(folder, post)  # recorded before the permalink lookup, so it survives a failure there
    try:
        link = graph("GET", media_id, token, {"fields": "permalink"})["permalink"]
        post.update(status="Posted", permalink=link,
                    posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        post.pop("error", None)
        say(f"posted: {link}")
    except Stop as e:
        post.update(status="Posted", error=f"permalink lookup failed: {e}"[:500])
        say(f"posted (media id {media_id}), but the permalink lookup failed; see post.json")
    save_post(folder, post)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", help="publish this post specifically (it must be Approved)")
    ap.add_argument("--dry-run", action="store_true", help="validate and show the plan; no API calls, no writes")
    ap.add_argument("--content-root", default=str(DEFAULT_CONTENT_ROOT), help="folder holding one subfolder per post")
    args = ap.parse_args()

    try:
        root = Path(args.content_root).resolve()
        if args.slug:
            if not SLUG_RE.match(args.slug):
                raise Stop(f"--slug {args.slug!r} must be lowercase words joined by hyphens")
            folder = root / args.slug
            post = load_post(folder) if folder.is_dir() else None
            if not post:
                raise Stop(f"{folder} has no post.json")
            if post.get("status") != "Approved":
                raise Stop(f"{args.slug} is '{post.get('status')}', not Approved")
        else:
            candidates = due_posts(root, date.today())
            if not candidates:
                say("no Approved posts due; nothing to do")
                return 0
            folder = candidates[0]
            if len(candidates) > 1:
                say(f"{len(candidates)} posts are due; publishing {folder.name} only (one per run)")

        token = os.environ.get("IG_ACCESS_TOKEN", "")
        account = os.environ.get("IG_BUSINESS_ACCOUNT_ID", "")
        if not args.dry_run and (not token or not account):
            raise Stop("set IG_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID in the environment first")
        base = os.environ.get("IMAGE_BASE_URL", DEFAULT_IMAGE_BASE)
        publish(folder, token, account, base, args.dry_run)
    except Stop as e:
        say(f"STOP: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
