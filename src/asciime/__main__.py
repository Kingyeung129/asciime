"""
ASCIIme main module - Handles program initialization and main execution flow.
"""
import asyncio
import os
import random
import sys
from typing import Dict, List, Optional
import signal
import argparse
import logging
from pathlib import Path

from asciime.core.cache_manager import CacheManager
from asciime.core.gif_fetcher import GIFFetcher
from asciime.core.gif_player_fast import GIFPlayer
from asciime.core.config_manager import ConfigManager

def setup_logging(debug: bool = False) -> None:
    """Configure logging with appropriate level and format."""
    level = logging.DEBUG if debug else logging.INFO
    log_dir = Path.home() / ".cache" / "asciime"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                log_dir / "asciime.log",
                encoding="utf-8"
            )
        ]
    )

logger = logging.getLogger(__name__)

class ASCIImeError(Exception):
    """Base exception class for ASCIIme-specific errors."""
    pass

class GIFPlaybackError(ASCIImeError):
    """Raised when there's an error playing a GIF."""
    pass

class FetchError(ASCIImeError):
    """Raised when there's an error fetching a GIF."""
    pass

class CacheSelectionError(ASCIImeError):
    """Raised when a GIF cannot be selected from the cache."""
    pass

def match_cached_gifs(entries: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    """Match a list number, exact name, or partial name against cached GIFs."""
    query = query.strip()
    if query.isdigit():
        index = int(query)
        return [entries[index - 1]] if 1 <= index <= len(entries) else []

    lowered = query.lower()
    if lowered.endswith(".gif"):
        lowered = lowered[:-4]
    exact = [
        e for e in entries
        if lowered in (e["name"].lower(), e["label"].lower())
    ]
    if exact:
        return exact
    return [e for e in entries if lowered in e["label"].lower()]

def print_cached_gifs(entries: List[Dict[str, str]]) -> None:
    width = len(str(len(entries)))
    for number, entry in enumerate(entries, 1):
        print(f"  {number:>{width}}. {entry['label']}")

def choose_cached_gif(cache_manager: CacheManager, query: Optional[str]) -> Optional[str]:
    """Pick a cached GIF by query or interactive prompt.

    Returns the GIF path, or None if the user cancelled the prompt.
    """
    entries = cache_manager.list_cached()
    if not entries:
        raise CacheSelectionError(
            f"No cached GIFs found in {cache_manager.cache_dir}. "
            "Run asciime online first to download some."
        )

    if query:
        matches = match_cached_gifs(entries, query)
        if len(matches) == 1:
            return matches[0]["path"]
        if not matches:
            raise CacheSelectionError(f"No cached GIF matches '{query}'. Use --cache to list them.")
        print(f"'{query}' matches several cached GIFs:", file=sys.stderr)
        print_cached_gifs(matches)
        raise CacheSelectionError("Be more specific, or pick one by number with --cache.")

    if not sys.stdin.isatty():
        print_cached_gifs(entries)
        raise CacheSelectionError("Pass a number or name, e.g. asciime --cache 1")

    print(f"Cached GIFs ({cache_manager.cache_dir}):")
    print_cached_gifs(entries)
    while True:
        try:
            answer = input(
                f"Choose a GIF [1-{len(entries)} or name, Enter = random, q = quit]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if answer.lower() in ("q", "quit", "exit"):
            return None
        if not answer:
            return random.choice(entries)["path"]

        matches = match_cached_gifs(entries, answer)
        if len(matches) == 1:
            return matches[0]["path"]
        if not matches:
            print(f"No cached GIF matches '{answer}'.")
        else:
            print("Several GIFs match:")
            print_cached_gifs(matches)

async def play_cached_gif(config: Dict, gif_path: str) -> None:
    """Play a GIF chosen from the cache without contacting the API."""
    gif_player = GIFPlayer(
        config["display_mode"],
        terminal_title="ASCIIme",
        loop_count=config.get("loop_count", 3),
        renderer=config.get("renderer", "blocks"),
        acceleration=config.get("acceleration", "auto"),
    )
    try:
        CacheManager().mark_played(gif_path)
    except Exception as e:
        logger.debug(f"Could not record playback of {gif_path}: {e}")

    logger.debug(f"Playing cached GIF {gif_path}")
    try:
        await gif_player.play_animation(gif_path)
    except Exception as e:
        logger.error(f"Error playing cached GIF: {e}", exc_info=True)
        raise GIFPlaybackError(f"Failed to play GIF: {str(e)}")

async def play_random_gif(config: Dict) -> None:
    """Play a single random GIF without caching or prefetching.
    
    Args:
        config: Configuration dictionary containing display settings
        
    Raises:
        GIFPlaybackError: If there's an error during playback
        FetchError: If there's an error fetching the GIF
    """
    gif_fetcher = None
    try:
        cache_manager = CacheManager()
        gif_fetcher = GIFFetcher(config["api_url"], cache_manager)
        logger.debug("Initializing GIF fetcher session")
        await gif_fetcher._init_session()
        
        gif_player = GIFPlayer(
            config["display_mode"],
            terminal_title="ASCIIme",
            loop_count=config.get("loop_count", 3),
            renderer=config.get("renderer", "blocks"),
            acceleration=config.get("acceleration", "auto"),
        )
        logger.debug("GIF player initialized")
        
        category = config.get("preferred_category")
        gif = await gif_fetcher.fetch_random(category)
        if not gif:
            raise FetchError("Failed to fetch random GIF")
            
        logger.debug(f"Playing GIF from {gif}")
        await gif_player.play_animation(gif)

    except Exception as e:
        logger.error(f"Error playing random GIF: {e}", exc_info=True)
        raise GIFPlaybackError(f"Failed to play GIF: {str(e)}")

    finally:
        if gif_fetcher:
            await gif_fetcher.close()

async def async_cleanup_cache(
    cache_manager: CacheManager,
    config: Dict
) -> None:
    """Clean up old cached files.
    
    Args:
        cache_manager: Instance of CacheManager
        config: Configuration dictionary containing cache settings
    """
    try:
        await cache_manager.cleanup_old(
            max_age_days=config["max_cache_age_days"],
            max_size_mb=config["max_cache_size_mb"],
        )
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}", exc_info=True)

async def async_main(args: argparse.Namespace) -> None:
    """Main async execution flow.
    
    Args:
        args: Parsed command line arguments
    """
    gif_fetcher = None
    try:
        config_manager = ConfigManager()
        config = config_manager.load_config()
        for option in ("renderer", "acceleration"):
            override = getattr(args, option, None)
            if override is not None:
                config[option] = override
        if getattr(args, "screensaver", False):
            config["loop_count"] = 0
        elif getattr(args, "repeat", None) is not None:
            config["loop_count"] = args.repeat
        
        # Set up logging based on config
        setup_logging(config.get("debug", False))

        if args.cache_path:
            await play_cached_gif(config, args.cache_path)
            return

        if args.random:
            await play_random_gif(config)
            return

        cache_manager = CacheManager()
        cleanup_task = asyncio.create_task(
            async_cleanup_cache(cache_manager, config)
        )

        gif_player = GIFPlayer(
            config["display_mode"],
            terminal_title="ASCIIme",
            loop_count=config.get("loop_count", 3),
            renderer=config.get("renderer", "blocks"),
            acceleration=config.get("acceleration", "auto"),
        )

        gif_fetcher = GIFFetcher(config["api_url"], cache_manager)
        await gif_fetcher._init_session()

        # Try to get a cached GIF first
        gif: Optional[str] = None
        try:
            category = config.get("preferred_category")
            gif = await cache_manager.get_random_cached(category)
        except Exception as e:
            logger.warning(f"Error fetching cached GIF: {e}")

        # Fetch new GIF if no cached one available
        if not gif:
            try:
                category = config.get("preferred_category")
                gif = await gif_fetcher.fetch_random(category)
                if not gif:
                    raise FetchError("Failed to fetch random GIF")
            except Exception as e:
                logger.error(f"Error fetching GIF: {e}", exc_info=True)
                raise FetchError(f"Failed to fetch GIF: {str(e)}")

        # Start prefetching in background
        prefetch_task = asyncio.create_task(
            gif_fetcher.prefetch_batch(
                config["prefetch_count"],
                config.get("preferred_category")
            )
        )

        await gif_player.play_animation(gif)

        try:
            await asyncio.gather(cleanup_task, prefetch_task)
        except Exception as e:
            logger.warning(f"Background task error: {e}")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

    finally:
        if gif_fetcher:
            await gif_fetcher.close()

def positive_repeat(value: str) -> int:
    """Parse a positive total loop count."""
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("repeat count must be a positive integer") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("repeat count must be a positive integer")
    return count

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments.
    
    Returns:
        Parsed argument namespace
    """
    parser = argparse.ArgumentParser(
        description="ASCIIme - Turn your terminal into a kawaii anime experience!"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-r", "--random",
        action="store_true",
        help="Fetch and play a random GIF without prefetching or cleanup",
    )
    mode.add_argument(
        "-c", "--cache",
        nargs="?",
        const="",
        default=None,
        metavar="GIF",
        help=(
            "Play a GIF from the local cache without using the API. "
            "Without a value, list cached GIFs and choose one; otherwise "
            "pass a list number or (partial) name"
        ),
    )
    parser.add_argument(
        "-R", "--renderer", choices=("blocks", "kitty", "auto"), default=None,
        help="Block art (default), native Kitty graphics, or terminal auto-detection",
    )
    parser.add_argument(
        "-a", "--acceleration", choices=("auto", "cpu", "gpu"), default=None,
        help="Block resizing backend: auto (default), CPU, or required OpenGL GPU",
    )
    playback = parser.add_mutually_exclusive_group()
    playback.add_argument(
        "-s", "--screensaver", action="store_true",
        help="Loop the selected GIF forever, until Ctrl-C or SIGTERM",
    )
    playback.add_argument(
        "-n", "--repeat", type=positive_repeat, metavar="N",
        help="Play the selected GIF N times, overriding the configured loop count",
    )
    return parser.parse_args()

def main() -> None:
    """Entry point for the ASCIIme application."""
    loop = None
    original_handlers = {}
    try:
        args = parse_arguments()
        args.cache_path = None

        if args.cache is not None:
            args.cache_path = choose_cached_gif(CacheManager(), args.cache)
            if args.cache_path is None:
                return

        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        main_task = loop.create_task(async_main(args))
        
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            original_handlers[s] = signal.signal(
                s, lambda signum, frame: loop.call_soon_threadsafe(main_task.cancel)
            )


        loop.run_until_complete(main_task)

    except asyncio.CancelledError:
        logger.info("Playback stopped")
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user")
    except CacheSelectionError as e:
        print(f"asciime: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if loop is not None:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            if hasattr(loop, "shutdown_default_executor"):
                loop.run_until_complete(loop.shutdown_default_executor())
            for signum, handler in original_handlers.items():
                signal.signal(signum, handler)
            loop.close()
            asyncio.set_event_loop(None)

if __name__ == "__main__":
    main()
