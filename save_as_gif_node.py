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


def _fit(source_width, source_height, width, height):
    """Where a source frame lands on a `width` x `height` canvas.

    Returns `(scaled_w, scaled_h, canvas_w, canvas_h, offset_x, offset_y)`. The
    aspect ratio is always kept, so asking for a canvas the source does not match
    leaves letterbox bars — those are what get written as transparent. A zero on
    either axis means "derive it", and zero on both means "leave it alone", so
    the widgets default to doing nothing.
    """
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


def _with_transparency(palette):
    """`palette` with a transparent entry prepended at index 0.

    Index 0 rather than appended, so the transparent index is the same number
    whatever each frame's palette turned out to be — `per_frame` palettes vary in
    length, and a GIF carries one transparent index for the whole file.
    """
    return np.vstack([_spare_color(palette), palette]).astype(np.uint8)


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
                        "tooltip": "Output width in pixels. 0 keeps the source width, or derives it from `height`. With both set, the frame is scaled to fit inside the canvas keeping its aspect ratio, and the leftover bars are written transparent.",
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
                        "tooltip": "How colors the palette does not hold are approximated. Error diffusion looks cleanest on photographic frames; the ordered and halftone screens are stable frame to frame, so they do not crawl on animation. halftone/halftone-square cap a cell at two colors for a printed-ink look; their -ordered variants lift that cap for smoother tone. The -poster pair is ImageMagick's -ordered-dither: it ignores the palette entirely and rounds each channel to an even RGB grid, throwing away most of the tone for a much smaller file.",
                    },
                ),
                "dither_strength": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.05,
                        "tooltip": "How much of the quantization error is dithered away. 0 disables the dither entirely; lower values trade banding back for less noise.",
                    },
                ),
                "halftone_size": (
                    "INT",
                    {
                        "default": DEFAULT_HALFTONE_SIZE,
                        "min": MIN_HALFTONE_SIZE,
                        "max": MAX_HALFTONE_SIZE,
                        "step": 1,
                        "tooltip": "Width of one halftone cell in pixels (any of the halftone dithers). For the palette-searching screens, larger dots carry less of the image and compress far better. For the -poster pair the cell also sets the number of threshold steps, so the file peaks around size 8 and only shrinks again past 16 — there, small cells give the smallest file and large cells the boldest dots.",
                    },
                ),
                "halftone_steps": (
                    "INT",
                    {
                        "default": DEFAULT_HALFTONE_STEPS,
                        "min": 0,
                        "max": MAX_HALFTONE_STEPS,
                        "step": 1,
                        "tooltip": "How many tonal steps one halftone cell resolves. 0 gives one per cell, which is what the cell size implies on its own. Setting it lower separates the dot size from the tone: big dots with coarse tone, which is the smallest a halftone gets.",
                    },
                ),
                "halftone_ink": (
                    HALFTONE_INKS,
                    {
                        "default": "black",
                        "tooltip": "Which end of the tonal range the halftone dot grows from. black: dark dots on a light ground, the way ink sits on paper. white: light dots out of a dark ground.",
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

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
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
            int(source.shape[2]), int(source.shape[1]), width, height
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
        palette_size = min(colors, MAX_COLORS - 1) if letterboxed else colors

        # The diversity choosers pick a different palette when they know the
        # result will be dithered, so they need to be told.
        dithered = dither != "none" and dither_strength > 0.0

        # A channel-independent dither dictates its own palette — it rounds each
        # channel to a grid rather than searching, so an adaptive palette would
        # describe colors it can never produce. `quantizer` and `palette_scope`
        # have nothing to decide in that case.
        grid_palette = None
        if dither in POSTER_DITHERS:
            grid_palette, _ = uniform_palette(palette_size)

        shared = None
        if grid_palette is not None or palette_scope == "global":
            base = (
                grid_palette
                if grid_palette is not None
                else build_palette(frames, palette_size, quantizer, dithered)
            )
            shared = Ditherer(
                base,
                dither,
                dither_strength,
                halftone_size,
                halftone_ink,
                halftone_steps,
            )

        shared_palette = np.zeros((0, 3), dtype=np.uint8)
        if shared is not None:
            shared_palette = (
                _with_transparency(shared.palette) if letterboxed else shared.palette
            )

        progress = ProgressBar(count) if ProgressBar is not None else None
        pages = []
        result = np.empty((count, canvas_h, canvas_w, 3), dtype=np.uint8)
        for i in range(count):
            ditherer, palette = shared, shared_palette
            if ditherer is None:
                ditherer = Ditherer(
                    build_palette(frames[i : i + 1], palette_size, quantizer, dithered),
                    dither,
                    dither_strength,
                    halftone_size,
                    halftone_ink,
                    halftone_steps,
                )
                palette = (
                    _with_transparency(ditherer.palette)
                    if letterboxed
                    else ditherer.palette
                )

            # The dither only ever sees the picture, never the bars: running it
            # over the padding would let error diffusion bleed the bar color into
            # the edge of the frame.
            indices = ditherer(frames[i])
            if letterboxed:
                padded = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
                padded[
                    offset_y : offset_y + scaled_h, offset_x : offset_x + scaled_w
                ] = indices + 1
                indices = padded

            result[i] = palette[indices]
            pages.append(_page(indices, palette))
            if progress is not None:
                progress.update(1)

        output_dir = folder_paths.get_output_directory()
        full_output_folder, filename, counter, subfolder, _ = (
            folder_paths.get_save_image_path(
                filename_prefix, output_dir, canvas_w, canvas_h
            )
        )
        file = f"{filename}_{counter:05}_.gif"
        # Handing Pillow the palette is what keeps a shared palette shared: without
        # it every frame after the first is written with its own copy of the color
        # table, which on a 256-color GIF is 768 wasted bytes per frame. Per-frame
        # palettes have to go the other way, since each frame's table really is
        # different.
        options = {}
        if shared is not None:
            options["palette"] = shared_palette.reshape(-1).tobytes()
        if letterboxed:
            options["transparency"] = 0
        pages[0].save(
            os.path.join(full_output_folder, file),
            save_all=True,
            append_images=pages[1:],
            duration=_frame_delays(count, float(frame_rate)),
            loop=loop_count,
            # Pillow's `optimize` rebuilds the palette to the colors a frame
            # actually uses, which would undo a deliberately shared global one.
            optimize=False,
            **options,
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
                "images": [{"filename": file, "subfolder": subfolder, "type": "output"}]
            },
            "result": (torch.from_numpy(result.astype(np.float32) / 255.0),),
        }


NODE_CLASS_MAPPINGS = {
    "SaveAsGif": SaveAsGif,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveAsGif": "Save as GIF",
}
