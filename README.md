# ASCIIme

[![PyPI version](https://badge.fury.io/py/asciime.svg)](https://badge.fury.io/py/asciime)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Turn your terminal into a kawaii anime experience! ASCIIme is a command-line tool that displays animated ASCII art GIFs in your terminal, with a focus on anime and kawaii content.


## Features

- Display animated GIFs as ASCII art in your terminal
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
```

Cached GIFs are stored in `~/.cache/asciime/<category>/<id>.gif`. With
`--cache` and no value, ASCIIme lists them and prompts you to choose one by
number or name; press Enter for a random one or `q` to quit.

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
work with normal mode, `--random`, and `--cache`, and override `loop_count` in
the configuration for this run. `--repeat N` requires a positive integer;
use `--screensaver` for unlimited loops. They cannot be combined. The terminal
is restored on Ctrl-C or SIGTERM.

| Flag | Short form | Purpose |
|------|------------|---------|
| `--help` | `-h` | Show help |
| `--random` | `-r` | Fetch a random GIF |
| `--cache [GIF]` | `-c [GIF]` | Choose a cached GIF |
| `--renderer MODE` | `-R MODE` | Select `blocks`, `kitty`, or `auto` |
| `--acceleration MODE` | `-a MODE` | Select `auto`, `cpu`, or `gpu` |
| `--screensaver` | `-s` | Loop indefinitely |
| `--repeat N` | `-n N` | Set the total animation loop count |

### Smoother and higher-detail playback

Frames are prepared before playback, so image scaling and character conversion
do not interrupt the first loop. Playback follows the GIF's frame durations and
skips overdue frames if terminal output cannot keep up. This does not generate
extra frames or recover detail missing from the source GIF. Preparation takes
some time and caches frames in memory.

Block art remains the default. For full image resolution instead of character
cells, run this in a Kitty-graphics-compatible terminal, such as Kitty or Ghostty:

```bash
asciime --cache --renderer auto
# Explicitly request the Kitty graphics protocol
asciime --cache nier --renderer kitty
```

`auto` detects Kitty/Ghostty terminal environment variables and otherwise uses
blocks. Native images are uploaded once and reused across loops; scaling and
display are handled by the terminal, including its GPU renderer when available.
The terminal must support the Kitty graphics protocol, not just GPU-accelerated
text. Native output is currently disabled inside tmux/screen. Use `blocks`
for ordinary terminals or when you prefer the character-art look.
`display_mode` controls block art; native images retain their original colors.

For optional GPU resizing of block art:

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
    "renderer": "blocks",            // "blocks", "kitty", or "auto"
    "acceleration": "auto",          // "auto", "cpu", or "gpu" (block resizing)
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