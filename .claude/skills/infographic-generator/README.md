# infographic-generator

Builds a vivektraces Instagram carousel (3–7 slides, 1080×1350, exported at 2160×2700) from HTML and CSS.

Setup (from the project root):

```bash
bash .claude/skills/infographic-generator/scripts/setup.sh
```

Then put a transparent PNG logo (at least 200px wide) at `.claude/skills/infographic-generator/resources/logo.png`.

Render a set after writing `output/<slug>/slides.json`:

```bash
python .claude/skills/infographic-generator/scripts/render_carousel.py <slug>
```

Publish one approved post by hand (set `IG_ACCESS_TOKEN` and `IG_BUSINESS_ACCOUNT_ID` first):

```bash
python .claude/skills/infographic-generator/scripts/publish_post.py --dry-run
python .claude/skills/infographic-generator/scripts/publish_post.py
```

Full instructions are in `SKILL.md`.
