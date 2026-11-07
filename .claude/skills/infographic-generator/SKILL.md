---
name: infographic-generator
description: Builds a finished vivektraces Instagram carousel (3 to 7 slides at 1080x1350, screenshotted at 2x to 2160x2700) plus an Instagram caption, from HTML and CSS rendered in headless Chromium. Use when the user says things like "make a carousel about X", "infographic for X", "turn this into an Instagram carousel", "carousel slides on X", "carousel it", or pastes a topic and asks for slides or a carousel. No image generation model and no external API.
---

# infographic-generator

Produces `output/<topic-slug>/` containing:

- `slides.json`: the copy, the single source of truth (you write it)
- `01.jpg` … `NN.jpg`: 2160×2700 slides
- `contact-sheet.jpg`: all slides in a row at 360px wide
- `caption.md`: the Instagram caption, then a blank line, then hashtags
- `html/NN.html`: one HTML file per slide, so the user can open and tweak them in a browser (gitignored)

Run every command from the project root (the folder that holds `output/`). The skill lives at `.claude/skills/infographic-generator/`; below, `SKILL=.claude/skills/infographic-generator`.

## Step 0: check the environment (first run only)

1. If `$SKILL/resources/fonts/` lacks the four `.ttf` files, or `python -c "import playwright"` fails, run `bash $SKILL/scripts/setup.sh`.
2. If `$SKILL/resources/logo.png` is missing, tell the user to drop in a transparent PNG (at least 200px wide). Do not generate a placeholder logo. The renderer will stop until the file is there.

## Step 0b: pull the latest from GitHub (every run, before any new content)

The user edits `post.json` status and other files on GitHub, so a stale checkout can overwrite their changes. Before writing anything, from the project root:

```bash
git status --short
git pull --ff-only origin <current-branch>
```

- If `git status` shows uncommitted changes in files other than the one this run is about to write, stop and tell the user. Do not stash or discard them.
- If `--ff-only` fails because the histories have diverged, stop and tell the user. Do not merge or rebase on your own.
- Report the new commit hash, or say it was already up to date.

## Step 1: write the copy first

Resolve the full copy for every slide and write `output/<topic-slug>/slides.json`. Do not stop for approval here: render straight away, then show the user the contact sheet next to the copy. They expect to edit either one.

Schema (flat, nothing else allowed):

```json
{
  "topic": "...",
  "slug": "...",
  "slides": [
    { "role": "cover", "headline": "Your team is not slow. Your *handoffs* are." },
    { "role": "body", "heading": "...", "body": ["line one", "line two"] },
    { "role": "body", "heading": "...", "body": ["one line is fine"], "layout": "B" },
    { "role": "closing", "headline": "...", "cta": "..." }
  ]
}
```

- `slug`: lowercase words joined by hyphens. It must match the folder name.
- **Slide count** is 3 to 7, chosen by the content. Do not pad to hit a number.
- **Cover** (slide 1): one bold hook headline. No body text, no subtitle, nothing else.
- **Middle slides**: each carries exactly one idea, with a short `heading` and `body` of at most two strings. Each string must fit on one line at body size, so keep it to roughly 45 characters. Never a paragraph. If an idea needs three lines, split it into two slides or cut it.
- **Closing** (last slide): a closing line and a soft call to action. Soft means an invitation, not a pitch. No "DM me", no "link in bio" unless the user asks for it.
- **Accent markup**: wrap one word or short phrase in asterisks in a headline or heading to mark it as the slide's accent (rendered as italic in the accent colour). At most one per slide. The renderer refuses a slide with two.
- **Layout** (middle slides only): optional `"A"` or `"B"`. Without it, odd body slides take A and even take B.
- Body and CTA text cannot contain asterisks.

## Step 2: render

```bash
python .claude/skills/infographic-generator/scripts/render_carousel.py <topic-slug>
```

The script:

1. Validates the copy: slide count, roles, body length, one accent per slide (it warns when a slide has none).
2. Checks the logo: transparent PNG, at least 200px wide. It logs the result once.
3. Renders each slide from `template/slide.html` with Jinja2 into `html/NN.html`, with absolute `file://` paths for fonts and logo. Every `http`/`https` request is blocked. Any attempt stops the run.
4. Waits for `document.fonts.ready` and confirms all four faces are `loaded`.
5. Fit check. Headlines shrink in 2px steps: cover 136 → 116, headings 88 → 76, closing 96 → 84. Body text never shrinks. Line limits are 4 for the cover, 3 for headings and the closing, and 1 per body string. A block that still overflows stops the run and names the slide and block. Fix the copy, not the type.
6. Screenshots at 2x as JPEG quality 92, clipped to the viewport.
7. Drift check after each slide: the logo, folio, footer hairline and footer boxes must match slide 01 to the pixel, or the run stops.
8. Builds `contact-sheet.jpg`.

