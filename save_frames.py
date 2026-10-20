"""What `SaveAsGif` and `SaveAsAnimatedWebP` share: the frames before any format.

Both nodes take a VIDEO or an IMAGE batch, drop frames down to the requested
rate, scale into a `width` x `height` box, apply a tone curve and round to 8-bit
RGB. Only after that do they part ways — the GIF node quantizes and dithers,
the WebP node hands the frames to libwebp. Everything up to that point lives
here so the same widget block means the same thing on both nodes.
"""

from fractions import Fraction

import numpy as np
import torch
from PIL import Image

from .video_length import speed_baked_indices

# What `width` x `height` means when the source does not share its aspect ratio.
# `pad` is the original behavior and stays the default: the box is the canvas
# and the leftover is transparent bars. `fit` scales the frame into the box and
# writes it at that size, so the box is an upper bound rather than the output.
SIZE_MODES = ["pad", "fit"]

# Ordered best-quality-first for a downscale, which is what saving an animation
# usually is. `nearest` is last because it is the specialist: it is the only one
# that keeps pixel art and hard-edged animation from turning to mush.
RESAMPLERS = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "hamming": Image.Resampling.HAMMING,
    "box": Image.Resampling.BOX,
    "nearest": Image.Resampling.NEAREST,
}

# Canvas limit. Well past anything an animation file should be, and it only
# exists so a typo cannot ask for an allocation that takes the server down.
MAX_DIMENSION = 8192

# Matches the cap the loader nodes apply, so a batch that could be built there
# can always be saved.
MAX_FRAMES = 10000

# `gamma` follows the image-editor convention (Photoshop's Levels midtone,
# ImageMagick's -gamma): 1.0 is a no-op, above it brightens, below it darkens.
# The range is wide on purpose — a black mask screen at full strength halves the
# average tone, and pulling the midtones back up from that takes a value near 2.
MIN_GAMMA = 0.1
MAX_GAMMA = 10.0


def resolve_source(video, images, source_fps):
    """The frames to save and their rate, from whichever input is connected.

    A VIDEO carries its own frame rate; an IMAGE batch is timed by `source_fps`.
    Both connected is refused here rather than in `VALIDATE_INPUTS`, which never
    sees an unconnected optional input.
    """
    if video is not None and images is not None:
        raise ValueError(
            "Connect either `video` or `images`, not both — they would have "
            "different frame rates"
        )
    if video is not None:
        components = video.get_components()
        return components.images, components.frame_rate
    if images is not None:
        return images, source_fps
    raise ValueError("Connect a `video` or an `images` batch to save")


def decimate(count, source_rate, output_rate):
    """Indices that resample `count` frames down to `output_rate`, and the rate used.

    Dropping frames is the cheapest thing that shrinks an animation: half the
    frames is close to half the file. The playback time is unchanged, because
    the durations are then written at `output_rate` — 60 frames of 30fps
    material at 10fps becomes 20 frames that still run for two seconds.

    Returns `(None, source_rate)` when nothing should be dropped.
    """
    source_rate = Fraction(source_rate).limit_denominator(10000)
    output_rate = Fraction(output_rate).limit_denominator(1000)
    if output_rate <= 0 or source_rate <= 0:
        raise ValueError(
            f"Frame rates must be positive (source {source_rate}, output {output_rate})"
        )
    if output_rate >= source_rate:
        # Asking for a higher rate than the source has could only duplicate
        # frames, which costs bytes and shows nothing new. Keep the source rate.
        return None, source_rate

    # Exactly the gather `LoopVideo` uses to bake `speed` into an IMAGE batch:
    # stepping source_rate/output_rate frames per output frame is the same
    # operation, and the rounding is already worked out there.
    return speed_baked_indices(count, count, source_rate / output_rate), output_rate


def frame_durations(count, fps, ticks_per_second, min_ticks):
    """Per-frame durations in the container's own ticks (GIF: centiseconds, WebP: ms).

    Each duration is the difference between consecutive rounded playback times
    rather than one rounded duration repeated, so the rounding error stays
    bounded instead of accumulating: 12fps on a centisecond grid comes out as
    8,8,9,8,8,9,… and ends on the same wall clock as the source, not seconds
    early. `min_ticks` is the floor below which the format (or the viewers of
    it) would re-time the frame.
    """
    if fps <= 0:
        raise ValueError(f"Frame rate must be positive, got {fps}")

    durations = []
    elapsed = 0
    for i in range(count):
        step = max(min_ticks, round((i + 1) * ticks_per_second / fps) - elapsed)
        durations.append(step)
        elapsed += step
    return durations


