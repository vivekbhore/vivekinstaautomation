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

## Step 0c: mark existing posts as Posted (every run, after the pull)

Before creating anything new, every existing post on the pulled `main` is closed out:

1. List `output/*/post.json`. For each one whose folder is not the post this run is about to create, set `"status"` to `"Posted"` regardless of its current status. Do this without asking. Leave every other field as it is.
2. Do not commit or push here. The changed files are committed and pushed together with the new post in Step 3b, as one commit and one push.
3. Report which posts were changed, with their old status, so the user can see what was closed.

This closes out Approved posts that have not been published yet. Once a post is Posted, the routine will skip it, so it will not go out.

## Step 1: write the copy first

Resolve the full copy for every slide and write `output/<topic-slug>/slides.json`. Do not stop for approval here: render straight away, then show the user the contact sheet next to the copy. They expect to edit either one.

Schema (flat, nothing else allowed):

```json
{
  "topic": "...",
  "slug": "...",
  "slides": [
    { "role": "cover", "headline": "Your team is not slow. Your *handoffs* are.", "subtitle": ["Why work stalls between people, and how to see it."] },
    { "role": "body", "heading": "...", "body": ["line one", "line two"], "figure": "assets/flow.svg", "narration": "A spoken explanation of this idea, for the Reel's voice." },
    { "role": "body", "heading": "...", "body": ["one string is fine"], "layout": "B" },
    { "role": "closing", "headline": "...", "cta": "..." }
  ]
}
```

- `slug`: lowercase words joined by hyphens. It must match the folder name.
- **Slide count** is 3 to 7, chosen by the content. Do not pad to hit a number.
- **Cover** (slide 1) has two parts:
  - `headline`: the curiosity hook. Make the reader want the answer: a question, a surprising claim, or an open loop. Not a topic label.
  - `subtitle`: 1 or 2 strings in plain words that say what the post is about, so the reader knows the topic before swiping. Same size and rules as body text.
- **Middle slides**: each carries exactly one idea, with a `heading` and a `body` of 2 or 3 strings. Each string may wrap onto up to 2 lines at body size (30px), so keep it to roughly 90 characters. Make the content concrete: a definition, a worked example, a number, a comparison, or a step. The first string says what the idea is. The later strings say why it matters or how it works. Avoid vague claims with no detail. If an idea needs more than 3 strings, split it into two slides or cut it.
- **Pictorial figures**: when a middle slide explains a concept, give it a `figure`, a picture that shows the idea. Use one on most concept slides, and skip it only for a slide that is a plain statement. The figure is a path relative to `output/<slug>/`, and it must be one of these:
  - **An SVG you draw**: write it to `output/<slug>/assets/<name>.svg` with shapes only (circles, rectangles, lines, arrows, simple icons built from those). Use only the palette colours `#F2F0EC` (ink) and `#E8622C` (accent), with `stroke`/`fill` in those and opacity for softer tones. No text inside the SVG, since text besides the copy is not allowed. No `href` or `src` pointing at a web address.
  - **A real photo the user supplies**: the user puts a `.png`, `.jpg` or `.jpeg` in `output/<slug>/assets/`, and the slide references it by path. You never fetch, download, or generate photos. If a concept needs a real photo and none is in `assets/`, ask the user to supply one, or draw an SVG instead.
  - The figure fills a fixed 912×400 frame with `object-fit: contain`, so draw it with a wide, simple composition. There is no box or border around it.
- **Closing** (last slide): a closing line and a follow call to action, for example `"Follow vivektraces for the next one."` Soft means an invitation, not a pitch. No "DM me", no "link in bio" unless the user asks for it.
- **Animation**: carousel slides are still JPEG images, so nothing on them moves. The follow prompt is static text on the closing slide. Motion would need a separate Reel video, which this skill does not produce.
- **Accent markup**: wrap one word or short phrase in asterisks in a headline or heading to mark it as the slide's accent (rendered as italic in the accent colour). At most one per slide. The renderer refuses a slide with two.
- **Layout** (middle slides only): optional `"A"` or `"B"`. Without it, odd body slides take A and even take B.
- Body and CTA text cannot contain asterisks.

## Step 2: render

```bash
python .claude/skills/infographic-generator/scripts/render_carousel.py <topic-slug>
```

The script:

