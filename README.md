# ASCIIme

[![PyPI version](https://badge.fury.io/py/asciime.svg)](https://badge.fury.io/py/asciime)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Turn your terminal into a kawaii anime experience! ASCIIme is a command-line tool that displays animated ASCII art GIFs in your terminal, with a focus on anime and kawaii content.


## Features

- Display animated GIFs as ASCII art in your terminal
- Static images (PNG, JPG, WebP, BMP, TIFF) and animated WebP/APNG
- High-detail half-block rendering (two pixels per character cell)
- Optional full-resolution images in Kitty-compatible terminals
- Optional OpenGL GPU acceleration for image resizing, with CPU fallback
- Support for true color and 256-color terminals
- Flicker-free, centered playback that adapts to terminal resizes
- Automatic caching system for faster loading
- Configurable display settings
- Category-based GIF selection
- Background prefetching for smooth playback

## Installation

```bash
# Install from PyPI
pip install asciime

# Or install from source
git clone https://github.com/Felixdiamond/asciime.git
cd asciime
pip install -e .
```

## Usage

Basic usage:

```bash
# Display a random GIF
asciime --random

# Normal mode with prefetching and caching
asciime

# Pick a previously downloaded GIF from the cache (works offline)
asciime --cache

# Play a cached GIF directly by list number or (partial) name
asciime --cache 3
asciime --cache nier

# Play any local GIF or image file
asciime --file ~/Pictures/wallpaper.png
asciime -f ./sticker.webp -s
```

Cached GIFs are stored in `~/.cache/asciime/<category>/<id>.gif`. With
`--cache` and no value, ASCIIme lists them and prompts you to choose one by
number or name; press Enter for a random one or `q` to quit. Names match with
or without their extension, so use `nier.png` to pick the PNG when a GIF has
the same name.

### Static images

`--cache` and `--file` accept GIF, PNG, APNG, JPG/JPEG, WebP, BMP and TIFF.
Animated WebP and APNG files play like GIFs. To add your own images to the
`--cache` picker, copy them into `~/.cache/asciime/` or a subfolder (the
subfolder name is used as the category). Files you play from the cache are
recorded in its database, so cache cleanup (`max_cache_age_days` and
`max_cache_size_mb`) may delete them later; use `--file` for images you want
to keep elsewhere.

A static image stays on screen for 3 seconds per loop, so `--repeat 5` shows
it for 15 seconds, and `--screensaver` keeps it up until Ctrl-C or SIGTERM.
Photos are rotated according to their EXIF orientation, and 16-bit PNGs are
scaled down to 8-bit color.

### Screensaver and repeat count

```bash
# Loop one selected cached GIF until Ctrl-C or SIGTERM
asciime -c nier -s
asciime --cache nier --screensaver

# Play the selected GIF exactly five times
asciime -c nier -n 5
asciime --random --repeat 5
```

Screensaver mode repeats the same selected GIF, not a playlist. Both options
work with normal mode, `--random`, `--cache` and `--file`, and override `loop_count` in
the configuration for this run. `--repeat N` requires a positive integer;
use `--screensaver` for unlimited loops. They cannot be combined. The terminal
is restored on Ctrl-C or SIGTERM.

| Flag | Short form | Purpose |
|------|------------|---------|
| `--help` | `-h` | Show help |
| `--random` | `-r` | Fetch a random GIF |
| `--cache [NAME]` | `-c [NAME]` | Choose a cached GIF or image |
| `--file PATH` | `-f PATH` | Play a local GIF or image file |
| `--renderer MODE` | `-R MODE` | Select `blocks`, `sextant`, `kitty`, `sixel`, or `auto` |
| `--acceleration MODE` | `-a MODE` | Select `auto`, `cpu`, or `gpu` |
| `--screensaver` | `-s` | Loop indefinitely |
| `--repeat N` | `-n N` | Set the total animation loop count |

### Smoother and higher-detail playback

Frames are prepared before playback, so image scaling and character conversion
do not interrupt the first loop. Playback follows the GIF's frame durations and
skips overdue frames if terminal output cannot keep up. This does not generate
extra frames or recover detail missing from the source GIF. Preparation takes
some time and caches frames in memory.

Half-block art (`blocks`) remains the default. Renderers, from most to least
pixelated:

| Renderer | Detail | Requirements |
|----------|--------|--------------|
| `blocks` | 1x2 pixels per cell | Any truecolor/256-color terminal |
| `sextant` | 2x3 pixels per cell | A font or terminal with Unicode 13 sextant glyphs |
| `sixel` | Real pixels | Sixel terminal: Windows Terminal 1.22+, WezTerm, foot, mlterm, xterm `-ti vt340` |
| `kitty` | Full source resolution | Kitty graphics protocol: Kitty, Ghostty |

```bash
# Pick the best renderer the terminal supports (Kitty, then Sixel, then blocks)
asciime --cache --renderer auto
# Windows Terminal 1.22 or newer
asciime -f wallpaper.png -R sixel
# Finer character art that still works over plain text terminals
asciime -c -R sextant
```

`auto` detects Kitty/Ghostty from environment variables, then asks the terminal
whether it supports Sixel (a device-attributes query with a short timeout),
and otherwise uses blocks. Native images are disabled inside tmux/screen in
`auto` mode; explicit `--renderer sixel` is allowed there for multiplexers that
pass Sixel through (tmux 3.4+ built with Sixel support).

Kitty images are uploaded once and scaled by the terminal. Sixel frames are
scaled to the terminal's pixel size (Windows Terminal uses 10x20 pixels per
cell) and reduced to a 255-color palette per frame; still images additionally
get Floyd-Steinberg dithering. Animated frames are encoded by parallel worker
processes while the first loop plays, so it may run slower than later loops.
Sixel frames of a large terminal are several hundred KB each; if the terminal
cannot draw that fast, frames are skipped to keep time. A smaller terminal
window (or larger font) gives smoother Sixel playback.

Sextant glyphs (U+1FB00-1FB3B) must be drawn by the terminal or font. Many
recent terminals have built-in drawing for them; otherwise use a font that
includes them, such as Cascadia Code or Iosevka. If you see boxes or question
marks, use `blocks`. `display_mode` controls text art colors;
`nocolor` uses ASCII art for `blocks`/`sextant` and grayscale Sixel.

For optional GPU resizing of block and sextant art (Sixel frames are scaled by
the CPU worker processes):

```bash
# From this source checkout
python -m pip install -e '.[gpu]'
asciime --cache --acceleration auto
# Require a hardware OpenGL context (report an error if unavailable)
asciime --cache --acceleration gpu
# Always use Pillow's CPU Lanczos resizer
asciime --cache --acceleration cpu
```

The GPU backend uses OpenGL 3.3 and a two-pass Lanczos filter, including
alpha-aware filtering. Linux requires working EGL drivers. It detects a usable
hardware context rather than just the presence of a GPU; software OpenGL
renderers are not counted as hardware acceleration. In `auto` mode, missing
dependencies, unsupported drivers, or GPU errors silently fall back to
CPU resizing (set `"debug": true` to log why). GIF decoding and terminal escape generation still run on the CPU.
GPU transfers and initialization can make small GIFs or older GPUs slower than
CPU resizing; GPU support does not guarantee a speedup.

The `--renderer` and `--acceleration` options work with cache playback, random
downloads, and normal mode, and override their corresponding config values.

**Note**: I'm using the Free Plan on render so my API usually sleeps in 5 mins or so. If you want 100% uptime, you can set up your own server by cloning the repo [asciime-api](https://github.com/Felixdiamond/asciime-api)

If you want to see a random anime gif whenever you open your terminal:

### For Bash users
Add this to your `~/.bashrc`:
```bash
# Display random ASCII anime GIF on terminal start
asciime
```

### For Zsh users
Add this to your `~/.zshrc`:
```bash
# Display random ASCII anime GIF on terminal start
asciime
```

### For Fish users
Add this to your `~/.config/fish/config.fish`:
```bash
# Display random ASCII anime GIF on terminal start
asciime
```

Not sure if this works for windows yet..

## Configuration

ASCIIme can be configured by editing `~/.config/asciime/config.json`. Available options:

```json
{
    "display_mode": "truecolor",     // Display mode: "truecolor", "256", "256fgbg", or "nocolor"
    "renderer": "blocks",            // "blocks", "sextant", "kitty", "sixel", or "auto"
    "acceleration": "auto",          // "auto", "cpu", or "gpu" (image resizing)
    "max_cache_size_mb": 100,        // Maximum cache size in megabytes
    "max_cache_age_days": 30,        // Maximum age of cached files in days
    "prefetch_count": 3,             // Number of GIFs to prefetch
    "api_url": "https://asciime-api.onrender.com/api",  // API endpoint
    "preferred_category": null,       // Preferred GIF category (null for random)
    "loop_count": 3,                 // Number of times to loop animation (0 for infinite)
    "debug": false                   // Enable debug logging
}
```

## Requirements

- Python 3.8 or higher
- Internet connection for fetching GIFs

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Acknowledgments

- Originally inspired by [gif-cli-fast](https://github.com/telnet23/gif-cli-fast)