Progress is printed per slide with the final headline size and line count.

Other flags: `--scale 2` (default), `--slide N` (re-render one slide after a copy edit; it rebuilds the contact sheet from the existing JPGs), `--out-root DIR`, `--logo PATH`.

After rendering, open `output/<slug>/contact-sheet.jpg` with the Read tool and check it: nothing clipped, one accent per slide, hierarchy readable at thumbnail size, furniture in the same place on every slide. If something is off, fix the copy or the template, re-render, and check again. Then show the user the contact sheet with the copy.

## Step 3: caption

Write `output/<slug>/caption.md`:

- The first line works as the preview Instagram shows before "more". It should stand on its own and make someone open the post.
- Roughly 60 to 120 words in total, in the vivektraces account's voice: plain, specific, no hype and no buzzwords. If the user has a voice guide, follow it instead.
- A blank line, then 8 to 15 hashtags, mixed reach (a few broad, most niche). No hashtag walls. No emoji anywhere.

## Step 3b: commit and push

After the caption is written and the contact sheet has been checked, write `output/<slug>/post.json` and push the post. Run these from the project root:

```bash
git add output/<slug>/
git commit -m "post: <slug>"
git push origin <current-branch>
```

The commit message ends with the trailer `Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>`.

- `post.json` is always written in this step, with exactly this content and the date the user gave for the post:

  ```json
  { "status": "Scheduled", "scheduled": "YYYY-MM-DD" }
  ```

- If the user has not given a date, ask for one before committing. Do not guess.
- Never set `"status": "Approved"`. Only the user does that, by hand on GitHub. The publish routine ignores anything that is not Approved.
- Stage only `output/<slug>/`. Never use `git add -A` or `git add .`. `html/` is gitignored, so it stays out.
- If `output/<slug>/post.json` already exists with status `Approved`, `Publishing`, or `Posted`, stop before committing and tell the user. Don't overwrite a post that is queued or has gone out.
- Push to the branch you are on. Today that is `main`, which the routine reads.
- Report the commit hash and the push result. If the push fails, say so and leave the commit in place.

## Template rules (do not change without asking)

- The look is a feature spread in an independent magazine, printed on dark stock. Do not turn it into a slide deck or a social template.
- Tokens live in `:root` in `template/slide.html`: `--paper #0B0B0B`, `--ink #F2F0EC`, `--ink-muted` (58%), `--ink-faint` (16%), `--accent #E8622C`.
- Fonts: Instrument Serif (regular and italic) for headlines; Manrope for body and CTA; JetBrains Mono for folio and footer. Do not swap families to suit a topic.
- One accent per slide. The renderer counts them: an accent word, the hairline on a layout B slide with no accent word, or the end mark on the closing slide.
- Forbidden: emoji, shadows, glows, gradients behind text, boxes, cards, rounded corners, icons, watermarks, animation, and any text besides the copy plus folio and footer.
- The logo is a CSS mask filled with ink, so it renders in off-white with clean edges.

## Step 4: publish (manual, on your command)

Publishing is never automatic. Run it by hand when you want a post to go out:

```bash
python .claude/skills/infographic-generator/scripts/publish_post.py --dry-run   # check the plan, no API calls
python .claude/skills/infographic-generator/scripts/publish_post.py             # publish the next due Approved post
python .claude/skills/infographic-generator/scripts/publish_post.py --slug SLUG # publish one specific post
```

- Lifecycle: the skill pushes `post.json` as `Scheduled`. The user changes it to `Approved` by hand on GitHub. The publish routine then picks up `Approved` posts whose `"scheduled"` date is on or before today. The script never approves anything.
- Instagram fetches the slides from `IMAGE_BASE_URL/<slug>/NN.jpg`. Step 3b's push makes these available from the GitHub Pages site. The script itself doesn't push anything.
- Status is written to the local `post.json` (`Publishing`, then `Posted` with the permalink, or `Failed` with the error). A post that is `Posted` or `Failed` is never picked up again. To retry a failed one, set its status back to `Approved` by hand.
- Required environment variables: `IG_ACCESS_TOKEN` and `IG_BUSINESS_ACCOUNT_ID`. Optional: `IMAGE_BASE_URL`.

## Things that must not happen

- No API calls, no keys, no network at render time.
- Do not place a placeholder logo or fake any asset the user has not supplied.
- Commit and push only `output/<slug>/`, as described in Step 3b. Never commit `html/`, and never push anything else. Never push a post marked Approved. The skill only ever pushes `Scheduled`.
