# Product

<!-- impeccable:product-schema 1 -->

## Platform

Native Linux desktop (the schema's `web`, `ios`, `android` and `adaptive` values don't apply).

## Stack

Python 3 (system interpreter), GTK4, libadwaita, cairo-drawn graphs, `fio` as the benchmark engine, `smartmontools` for SMART data. Chosen and approved in the build plan.

## Users

The owner, on an Omarchy (Arch, Hyprland) machine, with external USB drives. Shared later through GitHub or with friends, so terms like IOPS, UAS and p99 need plain-language explanations nearby and setup needs documenting.

The user comes for two jobs about equally: watching a real transfer in progress, and judging whether a drive, cable or port performs as it should.

## Product Purpose

Show the real-world read and write speed of external drives, and explain what limits it. Success means the user can trust the number, knows what is holding the drive back, and knows when it is safe to unplug.

## Positioning

Measures the block device itself, separately from what the application sees, so cached speed can't pass as drive speed. Every benchmark is checked against what the drive actually did.

## Operating Context

Often open beside a running copy or backup, in a tiled window that may be small. Drives get plugged in and pulled out while it runs.

## Capabilities and Constraints

- Live monitor, benchmarks, connection and health, history and export.
- Never writes to a raw device. Write tests only touch a dedicated test directory on a writable, non-system volume.
- A read-only volume gets read tests and monitoring only.
- Dirty and Writeback data are system-wide unless running as root, so "safe to unplug" is shown only when provable.

## Brand Commitments

The logo and wordmark supplied by the owner, in light and dark versions, are binding, and live in `data/brand/` as unaltered SVG masters. Icons in `data/icons/` and the `*-tight` wordmarks are generated from them by `data/brand/render-assets.sh`. Inside the app, the artwork follows the theme. The launcher icon can't change with the theme, so it uses the dark logo, whose bright blue reads on both. Don't recolor the artwork.

## Evidence on Hand

No published benchmarks or vendor speed claims. Don't fabricate any: every number on screen comes from a measurement on this machine.

## Product Principles

1. A number is shown only with what it measured. Apparent and on-disk speed are never merged.
2. Say "unknown" or "inconclusive" rather than imply certainty the data doesn't support.
3. The user's data comes first. Anything that writes is explicit, confirmed and cleaned up.
4. Explain terms in plain language, without hiding the technical detail.
