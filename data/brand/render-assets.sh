#!/bin/sh
# Rebuilds every PNG the app uses from the SVG masters here.
# Needs rsvg-convert and ImageMagick. Run ./install.sh afterward to update the launcher icon.
set -eu
cd "$(dirname "$0")"
app=dev.drivescope.DriveScope
icons=../icons/hicolor
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

wordmark() {  # svg, output: cropped to the artwork, large enough for HiDPI
    rsvg-convert -w 1700 "$1" | magick - -trim +repage -bordercolor none -border 6 -strip PNG32:"$2"
}

icon_set() {  # svg, icon name: the artwork centered in a square, with a small margin
    rsvg-convert -w 2048 "$1" | magick - -trim +repage "$tmp/art.png"
    width=$(magick "$tmp/art.png" -format '%w' info:)
    side=$((width * 1000 / 957))
    magick "$tmp/art.png" -background none -gravity center -extent "${side}x${side}" "$tmp/square.png"
    for size in 16 22 24 32 48 64 128 256 512; do
        mkdir -p "$icons/${size}x${size}/apps"
        magick "$tmp/square.png" -filter Lanczos -resize "${size}x${size}" -strip \
            PNG32:"$icons/${size}x${size}/apps/$2.png"
    done
}

wordmark drivescope-wordmark-dark.svg wordmark-dark-tight.png
wordmark drivescope-wordmark-light.svg wordmark-tight.png
# The launcher can't follow the theme, and the dark logo's bright blue reads on both.
icon_set drivescope-logo-dark.svg "$app"
icon_set drivescope-logo-light.svg "$app-light"
