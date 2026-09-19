#!/usr/bin/env bash
# Rebuild public/assets/collage-bg.jpg from raven_images/ at screen resolution.
#
# Usage: FE=<path to frontend> bash tools/build-collage.sh
# The old file was a 512px crop lifted off the Stitch mockup screenshot, and the choose
# screen paints it full-bleed (`object-fit: cover`) — a ~4x upscale on any real display.
set -euo pipefail
R="$FE/raven_images"
OUT="$FE/public/assets/collage-bg.jpg"
W=1920; H=1080
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

srcs=(
  "$R/vibe search.jpg"      "$R/playlist builder.jpg" "$R/★.jpg"               "$R/DJ Raven!.jpg"
  "$R/similar by name.jpg"  "$R/jpg"                  "$R/Raven.jpg"           "$R/★(3).jpg"
  "$R/local library.jpg"    "$R/★(2).jpg"             "$R/Ravena💜 on TikTok.jpg" "$R/vibe search.jpg"
)
#         x     y   size  angle   — 4x3 with deliberate overlap; edges bleed off-canvas
layout=(
  " -70   -80   520   -6"  " 400  -110   520    5"  " 900   -70   520   -3"  "1380  -100   540    6"
  " -50   340   540    4"  " 430   370   520   -5"  " 880   330   540    3"  "1370   360   520   -4"
  " -60   790   520   -3"  " 410   810   540    6"  " 890   780   520   -4"  "1380   800   540    5"
)

canvas="$work/canvas.png"
magick -size ${W}x${H} "xc:#0b0616" "$canvas"

for i in "${!srcs[@]}"; do
  read -r x y s a <<<"${layout[$i]}"
  p="$work/p$i.png"
  # square crop -> polaroid border -> tilt -> drop shadow
  magick "${srcs[$i]}" -resize "${s}x${s}^" -gravity center -extent "${s}x${s}" \
    -bordercolor "#e8e3f0" -border 14 \
    -background none -rotate "$a" \
    \( +clone -background black -shadow 70x14+0+10 \) +swap \
    -background none -layers merge +repage "$p"
  magick "$canvas" "$p" -geometry "+${x}+${y}" -composite "$canvas"
done

# The choose screen lays its own ambient gradient over this, so the art ships pre-dimmed
# to the tone the old file carried (mean ~0.17). A linear multiply rather than -modulate:
# -modulate rolls hue on near-white pixels and speckled her eyes.
magick "$canvas" -modulate 100,88 -evaluate multiply 0.34 \
  -fill "#120b24" -colorize 10% -strip -quality 85 "$OUT"
magick "$OUT" -format "built %wx%h mean=%[fx:mean]\n" info:
