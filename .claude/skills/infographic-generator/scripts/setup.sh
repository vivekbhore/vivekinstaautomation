#!/usr/bin/env bash
# Install Python deps, Chromium for Playwright, and the three OFL fonts into resources/fonts/.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FONT_DIR="$SKILL_DIR/resources/fonts"
PY="${PYTHON:-python}"
RAW="https://raw.githubusercontent.com/google/fonts/main/ofl"

# --use-feature=truststore lets pip trust the OS certificate store, which corporate TLS proxies need.
"$PY" -m pip install --upgrade --use-feature=truststore playwright jinja2 pillow
"$PY" -m playwright install chromium

mkdir -p "$FONT_DIR"
curl -fL -o "$FONT_DIR/InstrumentSerif-Regular.ttf" "$RAW/instrumentserif/InstrumentSerif-Regular.ttf"
curl -fL -o "$FONT_DIR/InstrumentSerif-Italic.ttf"  "$RAW/instrumentserif/InstrumentSerif-Italic.ttf"
curl -fL -o "$FONT_DIR/Manrope-Variable.ttf"        "$RAW/manrope/Manrope%5Bwght%5D.ttf"
curl -fL -o "$FONT_DIR/JetBrainsMono-Variable.ttf"  "$RAW/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf"
curl -fL -o "$FONT_DIR/Caveat-Variable.ttf"         "$RAW/caveat/Caveat%5Bwght%5D.ttf"

echo "Setup complete. Fonts in $FONT_DIR. Next: drop a transparent PNG at resources/logo.png."
