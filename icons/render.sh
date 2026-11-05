#!/bin/bash
# Render the badge sources in icons/src/ to the PNGs Strata loads. Run from the
# repository root after changing a source; needs rsvg-convert (librsvg) and
# optipng.
set -euo pipefail
for svg in icons/src/*.svg; do
    png="icons/$(basename "${svg%.svg}").png"
    rsvg-convert -w 128 -h 128 -o "$png" "$svg"
    optipng -quiet -o2 "$png"
done
