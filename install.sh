#!/bin/sh
# Installs DriveScope for the current user, and any packages it's missing on Arch.
#   ./install.sh                     the app
#   ./install.sh --with-temperature  also lets benchmarks read temperature without a prompt
set -eu
root=$(cd "$(dirname "$0")" && pwd)
packages="gtk4 libadwaita python-gobject python-cairo fio smartmontools udisks2"
policy=dev.drivescope.smartctl.policy
with_temperature=false

for arg in "$@"; do
    case "$arg" in
        --with-temperature) with_temperature=true ;;
        *) echo "Unknown option: $arg" >&2; echo "Usage: ./install.sh [--with-temperature]" >&2; exit 2 ;;
    esac
done

if command -v pacman >/dev/null; then
    # pacman -T exits non-zero and lists whatever isn't installed.
    # shellcheck disable=SC2086
    missing=$(pacman -T $packages || true)
    if [ -n "$missing" ]; then
        echo "Installing missing packages: $(echo "$missing" | tr '\n' ' ')"
        confirm=""
        [ -t 0 ] || confirm="--noconfirm"  # there's nobody to answer when run without a terminal
        # shellcheck disable=SC2086
        sudo pacman -S --needed $confirm $missing
    fi
else
    for tool in fio smartctl udisksctl; do
        command -v "$tool" >/dev/null || echo "Warning: $tool is missing. DriveScope needs: $packages" >&2
    done
    /usr/bin/python3 -c "import gi, cairo" 2>/dev/null || echo "Warning: GTK's Python bindings are missing. DriveScope needs: $packages" >&2
fi

bin_dir="$HOME/.local/bin"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
apps_dir="$data_home/applications"
mkdir -p "$bin_dir" "$apps_dir" "$data_home/icons"
cp -r "$root/data/icons/hicolor" "$data_home/icons/"
# Launchers read this cache when one exists, so refresh it if it's there.
if [ -f "$data_home/icons/hicolor/index.theme" ] && command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -q -t "$data_home/icons/hicolor" || true
fi
ln -sf "$root/bin/drivescope" "$bin_dir/drivescope"
sed "s|^Exec=.*|Exec=$bin_dir/drivescope|" "$root/data/dev.drivescope.DriveScope.desktop" \
    > "$apps_dir/dev.drivescope.DriveScope.desktop"

if $with_temperature; then
    echo "Allowing temperature to be read without a prompt. This needs your password."
    sudo install -m 644 "$root/data/$policy" /usr/share/polkit-1/actions/
fi

echo "Installed. Open DriveScope from your app launcher, or run: drivescope"
