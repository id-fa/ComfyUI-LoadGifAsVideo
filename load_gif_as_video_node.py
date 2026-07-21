"""LoadGifAsVideo — read an animated GIF/APNG/WEBP and emit it as a VIDEO stream."""

import os
from fractions import Fraction

import numpy as np
import torch
from PIL import Image, ImageSequence

import folder_paths

from .video_length import length_inputs, resolve_frame_count, scaled_frame_rate

try:
    from comfy_api.input_impl import VideoFromComponents
    from comfy_api.util import VideoComponents

    _VIDEO_IMPORT_ERROR = None
except ImportError as exc:  # ComfyUI too old to have the VIDEO type
    VideoFromComponents = None
    VideoComponents = None
    _VIDEO_IMPORT_ERROR = exc

# Formats Pillow can decode as an animation. ".png" covers APNG.
_ANIMATION_EXTENSIONS = (".gif", ".webp", ".png", ".apng")

# GIFs routinely declare 0ms or 10ms delays meaning "as fast as possible";
# every browser substitutes 100ms instead, so we match that playback.
_MIN_DELAY_MS = 20
_DEFAULT_DELAY_MS = 100

_BACKGROUNDS = {"black": 0.0, "white": 1.0}


def _list_animation_files():
    input_dir = folder_paths.get_input_directory()
    try:
        names = os.listdir(input_dir)
    except OSError:
        return []
    return sorted(
        f
        for f in names
        if f.lower().endswith(_ANIMATION_EXTENSIONS)
        and os.path.isfile(os.path.join(input_dir, f))
    )


def _read_animation(path, background):
    """Decode every frame to RGB float32 [H, W, 3], plus each frame's delay in ms.

    Transparent pixels are composited over `background` because VIDEO carries no
    alpha channel.
    """
    bg = _BACKGROUNDS[background]
    frames = []
    delays = []
    with Image.open(path) as img:
        for frame in ImageSequence.Iterator(img):
            rgba = np.asarray(frame.convert("RGBA"), dtype=np.float32) / 255.0
            alpha = rgba[..., 3:4]
            frames.append(rgba[..., :3] * alpha + bg * (1.0 - alpha))

            delay = frame.info.get("duration", 0)
            try:
                delay = int(delay)
            except (TypeError, ValueError):
                delay = 0
            delays.append(delay if delay >= _MIN_DELAY_MS else _DEFAULT_DELAY_MS)

    if not frames:
        raise ValueError(f"No frames could be decoded from '{path}'")

    # Frame sizes may differ across frames in a malformed file; Pillow normally
    # composites onto the full canvas, but bail out clearly if it did not.
    shapes = {f.shape for f in frames}
    if len(shapes) > 1:
        raise ValueError(
            f"Frames of '{path}' have inconsistent sizes: {sorted(shapes)}"
        )

    return frames, delays


def _to_uniform_timebase(frames, delays):
    """Put the source on a single frame rate and return (frames, fps).

    An animation whose frames all share one delay is returned untouched — the
    output is then a bit-exact copy of the source at its own rate. Variable-delay
    animations are nearest-hold resampled onto their average rate, which keeps the
    frame count and the total duration identical to the source.
    """
    count = len(frames)
    total_ms = sum(delays)
    if total_ms <= 0:
        return frames, Fraction(1000, _DEFAULT_DELAY_MS)

    fps = Fraction(count * 1000, total_ms).limit_denominator(10000)
    if len(set(delays)) <= 1:
        return frames, fps

    step_ms = total_ms / count
    ends = np.cumsum(delays)
    resampled = [
        frames[
            min(
                int(np.searchsorted(ends, (i + 0.5) * step_ms, side="right")), count - 1
            )
        ]
        for i in range(count)
    ]
    return resampled, fps


class LoadGifAsVideo:
    """Load an animated GIF/APNG/WEBP from the input directory as a VIDEO."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file": (
                    # The leading blank is the default, so a freshly added node
                    # never silently points at whatever file happens to sort first.
                    ["", *_list_animation_files()],
                    {
                        "default": "",
                        "image_upload": True,
                        "tooltip": "Animated GIF / APNG / WEBP in the ComfyUI input directory.",
                    },
                ),
                **length_inputs("animation"),
                "background": (
                    list(_BACKGROUNDS),
                    {
                        "default": "black",
                        "tooltip": "Color transparent pixels are composited over — VIDEO has no alpha channel.",
                    },
                ),
            },
        }

    # `images` is appended, never inserted — existing workflows keep their links.
    RETURN_TYPES = ("VIDEO", "IMAGE")
    RETURN_NAMES = ("video", "images")
    FUNCTION = "load"
    CATEGORY = "load-gif-as-video"
    DESCRIPTION = (
        "Loads an animated GIF/APNG/WEBP as a VIDEO stream. The animation loops "
        "until the requested frame count or duration is filled, and the playback "
        "speed is adjustable."
    )

    def load(self, file, length_mode, frames, seconds, loops, speed, background):
        if VideoFromComponents is None or VideoComponents is None:
            raise RuntimeError(
                "This ComfyUI build has no VIDEO support (comfy_api.input_impl is "
                f"unavailable): {_VIDEO_IMPORT_ERROR}"
            )

        path = folder_paths.get_annotated_filepath(file)
        source_frames, delays = _read_animation(path, background)
        source_frames, base_fps = _to_uniform_timebase(source_frames, delays)

        fps = scaled_frame_rate(base_fps, speed)
        count = resolve_frame_count(
            length_mode, frames, seconds, loops, fps, len(source_frames)
        )

        # Looping is just a modulo over the source frames.
        stacked = np.stack(source_frames)
        images = torch.from_numpy(stacked[np.arange(count) % len(source_frames)])

        video = VideoFromComponents(VideoComponents(images=images, frame_rate=fps))
        return (video, images)

    @classmethod
    def IS_CHANGED(cls, file, **kwargs):
        if not file:
            return float("nan")
        path = folder_paths.get_annotated_filepath(file)
        return os.path.getmtime(path)

    @classmethod
    def VALIDATE_INPUTS(cls, file, **kwargs):
        if not file:
            return "No animation file selected"
        if not folder_paths.exists_annotated_filepath(file):
            return f"Invalid animation file: {file}"
        return True


NODE_CLASS_MAPPINGS = {
    "LoadGifAsVideo": LoadGifAsVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadGifAsVideo": "Load GIF as Video",
}