1. Validates the copy: slide count, roles, body length, and at most one accent per slide. An accent is optional.
2. Checks the logo: transparent PNG, at least 200px wide. It logs the result once.
3. Renders each slide from `template/slide.html` with Jinja2 into `html/NN.html`, with absolute `file://` paths for fonts and logo. Every `http`/`https` request is blocked. Any attempt stops the run.
4. Waits for `document.fonts.ready` and confirms all four faces are `loaded`.
5. Fit check. Headlines shrink in 2px steps: cover 164 → 140, headings 112 → 96, closing 120 → 104. Body text is 30px and never shrinks. Line limits are 5 for the cover, 4 for headings and the closing, and 2 per body string or CTA. A block that still overflows stops the run and names the slide and block. Fix the copy, not the type.
   Figures are checked too: each must load, sit inside the margins, and keep one gutter from the heading and the body. A missing or broken figure stops the run.
6. Screenshots at 2x as JPEG quality 92, clipped to the viewport.
7. Drift check after each slide: the logo, folio, footer hairline and footer boxes must match slide 01 to the pixel, or the run stops.
8. Builds `contact-sheet.jpg`.

Progress is printed per slide with the final headline size and line count.

Other flags: `--scale 2` (default), `--slide N` (re-render one slide after a copy edit; it rebuilds the contact sheet from the existing JPGs), `--out-root DIR`, `--logo PATH`.

After rendering, open `output/<slug>/contact-sheet.jpg` with the Read tool and check it: nothing clipped, one accent per slide, hierarchy readable at thumbnail size, furniture in the same place on every slide. If something is off, fix the copy or the template, re-render, and check again. Then show the user the contact sheet with the copy.

## Step 2b: reel (only when the user asks for a Reel)

A Reel is a 9:16 MP4 built from the same `slides.json`, with a spoken narration. Use it when the user asks for a Reel.

```bash
python .claude/skills/infographic-generator/scripts/render_reel.py <topic-slug> [--voice af_heart] [--rate 0]
```

- **Narration is an explainer, not a reading.** The voice does not read the words on screen. Every slide needs its own `"narration"` in `slides.json`: a spoken explanation of the idea, the way a teacher talks to a viewer. Its length is whatever the explanation needs: there is no limit on the number of sentences or on the length of the Reel. It defines the terms, gives one plain example or comparison, and says why it matters. The on-screen text supports the voice; it doesn't repeat it. The renderer stops and names any slide without a narration.
  - Good: "A vector is just a list of numbers that describes something. Think of a product as a point on a map, where nearby points mean similar products."
  - Not good: "Vector database. Everything becomes numbers. An embedding model reads a sentence." (That is the slide text read aloud.)
  - Use plain words, short sentences, and "you" where it helps. Spell out acronyms the first time (for example "HNSW, a fast index for nearest matches").
