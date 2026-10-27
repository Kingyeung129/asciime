import sys
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageChops, ImageStat

from asciime.core.image_accelerator import GPUUnavailableError, ImageResizer


class ImageResizerTests(unittest.TestCase):
    def test_cpu_matches_pillow_lanczos(self):
        image = Image.effect_noise((60, 40), 60).convert("RGBA")
        resizer = ImageResizer("cpu")
        try:
            result = resizer.resize(image, (17, 11))
            expected = image.resize((17, 11), Image.Resampling.LANCZOS)
            self.assertEqual(result.tobytes(), expected.tobytes())
        finally:
            resizer.close()

    def test_missing_extra_falls_back_with_message(self):
        with patch.dict(sys.modules, {"moderngl": None}):
            with self.assertLogs("asciime.core.image_accelerator", level="DEBUG"):
                resizer = ImageResizer("auto")
            self.assertIsNone(resizer._context)
            self.assertEqual(resizer.resize(Image.new("RGBA", (8, 8)), (4, 4)).size, (4, 4))
            resizer.close()

    def test_explicit_gpu_reports_missing_extra(self):
        with patch.dict(sys.modules, {"moderngl": None}):
            with self.assertRaisesRegex(GPUUnavailableError, "asciime\\[gpu\\]"):
                ImageResizer("gpu")

    def test_software_context_is_not_claimed_as_gpu(self):
        module = MagicMock()
        context = module.create_standalone_context.return_value
        context.info = {"GL_RENDERER": "llvmpipe"}
        with patch.dict(sys.modules, {"moderngl": module}):
            with self.assertLogs("asciime.core.image_accelerator", level="DEBUG"):
                resizer = ImageResizer("auto")
        context.release.assert_called_once()
        self.assertIsNone(resizer._context)

    def test_runtime_failure_falls_back_and_releases_context(self):
        resizer = ImageResizer("cpu")
        resizer.acceleration = "auto"
        context = resizer._context = MagicMock()
        image = Image.new("RGBA", (8, 8), "red")
        with patch.object(resizer, "_resize_gpu", side_effect=RuntimeError("device lost")):
            with self.assertLogs("asciime.core.image_accelerator", level="DEBUG"):
                result = resizer.resize(image, (4, 4))
        context.release.assert_called_once()
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertIsNone(resizer._context)

    def test_explicit_gpu_runtime_failure_is_not_silenced(self):
        resizer = ImageResizer("cpu")
        resizer.acceleration = "gpu"
        resizer._context = MagicMock()
        with patch.object(resizer, "_resize_gpu", side_effect=RuntimeError("device lost")):
            with self.assertRaisesRegex(GPUUnavailableError, "device lost"):
                resizer.resize(Image.new("RGBA", (8, 8)), (4, 4))
        self.assertIsNone(resizer._context)

    def test_hardware_resize_preserves_orientation_alpha_and_detail(self):
        try:
            resizer = ImageResizer("gpu")
        except GPUUnavailableError as exc:
            self.skipTest(str(exc))
        try:
            image = Image.new("RGBA", (96, 64))
            for y in range(image.height):
                for x in range(image.width):
                    image.putpixel((x, y), (x * 255 // 95, y * 255 // 63, 120, 255))
            for size in ((31, 19), (151, 97), (1, 1)):
                with self.subTest(size=size):
                    actual = resizer.resize(image, size)
                    expected = image.resize(size, Image.Resampling.LANCZOS)
                    difference = ImageStat.Stat(ImageChops.difference(actual, expected))
                    self.assertLess(max(difference.mean), 3)
                    self.assertEqual(actual.size, size)
            transparent = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
            self.assertEqual(
                resizer.resize(transparent, (8, 8)).getpixel((3, 3)), (0, 0, 0, 0)
            )
            translucent = Image.new("RGBA", (16, 16), (20, 80, 160, 128))
            self.assertEqual(
                resizer.resize(translucent, (8, 8)).getpixel((3, 3)), (20, 80, 160, 128)
            )
            edge = Image.new("RGBA", (64, 64), (0, 255, 0, 0))
            edge.paste((255, 0, 0, 255), (16, 16, 48, 48))
            actual = resizer.resize(edge, (23, 23))
            self.assertEqual(actual.getpixel((11, 11)), (255, 0, 0, 255))
            self.assertEqual(actual.getchannel("G").getextrema(), (0, 0))
            self.assertEqual(actual.getchannel("B").getextrema(), (0, 0))
        finally:
            resizer.close()
