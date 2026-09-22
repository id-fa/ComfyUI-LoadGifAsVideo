"""SaveAsGif — write a VIDEO or an IMAGE batch out as an animated GIF."""

import os
from fractions import Fraction

import numpy as np
import torch
from PIL import Image

import folder_paths

from .video_length import speed_baked_indices

from .gif_dither import (
    DEFAULT_HALFTONE_SIZE,
    DEFAULT_HALFTONE_STEPS,
    DITHER_METHODS,
    HALFTONE_INKS,
    MAX_HALFTONE_SIZE,
    MASK_DITHERS,
    MASK_INK_COLORS,
    MAX_HALFTONE_STEPS,
    MIN_HALFTONE_SIZE,
    POSTER_DITHERS,
    Ditherer,
)
from .gif_palette import MAX_COLORS, QUANTIZERS, build_palette, uniform_palette

try:
    from comfy.utils import ProgressBar
except ImportError:  # ComfyUI too old, or the module imported outside ComfyUI
    ProgressBar = None

PALETTE_SCOPES = ["global", "per_frame"]

# What `width` x `height` means when the source does not share its aspect ratio.
# `pad` is the original behavior and stays the default: the box is the canvas
# and the leftover is transparent bars. `fit` scales the frame into the box and
# writes it at that size, so the box is an upper bound rather than the output.
SIZE_MODES = ["pad", "fit"]

# Ordered best-quality-first for a downscale, which is what saving a GIF usually
# is. `nearest` is last because it is the specialist: it is the only one that
# keeps pixel art and hard-edged animation from turning to mush.
RESAMPLERS = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "hamming": Image.Resampling.HAMMING,
    "box": Image.Resampling.BOX,
    "nearest": Image.Resampling.NEAREST,
}

# Canvas limit. Well past anything a GIF should be, and it only exists so a typo
# cannot ask for an allocation that takes the server down.
MAX_DIMENSION = 8192

# GIF stores frame delays in centiseconds. Every browser re-times a delay under
# 2cs to 10cs, so writing one would make a fast animation play ~5x too slow —
# the same substitution `LoadGifAsVideo` compensates for on the way in. Clamping
# here caps the output at 50fps, which is the real ceiling of the format.
_MIN_DELAY_CS = 2

# Matches the cap the two loader nodes apply, so a batch that could be built here
# can always be saved.
_MAX_FRAMES = 10000