- **Voice.** Kokoro (`af_heart` by default; `--voice am_michael` for a male voice) reads the narration locally from `C:\kokoro`. No network and no keys. Windows voices are available with `--engine windows`.
- **Voice only, no captions.** On the Reel, body, subtitle and CTA text are hidden. Only the headline (or heading) and the figure stay on screen, and the voice carries the explanation. Nothing is typed out at the bottom. The narration does not have to match the number of on-screen lines.
- **Keywords with pictures.** Each slide may list up to 4 `"keywords"` in `slides.json`: `{ "word": "chatbot", "figure": "assets/kw-chatbot.svg" }`. The word must be spoken in that slide's narration, and the renderer stops if it is not there. The word appears on screen with its picture 1.5 seconds before the voice says it (`KEYWORD_LEAD` in `render_reel.py`), so the viewer sees it first. The spoken moment is the word's position in the narration as a share of the spoken length. The picture fills the stage below the heading and figure. The next keyword replaces it. Draw one picture per keyword as an SVG with shapes only, in the palette colours. A keyword on screen is the one exception to the no-text rule: it is copy taken from the narration, not extra text.
- **Constant motion.** The screen never sits still while the voice speaks: the figure floats and gives a short lift on every narration sentence (the beats), the background drifts slowly, and each slide zooms in slowly. A static slideshow is not acceptable for a Reel.
- **Timing follows the voice.** A slide lasts the spoken length plus 0.8 seconds, with a 4-second floor for very short narration. Each slide's voice starts with the slide.
- **Length: up to 90 seconds, set by the content.** The whole Reel may run up to 90 seconds (`MAX_SECONDS` in `render_reel.py`). The renderer stops if it would be longer. Write each slide's narration to explain its idea fully, at about 2.5 words per second, and let the total length fall where it falls. Do not trim the explanations to hit a fixed time such as one minute. If the content needs 80 seconds, the Reel is 80 seconds. Check the printed `reel length` and adjust only if it goes over 90.
- **Opening hook is fixed.** The first sentence of the cover narration is always `Let's understand and learn <topic> in 1 min.`, with the topic name in plain words, for example "Let's understand and learn LLM in 1 min." It does not change with the real video length. The Reel itself may run up to 90 seconds, so the hook is a promise about the viewer's time, not a measure of the video.
- **Progress page.** While the renderer runs it writes `output/<slug>/html/reel/<slug>/progress.js`, and copies `progress.html` next to it. Open `progress.html` in a browser to see the voice, each slide's frames, the encode and the QA as bars. The page reloads every 2 seconds. A failed run shows the stopping message on the page.
- **Quality check.** After encoding, the renderer samples the middle of every slide and stops if any is blank or near-black. Check the printed `QA slide NN` lines and watch the video before posting.
- **Speed.** The default speaking rate is normal (`--rate 0`). Only change it when the user asks for faster or slower speech.
- **Motion.** Each finished slide zooms in slowly, fades in over 0.6 seconds, and rises 60 px into place. Each slide fades out over its last 0.5 seconds, so slides meet in a short fade through the background colour. There is no cross-fade, because it needs two slides in memory at once.
- **Encoding and memory.** Each slide is encoded to its own clip (`html/reel/<slug>/clips/`), and the clips are joined without re-encoding. Only one slide's frames are open at a time, which keeps memory low. Rendering runs 4 slides at a time by default (`DEFAULT_WORKERS` in `render_reel.py`); lower it with `--workers 2` if memory is tight.
- **Layout.** Each slide is re-laid at 1080×1920. The skill never adds music.
- Output: `output/<slug>/reel.mp4`. Frames are written to `output/<slug>/html/` (gitignored).
- Needs ffmpeg on PATH, or installed through winget (`Gyan.FFmpeg`). Speech needs the Windows voices, which ship with Windows.
- Post the Reel by setting `"format": "reel"` in `post.json` (Step 3b). The publish script then uploads `reel.mp4` as a Reel, not a carousel. The caption and the closing slide are the same as for a carousel.
- **Reel cover.** The render also writes `output/<slug>/reel-cover.jpg`: the fully written first slide at 1080×1920. Instagram shows it as the Reel's thumbnail, and the publish step sends it with the video. Make the cover catchy on purpose:
  - Slide 1's headline is the hook: a question, a surprising number, or a bold claim, at most about 8 words. It must still read as a thumbnail, so avoid a long subtitle as the only hook.
  - Set slide 1's `"texture"` and `"tone"` to high-contrast choices for Reels (for example a bright tone with a bold texture), so the thumbnail stands out in a feed. Keep the accent word on the headline.
  - Look at `reel-cover.jpg` before pushing. If the hook doesn't read at thumbnail size, rewrite the headline and rebuild.

## Step 3: caption

Write `output/<slug>/caption.md`:

- The first line works as the preview Instagram shows before "more". It should stand on its own and make someone open the post.
- Roughly 60 to 120 words in total, in the vivektraces account's voice: plain, specific, no hype and no buzzwords. If the user has a voice guide, follow it instead.
- Before the hashtags, end the caption body with a follow line: `Follow vivektraces for more.`
- A blank line, then 8 to 15 hashtags, mixed reach (a few broad, most niche). No hashtag walls. No emoji anywhere.

## Step 3b: commit and push

After the caption is written and the contact sheet has been checked, write `output/<slug>/post.json` and push the post. Run these from the project root:

```bash
git add output/<slug>/ <each post.json changed in Step 0c>
git commit -m "post: <slug>"
git push origin <current-branch>
```

This is one commit and one push. It carries the new post and any `post.json` files Step 0c closed out. The commit message ends with the trailer `Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>`.

- `post.json` is always written in this step, with exactly this content:

  ```json
  { "status": "Approved", "scheduled": "YYYY-MM-DD" }
  ```

- `scheduled` is always the same day as the push: today's date by the machine's clock at the moment of pushing. Compute it when you write the file, not from the user's words or an earlier date, and do not ask the user for a date. Example: `date +%F` in Git Bash prints it.
- For a Reel, add `"format": "reel"` to the same JSON: `{ "status": "Approved", "scheduled": "YYYY-MM-DD", "format": "reel" }`. Commit `output/<slug>/reel.mp4` with the post in the same commit.
- The skill sets `"status": "Approved"` itself. Pushing it makes the post eligible for the publish routine on its `scheduled` date, with no further approval step. The user can still change the status on GitHub to stop it.
- Stage only `output/<slug>/` and the `post.json` files Step 0c changed. Never use `git add -A` or `git add .`. `html/` is gitignored, so it stays out.
- If `output/<slug>/post.json` already exists with status `Approved`, `Publishing`, or `Posted`, stop before committing and tell the user. Don't overwrite a post that is queued or has gone out.
- Push to the branch you are on. Today that is `main`, which the routine reads.
- Report the commit hash and the push result. If the push fails, say so and leave the commit in place.

