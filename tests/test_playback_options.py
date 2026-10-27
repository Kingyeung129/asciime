import argparse
import contextlib
import io
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from asciime import __main__ as cli
from asciime.core.config_manager import ConfigManager


class PlaybackOptionTests(unittest.TestCase):
    def test_short_flags_match_long_flags(self):
        for short, long in (
            (["-c", "1", "-R", "auto", "-a", "cpu", "-n", "5"],
             ["--cache", "1", "--renderer", "auto", "--acceleration", "cpu", "--repeat", "5"]),
            (["-r", "-s"], ["--random", "--screensaver"]),
        ):
            with patch("sys.argv", ["asciime", *short]):
                short_args = cli.parse_arguments()
            with patch("sys.argv", ["asciime", *long]):
                self.assertEqual(short_args, cli.parse_arguments())

    def test_repeat_validation_and_conflicts(self):
        for options in (["-n", "0"], ["-n", "-1"], ["-n", "1.5"],
                        ["-n", "abc"], ["-n"], ["-s", "-n", "2"],
                        ["-r", "-c", "1"]):
            with self.subTest(options=options):
                with patch("sys.argv", ["asciime", *options]):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            cli.parse_arguments()
                    self.assertEqual(error.exception.code, 2)

    def test_cli_options(self):
        with patch("sys.argv", ["asciime", "--cache", "1", "--renderer", "auto",
                                "--acceleration", "gpu"]):
            args = cli.parse_arguments()
        self.assertEqual(args.cache, "1")
        self.assertEqual(args.renderer, "auto")
        self.assertEqual(args.acceleration, "gpu")

    def test_config_persists_options_and_preserves_default_style(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ConfigManager(directory)
            config = manager.load_config()
            self.assertEqual(config["renderer"], "blocks")
            self.assertEqual(config["acceleration"], "auto")
            config.update(renderer="auto", acceleration="cpu")
            manager.save_config(config)
            self.assertEqual(manager.load_config()["renderer"], "auto")
            self.assertEqual(manager.load_config()["acceleration"], "cpu")
            with self.assertLogs("asciime.core.config_manager", level="WARNING"):
                config = manager._validate_config({"renderer": "wrong", "acceleration": "bad"})
            self.assertEqual(config["renderer"], "blocks")
            self.assertEqual(config["acceleration"], "auto")


class PlaybackWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_override_reaches_cache_and_random_modes(self):
        for mode, target in (("cache", "play_cached_gif"), ("random", "play_random_gif")):
            for screensaver, repeat, expected in ((True, None, 0), (False, 5, 5),
                                                  (False, None, 2)):
                args = argparse.Namespace(
                    cache_path="local.gif" if mode == "cache" else None,
                    random=mode == "random", screensaver=screensaver, repeat=repeat,
                )
                with self.subTest(mode=mode, screensaver=screensaver, repeat=repeat):
                    with patch.object(cli, "ConfigManager") as manager:
                        manager.return_value.load_config.return_value = {"loop_count": 2}
                        with patch.object(cli, "setup_logging"):
                            with patch.object(cli, target, new=AsyncMock()) as play:
                                await cli.async_main(args)
                    self.assertEqual(play.call_args.args[0]["loop_count"], expected)

    async def test_cached_and_random_players_receive_options(self):
        config = {"display_mode": "truecolor", "api_url": "http://localhost/api",
                  "renderer": "kitty", "acceleration": "cpu", "loop_count": 2}
        for play, args in ((cli.play_cached_gif, (config, "local.gif")),
                           (cli.play_random_gif, (config,))):
            with self.subTest(play=play.__name__):
                with patch.object(cli, "GIFPlayer") as player:
                    player.return_value.play_animation = AsyncMock()
                    with patch.object(cli, "CacheManager"):
                        with patch.object(cli, "GIFFetcher") as fetcher:
                            fetcher.return_value._init_session = AsyncMock()
                            fetcher.return_value.fetch_random = AsyncMock(return_value="local.gif")
                            fetcher.return_value.close = AsyncMock()
                            await play(*args)
                self.assertEqual(player.call_args.kwargs["renderer"], "kitty")
                self.assertEqual(player.call_args.kwargs["acceleration"], "cpu")

    async def test_cli_overrides_reach_cache_playback_without_network(self):
        args = argparse.Namespace(cache_path="local.gif", random=False,
                                  renderer="kitty", acceleration="cpu")
        with patch.object(cli, "ConfigManager") as manager:
            manager.return_value.load_config.return_value = {"debug": False}
            with patch.object(cli, "setup_logging"):
                with patch.object(cli, "play_cached_gif", new=AsyncMock()) as play:
                    with patch.object(cli, "GIFFetcher") as fetcher:
                        await cli.async_main(args)
        fetcher.assert_not_called()
        self.assertEqual(play.call_args.args[0]["renderer"], "kitty")
        self.assertEqual(play.call_args.args[0]["acceleration"], "cpu")

    async def test_normal_mode_player_receives_options(self):
        config = {
            "display_mode": "truecolor", "api_url": "http://localhost/api",
            "renderer": "blocks", "acceleration": "cpu", "loop_count": 2,
            "prefetch_count": 1, "max_cache_size_mb": 100, "max_cache_age_days": 30,
        }
        args = argparse.Namespace(cache_path=None, random=False,
                                  renderer="auto", acceleration="gpu", repeat=5)
        with patch.object(cli, "ConfigManager") as manager:
            manager.return_value.load_config.return_value = config
            with patch.object(cli, "setup_logging"):
                with patch.object(cli, "CacheManager") as cache:
                    cache.return_value.get_random_cached = AsyncMock(return_value="local.gif")
                    cache.return_value.cleanup_old = AsyncMock()
                    with patch.object(cli, "GIFPlayer") as player:
                        player.return_value.play_animation = AsyncMock()
                        with patch.object(cli, "GIFFetcher") as fetcher:
                            fetcher.return_value._init_session = AsyncMock()
                            fetcher.return_value.prefetch_batch = AsyncMock()
                            fetcher.return_value.close = AsyncMock()
                            await cli.async_main(args)
        self.assertEqual(player.call_args.kwargs["renderer"], "auto")
        self.assertEqual(player.call_args.kwargs["acceleration"], "gpu")
        self.assertEqual(player.call_args.kwargs["loop_count"], 5)