def fit(source_width, source_height, width, height, mode="pad"):
    """Where a source frame lands given a `width` x `height` box.

    Returns `(scaled_w, scaled_h, canvas_w, canvas_h, offset_x, offset_y)`. The
    aspect ratio is always kept; `mode` decides what happens to the room left
    over when the box does not match it. `pad` treats the box as the canvas,
    centres the frame and leaves letterbox bars — those are what get written as
    transparent. `fit` makes the scaled frame itself the output, so the box is
    only an upper bound and nothing is padded. A zero on either axis means
    "derive it", and zero on both means "leave it alone", so the widgets default
    to doing nothing; with fewer than two axes given there is nothing to pad and
    the two modes agree.
    """
    if mode not in SIZE_MODES:
        raise ValueError(f"Unknown size mode: {mode}")
    if width <= 0 and height <= 0:
        return source_width, source_height, source_width, source_height, 0, 0
    if width <= 0:
        scaled_w = max(1, round(source_width * height / source_height))
        return scaled_w, height, scaled_w, height, 0, 0
    if height <= 0:
        scaled_h = max(1, round(source_height * width / source_width))
        return width, scaled_h, width, scaled_h, 0, 0

    scale = min(width / source_width, height / source_height)
    scaled_w = max(1, round(source_width * scale))
    scaled_h = max(1, round(source_height * scale))
    if mode == "fit":
        return scaled_w, scaled_h, scaled_w, scaled_h, 0, 0
    return (
        scaled_w,
        scaled_h,
        width,
        height,
        (width - scaled_w) // 2,
        (height - scaled_h) // 2,
    )


def to_uint8(images, size=None, resample=None, gamma=1.0):
    """A ComfyUI IMAGE batch as a uint8 [N, H, W, 3] array, optionally scaled.

    Converted a frame at a time: the whole batch as float32 is four times the
    size of the result, and a long animation is already large. Scaling in the
    same pass keeps the full-resolution copy from ever existing.

    `gamma` is applied here, on the float frame before it is rounded to 8 bits,
    so the tone curve costs no precision and every later stage — the palette,
    the dither, the encoder, the `images` output — sees the corrected frame.
    Black and white stay where they are; only the midtones move.
    """
    count, height, width = (int(images.shape[k]) for k in range(3))
    if size is not None:
        width, height = size
    if count == 0:
        raise ValueError("There are no frames to save")
    if count > MAX_FRAMES:
        raise ValueError(f"Cannot save {count} frames; the limit is {MAX_FRAMES}")
    gamma = float(gamma)
    if not MIN_GAMMA <= gamma <= MAX_GAMMA:
        raise ValueError(f"gamma must be between {MIN_GAMMA} and {MAX_GAMMA}")
    # Skipped at exactly 1.0 so the default stays bit-exact with what the node
    # wrote before the widget existed, rather than trusting pow(x, 1.0) to be.
    exponent = None if gamma == 1.0 else 1.0 / gamma

    frames = np.empty((count, height, width, 3), dtype=np.uint8)
    for i in range(count):
        frame = images[i, ..., :3]
        if torch.is_tensor(frame):
            frame = frame.detach().cpu().numpy()
        linear = np.clip(np.asarray(frame, dtype=np.float32), 0.0, 1.0)
        if exponent is not None:
            linear = np.power(linear, np.float32(exponent))
        scaled = np.clip(np.rint(linear * 255.0), 0, 255)
        if size is None:
            frames[i] = scaled
        else:
            frames[i] = np.asarray(
                Image.fromarray(scaled.astype(np.uint8)).resize(size, resample)
            )
    return frames


def prepare_frames(source, source_rate, fps, width, height, size_mode, resample, gamma):
    """The whole shared front half: decimate, fit, scale, tone-correct, round.

    Returns `(frames, frame_rate, layout)` where `frames` is uint8 [N, H, W, 3]
    at the *scaled* size and `layout` is `fit()`'s tuple, so the caller knows
    the canvas and where on it the picture goes. Frames are dropped before
    anything else looks at them: a palette should be built from the frames
    that actually get written, and every later stage costs proportionally less.
    """
    keep, frame_rate = decimate(int(source.shape[0]), source_rate, fps)
    if keep is not None:
        source = source[torch.from_numpy(keep)]

    if resample not in RESAMPLERS:
        raise ValueError(f"Unknown resample filter: {resample}")
    layout = fit(int(source.shape[2]), int(source.shape[1]), width, height, size_mode)
    scaled_w, scaled_h = layout[0], layout[1]
    resized = (scaled_w, scaled_h) != (int(source.shape[2]), int(source.shape[1]))
    frames = to_uint8(
        source,
        (scaled_w, scaled_h) if resized else None,
        RESAMPLERS[resample],
        gamma,
    )
    return frames, frame_rate, layout