## Template rules (do not change without asking)

- The look is a feature spread in an independent magazine, printed on dark stock. Do not turn it into a slide deck or a social template.
- Tokens live in `:root` in `template/slide.html`: `--paper #0B0B0B`, `--ink #F2F0EC`, `--ink-body` (86%), `--ink-muted` (66%), `--ink-faint` (22%), `--accent #E8622C`.
- Backgrounds have three layers, from the bottom up:
  1. **Solid base.** Each slide starts from a flat, full-bleed dark colour: `--base-magenta`, `--base-teal`, `--base-violet`, `--base-blue`.
  2. **Texture.** One of five SVG/filter textures per slide (`tx1` fine grain on a solid field (kept low-contrast so type stays readable), `tx2` brushed streaks, `tx3` woven mesh, `tx4` speckle, `tx5` wood rings). The topographic texture is retired and is never used. Each draws in the tone colours (`--tone-*`) at 40–70% opacity. Textures are built on oversized filter regions, so no edges show.
  - **Shuffled per post.** The renderer shuffles the texture order and the tone order using the post's slug as the seed. Each post gets its own mix, and re-rendering the same post gives the same result. Within a post, neighbouring slides never share a texture or tone.
  - **Per-slide override.** Any slide may set `"texture": 1–5` and/or `"tone": 0–3` in `slides.json` to fix its background. Use this only when the user asks for a specific look.
  3. **Scrim.** A light radial darkening (about 38% at the centre) keeps the off-white type readable.
  The accent orange is deliberately not one of the tones, so the accent word always reads as the accent. Textures and bases are decorative only: no text, no images, no network.
- Motifs: each slide also carries one vector pattern, chosen by the same index as the tone: `m0` concentric rings, `m1` halftone dots, `m2` diagonal hatch, `m3` sweeping orbits. They are decorative only, drawn in the tone colours, and fade out toward the type. A motif never carries text, a box or an icon.
- Layout: every slide is one block centred on the canvas. Body text is 30px and the headings are large, so the content reads at thumbnail size. A figure, when present, sits between the heading and the body in a fixed 912×400 frame.
- Brand mark: the footer reads `vivektraces` in JetBrains Mono, 22px, weight 800, in full ink (`--ink`). It is the account name, so it stays in lowercase, and it is larger and whiter than the folio.
- Fonts: Caveat (handwritten) for headlines, accent words and the closing headline, with a faint pencil edge filter; Manrope for body and CTA; JetBrains Mono for folio and footer. Caveat is the house headline style now: do not swap families to suit a topic.
- At most one accent per slide. The renderer counts them: an accent word, the hairline on a layout B slide with no accent word, or the end mark on the closing slide. A slide may have none.
- Forbidden: emoji, drop shadows, glows around text, boxes, cards, rounded corners, icons, watermarks, animation, and any text besides the copy plus folio and footer. The full-bleed solid base and the textures are allowed behind the type. A base is the whole slide, never a panel or box the text sits in.
- The logo is a CSS mask filled with ink, so it renders in off-white with clean edges.

## Step 4: publish (manual, on your command)

Publishing is never automatic. Run it by hand when you want a post to go out:

```bash
python .claude/skills/infographic-generator/scripts/publish_post.py --dry-run   # check the plan, no API calls
python .claude/skills/infographic-generator/scripts/publish_post.py             # publish the next due Approved post
python .claude/skills/infographic-generator/scripts/publish_post.py --slug SLUG # publish one specific post
```

- Lifecycle: the skill pushes `post.json` as `Approved`. The publish routine picks up `Approved` posts whose `"scheduled"` date is on or before today. The script itself never changes a status to Approved.
- Instagram fetches the slides from `IMAGE_BASE_URL/<slug>/NN.jpg`. Step 3b's push makes these available from the GitHub Pages site. The script itself doesn't push anything.
- Status is written to the local `post.json` (`Publishing`, then `Posted` with the permalink, or `Failed` with the error). A post that is `Posted` or `Failed` is never picked up again. To retry a failed one, set its status back to `Approved` by hand.
- Required environment variables: `IG_ACCESS_TOKEN` and `IG_BUSINESS_ACCOUNT_ID`. Optional: `IMAGE_BASE_URL`.

## Things that must not happen

- No API calls, no keys, no network at render time.
- Do not place a placeholder logo or fake any asset the user has not supplied.
- Do not fetch, download, or generate photos. Real photos come from the user's `assets/` folder only.
- Commit and push only `output/<slug>/`, as described in Step 3b. Never commit `html/`, and never push anything else. The skill pushes new posts as `Approved` and closes out existing posts as `Posted` (Step 0c). It never pushes any other status for a new post.