def _decimate(count, source_rate, output_rate):
    """Indices that resample `count` frames down to `output_rate`, and the rate used.

    Dropping frames is the cheapest thing that shrinks a GIF: half the frames is
    close to half the file. The playback time is unchanged, because the delays are
    then written at `output_rate` — 60 frames of 30fps material at 10fps becomes
    20 frames that still run for two seconds.

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


def _frame_delays(count, fps):
    """Per-frame delays in milliseconds, laid out on GIF's centisecond grid.

    Each delay is the difference between consecutive rounded playback times
    rather than one rounded delay repeated, so the rounding error stays bounded
    instead of accumulating: 12fps comes out as 8,8,9,8,8,9,… centiseconds and
    ends on the same wall clock as the source, not seconds early.
    """
    if fps <= 0:
        raise ValueError(f"Frame rate must be positive, got {fps}")

    delays = []
    elapsed = 0
    for i in range(count):
        step = max(_MIN_DELAY_CS, round((i + 1) * 100.0 / fps) - elapsed)
        delays.append(step * 10)
        elapsed += step
    return delays


def _fit(source_width, source_height, width, height, mode="pad"):
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


def _spare_color(palette):
    """An RGB value `palette` does not already hold, for the transparent slot.

    It has to be unused: Pillow maps a frame onto the palette through a
    color-to-index dict, so a repeated entry would send the mapping astray. Black
    first, because that is what a viewer ignoring transparency will most likely
    already be compositing against; magenta as the conventional fallback. A
    palette of at most 255 colors cannot cover a 16-step lattice of 4096, so the
    scan always finds something.
    """
    used = {tuple(int(v) for v in entry) for entry in palette}
    for candidate in ((0, 0, 0), (255, 0, 255)):
        if candidate not in used:
            return candidate
    for red in range(0, 256, 17):
        for green in range(0, 256, 17):
            for blue in range(0, 256, 17):
                if (red, green, blue) not in used:
                    return (red, green, blue)
    raise RuntimeError("Palette leaves no color free for transparency")


def _with_ink(palette, ink):
    """`palette` guaranteed to hold the mask screen's ink color exactly.

    A mask screen paints its dot one flat color, and `halftone_ink` names which:
    real black or real white, not "whatever the adaptive palette had nearest".
    On a photographic palette the nearest entry to black is often a dark blue,
    which would make the screen read as a tint rather than as ink.
    """
    target = np.array(MASK_INK_COLORS[ink], dtype=np.uint8)
    if (palette == target).all(1).any():
        return palette
    return np.vstack([palette, target]).astype(np.uint8)


def _with_transparency(palette, spare=None):
    """`palette` with a transparent entry prepended at index 0.

    Index 0 rather than appended, so the transparent index is the same number
    whatever each frame's palette turned out to be — `per_frame` palettes vary in
    length, and a GIF carries one transparent index for the whole file. `spare`
    is the color to put there when the caller has one that every frame can share.
    """
    if spare is None:
        spare = _spare_color(palette)
    return np.vstack([spare, palette]).astype(np.uint8)


def _to_uint8(images, size=None, resample=None):
    """A ComfyUI IMAGE batch as a uint8 [N, H, W, 3] array, optionally scaled.

    Converted a frame at a time: the whole batch as float32 is four times the
    size of the result, and a long animation is already large. Scaling in the
    same pass keeps the full-resolution copy from ever existing.
    """
    count, height, width = (int(images.shape[k]) for k in range(3))
    if size is not None:
        width, height = size
    if count == 0:
        raise ValueError("There are no frames to save")
    if count > _MAX_FRAMES:
        raise ValueError(f"Cannot save {count} frames; the limit is {_MAX_FRAMES}")

    frames = np.empty((count, height, width, 3), dtype=np.uint8)
    for i in range(count):
        frame = images[i, ..., :3]
        if torch.is_tensor(frame):
            frame = frame.detach().cpu().numpy()
        scaled = np.clip(np.rint(np.asarray(frame, dtype=np.float32) * 255.0), 0, 255)
        if size is None:
            frames[i] = scaled
        else:
            frames[i] = np.asarray(
                Image.fromarray(scaled.astype(np.uint8)).resize(size, resample)
            )
    return frames


def _page(indices, palette):
    """One GIF frame as a P-mode image carrying exactly `palette`.

    The palette is written at its true length rather than padded to 256, so a
    16-color GIF gets a 16-entry color table instead of paying 768 bytes per
    table for 720 bytes of black.
    """
    page = Image.fromarray(indices, mode="P")
    page.putpalette(palette.reshape(-1).tobytes())
    return page


class SaveAsGif:
    """Quantize and dither a video or image batch and write it as an animated GIF."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "ComfyUI",
                        "tooltip": "Prefix for the file written into the ComfyUI output directory.",
                    },
                ),
                "source_fps": (
                    "FLOAT",
                    {
                        "default": 12.0,
                        "min": 0.1,
                        "max": 240.0,
                        "step": 0.1,
                        "tooltip": "The rate an `images` batch is meant to play at. Ignored when `video` is connected — a VIDEO carries its own rate. This is only the description of the input; `fps` is what gets written.",
                    },
                ),
                "fps": (
                    "FLOAT",
                    {
                        "default": 12.0,
                        "min": 0.1,
                        "max": 50.0,
                        "step": 0.1,
                        "tooltip": "Frame rate of the GIF. Set it below the source rate to drop frames: the animation runs for the same length of time on fewer frames, which is the cheapest way to shrink the file. Raising it above the source rate does nothing. GIF cannot store a delay shorter than 1/50s.",
                    },
                ),
                "width": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": MAX_DIMENSION,
                        "step": 8,
                        "tooltip": "Output width in pixels. 0 keeps the source width, or derives it from `height`. With both set, the frame is scaled to fit inside the box keeping its aspect ratio; `size_mode` decides what happens to the room left over.",
                    },
                ),
                "height": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": MAX_DIMENSION,
                        "step": 8,
                        "tooltip": "Output height in pixels. 0 keeps the source height, or derives it from `width`.",
                    },
                ),
                "size_mode": (
                    SIZE_MODES,
                    {
                        "default": "pad",
                        "tooltip": "What `width` x `height` means when the source has a different aspect ratio. pad: the box is the canvas — the frame is centred and the leftover bars are written transparent, so the GIF is exactly the requested size. fit: the frame is scaled to the largest size that fits inside the box and written at that size, with no bars. Same thing whenever only one of width/height is set.",
                    },
                ),
                "resample": (
                    list(RESAMPLERS),
                    {
                        "default": "lanczos",
                        "tooltip": "Filter used when scaling. lanczos is the sharpest downscale; box and hamming are softer and quieter to dither; nearest keeps pixel art and hard-edged animation crisp instead of blurring it.",
                    },
                ),
                "colors": (
                    "INT",
                    {
                        "default": 256,
                        "min": 2,
                        "max": MAX_COLORS,
                        "step": 1,
                        "tooltip": "Palette size. GIF allows at most 256 colors; fewer makes a smaller file and a stronger dither pattern. The -poster dithers round this down to a cube (8, 27, 64, 125, 216) because they quantize each channel on its own.",
                    },
                ),
                "palette_scope": (
                    PALETTE_SCOPES,
                    {
                        "default": "global",
                        "tooltip": "global: one palette for the whole animation — no color flicker, smaller file. per_frame: a fresh palette per frame — better color on animations whose content changes a lot. Ignored by the -poster dithers, whose palette does not depend on the frames at all.",
                    },
                ),
                "quantizer": (
                    QUANTIZERS,
                    {
                        "default": "median_cut_aforge",
                        "tooltip": "How the palette is chosen. median_cut_* split the color cube; diversity picks popular and far-apart colors in turn, which holds onto small bright accents that median cut averages away. Ignored by the -poster dithers, which dictate their own even RGB grid.",
                    },
                ),
                "dither": (
                    DITHER_METHODS,
                    {
                        "default": "floyd-steinberg",
                        "tooltip": "How colors the palette does not hold are approximated. Error diffusion looks cleanest on photographic frames; the ordered and halftone screens are stable frame to frame, so they do not crawl on animation. halftone/halftone-square/halftone-diamond/halftone-brick cap a cell at two colors for a printed-ink look; their -ordered variants lift that cap for smoother tone. The -mask variants lay a single flat ink dot (the halftone_ink color) over the picture and leave the rest of the frame alone; their lattice is the same in every frame, so it never crawls. The -poster variants are ImageMagick's -ordered-dither: they ignore the palette entirely and round each channel to an even RGB grid, throwing away most of the tone for a much smaller file. Lattices: halftone puts the dots on a hexagonal grid whose rows run horizontally, -square on an upright square grid, -diamond on a square grid turned 45 degrees (the classic newsprint mesh, whose rows never line up with the scan lines), -brick in horizontal rows a full pitch apart with every other row shifted by half, so each dot sits under the gap above it.",
                    },
                ),
                "dither_strength": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.05,
                        "tooltip": "How much of the quantization error is dithered away. 0 disables the dither entirely; lower values trade banding back for less noise. For the -mask dithers this is the screen's coverage instead, reaching half at 1.0: the hex/square/diamond masks shrink the dot inside its cell, the brick mask keeps the dot's size and spreads the dots apart (pitch = halftone_size / sqrt(strength)).",
                    },
                ),
                "halftone_size": (
                    "INT",
                    {
                        "default": DEFAULT_HALFTONE_SIZE,
                        "min": MIN_HALFTONE_SIZE,
                        "max": MAX_HALFTONE_SIZE,
                        "step": 1,
                        "tooltip": "Distance between neighbouring halftone dots in pixels (any of the halftone dithers). For the palette-searching screens, larger dots carry less of the image and compress far better. For the -poster pair the cell also sets the number of threshold steps, so the file peaks around size 8 and only shrinks again past 16 — there, small cells give the smallest file and large cells the boldest dots.",
                    },
                ),
                "halftone_steps": (
                    "INT",
                    {
                        "default": DEFAULT_HALFTONE_STEPS,
                        "min": 0,
                        "max": MAX_HALFTONE_STEPS,
                        "step": 1,
                        "tooltip": "How many tonal steps one halftone cell resolves. 0 gives one per cell, which is what the cell size implies on its own. Setting it lower separates the dot size from the tone: big dots with coarse tone, which is the smallest a halftone gets. Ignored by the -mask dithers, whose dot is one fixed size.",
                    },
                ),
                "halftone_ink": (
                    HALFTONE_INKS,
                    {
                        "default": "black",
                        "tooltip": "Which end of the tonal range the halftone dot grows from. black: dark dots on a light ground, the way ink sits on paper. white: light dots out of a dark ground. For the -mask dithers this names the ink itself — real black or real white — and it is reserved in the palette.",
                    },
                ),
                "loop_count": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "step": 1,
                        "tooltip": "How many extra times the GIF replays. 0 loops forever.",
                    },
                ),
                "frame_diff": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Write only the pixels that changed since the previous frame; the rest are marked transparent so the viewer keeps what is already there. Costs one palette entry (255 colors instead of 256). Shrinks the file a lot when much of the frame holds still and the dither is stable (the ordered and halftone screens); error diffusion changes almost every pixel every frame, so it gains little there. Off writes every frame in full, cropped to the changed rectangle.",
                    },
                ),
                "pillow_optimize": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Pass optimize=True to Pillow's GIF writer. It trims each frame's color table to the colors that frame actually uses and marks unchanged pixels transparent. Note that it rebuilds the palette per frame, so palette_scope=global no longer gives one shared table and the file may grow instead of shrink. Try it and read the size in `info`.",
                    },
                ),
            },
            "optional": {
                "video": (
                    "VIDEO",
                    {"tooltip": "The video to save. Its frame rate is used as-is."},
                ),
                "images": (
                    "IMAGE",
                    {
                        "tooltip": "Frames to save, timed by the `fps` widget. Connect this or `video`, not both."
                    },
                ),
            },
        }

    # `info` is appended, never inserted — existing workflows keep their links.
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "load-gif-as-video"
    DESCRIPTION = (
        "Saves a VIDEO or an image batch as an animated GIF, with a choice of "
        "color quantizers and dithering filters ported from AForge.NET's color "
        "reduction filters and gifsicle's dither methods. The dithered frames "
        "come back out as an IMAGE batch so the result can be inspected without "
        "opening the file."
    )

    def save(
        self,
        filename_prefix,
        source_fps,
        fps,
        width,
        height,
        resample,
        colors,
        palette_scope,
        quantizer,
        dither,
        dither_strength,
        halftone_size,
        halftone_steps,
        halftone_ink,
        loop_count,
        video=None,
        images=None,
        # Keyword with a default so a queued prompt from before the widget
        # existed still runs, and runs the way it used to.
        size_mode="pad",
        frame_diff=False,
        pillow_optimize=False,
    ):
        if video is not None and images is not None:
            raise ValueError(
                "Connect either `video` or `images`, not both — they would have "
                "different frame rates"
            )
        if video is not None:
            components = video.get_components()
            source, source_rate = components.images, components.frame_rate
        elif images is not None:
            source, source_rate = images, source_fps
        else:
            raise ValueError("Connect a `video` or an `images` batch to save")

        # Drop frames before anything else looks at them: the palette should be
        # built from the frames that actually get written, and everything
        # downstream then costs proportionally less.
        keep, frame_rate = _decimate(int(source.shape[0]), source_rate, fps)
        if keep is not None:
            source = source[torch.from_numpy(keep)]

        if resample not in RESAMPLERS:
            raise ValueError(f"Unknown resample filter: {resample}")
        scaled_w, scaled_h, canvas_w, canvas_h, offset_x, offset_y = _fit(
            int(source.shape[2]), int(source.shape[1]), width, height, size_mode
        )
        resized = (scaled_w, scaled_h) != (int(source.shape[2]), int(source.shape[1]))
        frames = _to_uint8(
            source,
            (scaled_w, scaled_h) if resized else None,
            RESAMPLERS[resample],
        )
        count = frames.shape[0]

        # Bars are only needed when the source does not share the canvas's aspect
        # ratio. They are written as GIF transparency, which costs one palette
        # entry — so the adaptive palette gives up a color to make room.
        letterboxed = (scaled_w, scaled_h) != (canvas_w, canvas_h)
        # Frame differencing marks unchanged pixels with the same transparent
        # entry, so either use of transparency reserves the slot.
        transparent = letterboxed or bool(frame_diff)
        # The slot comes out of `colors`, not on top of it: a 64-color request
        # with a 65-entry table would be written as a 128-entry one.
        palette_size = max(2, colors - 1) if transparent else colors

        # The diversity choosers pick a different palette when they know the
        # result will be dithered, so they need to be told.
        dithered = dither != "none" and dither_strength > 0.0

        # A channel-independent dither dictates its own palette — it rounds each
        # channel to a grid rather than searching, so an adaptive palette would
        # describe colors it can never produce. `quantizer` and `palette_scope`
        # have nothing to decide in that case.
        grid_palette = None
        if dither in POSTER_DITHERS:
            # The grid keeps the full request even with a transparent slot: a
            # cube plus one entry (217) rounds up to the same 256-entry table a
            # cube alone does, while asking for 215 would drop it from 6^3 to 5^3.
            grid_palette, _ = uniform_palette(min(colors, MAX_COLORS - 1))

        # A mask screen paints its dot in one fixed ink, so that color has to be
        # an entry; give up a slot for it rather than settle for the nearest.
        masked = dither in MASK_DITHERS
        if masked:
            palette_size = max(2, palette_size - 1)

        def palette_for(source):
            if grid_palette is not None:
                return grid_palette
            palette = build_palette(source, palette_size, quantizer, dithered)
            return _with_ink(palette, halftone_ink) if masked else palette

        shared = None
        if grid_palette is not None or palette_scope == "global":
            shared = Ditherer(
                palette_for(frames),
                dither,
                dither_strength,
                halftone_size,
                halftone_ink,
                halftone_steps,
            )

        shared_palette = np.zeros((0, 3), dtype=np.uint8)
        if shared is not None:
            shared_palette = (
                _with_transparency(shared.palette) if transparent else shared.palette
            )

        # Per-frame palettes with a transparent slot are built up front so the
        # slot can hold one color in every frame. Pillow decides how much of a
        # frame to write by comparing it with the previous one *as colors*, so
        # a slot whose color changed between frames would count as a change
        # everywhere it appears — every bar, every unchanged pixel.
        ditherers = None
        spare = None
        if shared is None and transparent:
            ditherers = [
                Ditherer(
                    palette_for(frames[i : i + 1]),
                    dither,
                    dither_strength,
                    halftone_size,
                    halftone_ink,
                    halftone_steps,
                )
                for i in range(count)
            ]
            try:
                spare = _spare_color(np.vstack([d.palette for d in ditherers]))
            except RuntimeError:
                # Thousands of palettes can between them cover the lattice the
                # scan walks. Each frame then picks its own; the file is still
                # correct, only cropped less tightly.
                spare = None

        progress = ProgressBar(count) if ProgressBar is not None else None
        delays = _frame_delays(count, float(frame_rate))
        pages = []
        durations = []
        previous_page = None
        colors_written = 0
        result = np.empty((count, canvas_h, canvas_w, 3), dtype=np.uint8)
        for i in range(count):
            ditherer, palette = shared, shared_palette
            if ditherer is None:
                if ditherers is not None:
                    ditherer = ditherers[i]
                else:
                    ditherer = Ditherer(
                        palette_for(frames[i : i + 1]),
                        dither,
                        dither_strength,
                        halftone_size,
                        halftone_ink,
                        halftone_steps,
                    )
                palette = (
                    _with_transparency(ditherer.palette, spare)
                    if transparent
                    else ditherer.palette
                )

            # The dither only ever sees the picture, never the bars: running it
            # over the padding would let error diffusion bleed the bar color into
            # the edge of the frame.
            indices = ditherer(frames[i])
            if transparent:
                # Index 0 is the transparent slot, so the dither's indices move up
                # one. The dither's palette holds at most 255 entries here, so
                # this cannot wrap.
                indices = indices + 1
            if letterboxed:
                padded = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
                padded[
                    offset_y : offset_y + scaled_h, offset_x : offset_x + scaled_w
                ] = indices
                indices = padded

            result[i] = palette[indices]
            if frame_diff and previous_page is not None:
                # Compared as colors, not indices: under `per_frame` each frame
                # has its own palette, so equal indices mean nothing. A pixel
                # that kept its color is written as transparent and the viewer
                # leaves the previous frame's pixel in place (disposal 1). A
                # frame that changed nothing at all is folded into the previous
                # frame's delay rather than written.
                same = np.all(result[i] == result[i - 1], axis=-1)
                if same.all():
                    durations[-1] += delays[i]
                    if progress is not None:
                        progress.update(1)
                    continue
                diffed = np.where(same, 0, indices).astype(np.uint8)
                if shared is not None:
                    # Pillow crops each frame to where it differs from the frame
                    # it was handed before, not from what the viewer shows.
                    # Handing it the diffed frame outright would make that
                    # "where either frame has a change", or the whole canvas
                    # right after the opaque first frame. So outside the changed
                    # pixels' bounding box the page repeats the previous page,
                    # which Pillow then crops away; inside it, unchanged pixels
                    # go transparent. What reaches the file is the tight box,
                    # and the viewer's canvas is still the true frame.
                    rows = np.flatnonzero(~same.all(axis=1))
                    cols = np.flatnonzero(~same.all(axis=0))
                    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
                    page = previous_page.copy()
                    page[y0:y1, x0:x1] = diffed[y0:y1, x0:x1]
                    indices = page
                else:
                    # Under `per_frame` that trick fails: Pillow compares frames
                    # with different palettes as colors, so repeating the
                    # previous page's indices reads as a change wherever the
                    # palette moved. The plain diff is what stays comparable —
                    # transparent against transparent, with the shared spare
                    # color — and Pillow crops it to where this frame's changes
                    # and the last frame's changes together reach.
                    indices = diffed
            previous_page = indices
            pages.append(_page(indices, palette))
            durations.append(delays[i])
            # Reported in `info`. Under `per_frame` this ends up being the last
            # frame's count, which is the honest answer — there is no single one.
            colors_written = len(palette)
            if progress is not None:
                progress.update(1)

        output_dir = folder_paths.get_output_directory()
        full_output_folder, filename, counter, subfolder, _ = (
            folder_paths.get_save_image_path(
                filename_prefix, output_dir, canvas_w, canvas_h
            )
        )
        file = f"{filename}_{counter:05}_.gif"
        path = os.path.join(full_output_folder, file)
        # Handing Pillow the palette is what keeps a shared palette shared: without
        # it every frame after the first is written with its own copy of the color
        # table, which on a 256-color GIF is 768 wasted bytes per frame. Per-frame
        # palettes have to go the other way, since each frame's table really is
        # different.
        options = {}
        if shared is not None:
            options["palette"] = shared_palette.reshape(-1).tobytes()
        if transparent:
            options["transparency"] = 0
        if frame_diff:
            # "Leave in place": the next frame paints over this one, which is
            # what makes its transparent pixels mean "unchanged". The default 0
            # is read the same way by every viewer in practice, but 1 says so.
            options["disposal"] = 1
        pages[0].save(
            path,
            save_all=True,
            append_images=pages[1:],
            duration=durations,
            loop=loop_count,
            # Pillow's `optimize` rebuilds the palette to the colors a frame
            # actually uses, which undoes a deliberately shared global one — so
            # it is off unless asked for. When on, Pillow also does its own
            # unchanged-pixels-to-transparent pass, which agrees with ours.
            optimize=bool(pillow_optimize),
            **options,
        )

        written = os.path.getsize(path)
        # The file size is the number people actually tune for, so report it back
        # rather than making them go and look. `PreviewAny` ("Preview as Text")
        # renders this straight; `ui.text` matches the shape that node emits, in
        # case the frontend ever generalizes its text preview beyond it.
        info = "\n".join(
            [
                file,
                f"{written / 1024:,.1f} KB ({written:,} bytes)",
                f"{count} frames · {canvas_w}×{canvas_h} · "
                f"{float(frame_rate):.4g} fps · {sum(delays) / 1000.0:.2f} s",
                f"{colors_written} colors · {dither}"
                + (f" · {len(pages)} frames written, diffed" if frame_diff else "")
                + (" · pillow optimize" if pillow_optimize else ""),
            ]
        )

        return {
            # No `animated` flag, even though this *is* an animation. The stock
            # SaveAnimatedWEBP sets it, but the frontend reads it as "this is a
            # video unless it is .webp or .png":
            #
            #   isVideoOutput(o) = isAnimatedOutput(o)
            #                      && !images.some(f => f.endsWith(".webp"))
            #                      && !images.some(f => f.endsWith(".png"))
            #
            # A .gif is on neither list, so the flag sends it to the video player,
            # which fails to parse the path and shows "Invalid URL" instead of the
            # image. Without the flag it takes the image path and animates on its
            # own, because that is what an <img> does with a GIF.
            "ui": {
                "images": [
                    {"filename": file, "subfolder": subfolder, "type": "output"}
                ],
                "text": (info,),
            },
            "result": (torch.from_numpy(result.astype(np.float32) / 255.0), info),
        }


NODE_CLASS_MAPPINGS = {
    "SaveAsGif": SaveAsGif,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveAsGif": "Save as GIF",
}
