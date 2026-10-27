"""Optional OpenGL Lanczos resizing; GIF decoding remains on the CPU."""

import logging
import sys
from typing import Tuple

from PIL import Image

logger = logging.getLogger(__name__)

VERTEX_SHADER = """
#version 330
void main() {
    vec2 positions[3] = vec2[3](vec2(-1, -1), vec2(3, -1), vec2(-1, 3));
    gl_Position = vec4(positions[gl_VertexID], 0, 1);
}
"""

FRAGMENT_SHADER = """
#version 330
uniform sampler2D source;
uniform ivec2 source_size;
uniform ivec2 target_size;
uniform bool horizontal;
out vec4 result;

float lanczos(float x) {
    x = abs(x);
    if (x < 0.00001) return 1.0;
    if (x >= 3.0) return 0.0;
    float p = 3.14159265359 * x;
    return sin(p) * sin(p / 3.0) / (p * p / 3.0);
}

void main() {
    ivec2 pixel = ivec2(gl_FragCoord.xy);
    int axis = horizontal ? 0 : 1;
    float ratio = float(source_size[axis]) / float(target_size[axis]);
    float scale = max(1.0, ratio);
    float center = (float(pixel[axis]) + 0.5) * ratio - 0.5;
    vec4 total = vec4(0.0);
    float weights = 0.0;
    int start = int(ceil(center - 3.0 * scale));
    int end = int(floor(center + 3.0 * scale));
    for (int i = start; i <= end; i++) {
        ivec2 coord = pixel;
        coord[axis] = clamp(i, 0, source_size[axis] - 1);
        vec4 value = texelFetch(source, coord, 0);
        if (horizontal) value.rgb *= value.a;
        float weight = lanczos((float(i) - center) / scale);
        total += weight * value;
        weights += weight;
    }
    result = total / weights;
    if (!horizontal) {
        result.rgb = result.a > 0.00001 ? result.rgb / result.a : vec3(0.0);
        result = clamp(result, 0.0, 1.0);
    }
}
"""


class GPUUnavailableError(RuntimeError):
    """The requested hardware image-processing backend is unavailable."""


class ImageResizer:
    """Own an OpenGL context on the frame-preparation worker thread."""

    def __init__(self, acceleration: str = "auto"):
        self.acceleration = acceleration
        self._context = None
        self._program = None
        self._vao = None
        if acceleration == "cpu":
            return
        try:
            import moderngl
        except ImportError as exc:
            self._unavailable(
                "GPU support is not installed; install 'asciime[gpu]' to enable it", exc
            )
            return

        try:
            if sys.platform.startswith("linux"):
                self._context = moderngl.create_standalone_context(require=330, backend="egl")
            else:
                self._context = moderngl.create_standalone_context(require=330)
            renderer = self._context.info["GL_RENDERER"]
            if any(name in renderer.lower() for name in (
                "llvmpipe", "softpipe", "software", "swr", "swiftshader",
                "lavapipe", "gdi generic", "microsoft basic render",
            )):
                raise GPUUnavailableError(f"OpenGL selected a software renderer: {renderer}")
            self._program = self._context.program(
                vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER
            )
            self._vao = self._context.vertex_array(self._program, [])
            logger.debug("GPU image resizing enabled: %s", renderer)
        except Exception as exc:
            self.close()
            self._unavailable(f"GPU initialization failed: {exc}", exc)

    def _unavailable(self, message: str, exc: Exception) -> None:
        if self.acceleration == "gpu":
            raise GPUUnavailableError(message) from exc
        # Auto mode runs inside the playback screen, so stay quiet unless debugging.
        logger.debug("%s; using CPU Lanczos resizing", message)

    def resize(self, image: Image.Image, size: Tuple[int, int]) -> Image.Image:
        if self._context is None:
            return image.resize(size, Image.Resampling.LANCZOS)
        try:
            return self._resize_gpu(image, size)
        except Exception as exc:
            self.close()
            self._unavailable(f"GPU resizing failed: {exc}", exc)
            return image.resize(size, Image.Resampling.LANCZOS)

    def _resize_gpu(self, image: Image.Image, size: Tuple[int, int]) -> Image.Image:
        context, program, vao = self._context, self._program, self._vao
        if context is None or program is None or vao is None:
            raise GPUUnavailableError("GPU context is not initialized")
        resources = []
        try:
            source = context.texture(image.size, 4, image.tobytes())
            resources.append(source)
            intermediate = context.texture((size[0], image.height), 4, dtype="f4")
            resources.append(intermediate)
            target = context.texture(size, 4)
            resources.append(target)
            for horizontal, texture, output in (
                (True, source, intermediate), (False, intermediate, target)
            ):
                framebuffer = context.framebuffer(color_attachments=[output])
                resources.append(framebuffer)
                framebuffer.use()
                context.viewport = (0, 0, *output.size)
                texture.use(0)
                program["source"].value = 0
                program["source_size"].value = texture.size
                program["target_size"].value = output.size
                program["horizontal"].value = horizontal
                vao.render(vertices=3)
            return Image.frombytes("RGBA", size, target.read(alignment=1))
        finally:
            for resource in reversed(resources):
                resource.release()

    def close(self) -> None:
        for resource in (self._vao, self._program, self._context):
            if resource is not None:
                resource.release()
        self._vao = self._program = self._context = None
