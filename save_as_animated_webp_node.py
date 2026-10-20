"""SaveAsAnimatedWebP — write a VIDEO or an IMAGE batch out as an animated WebP.

The same front half as `SaveAsGif` (`save_frames`): drop frames to `fps`, scale
into the box, tone-correct. What follows is the opposite of the GIF path —
there is no palette and no dither, because WebP stores full 24-bit color and
libwebp does the compressing. So the node is the GIF node with the color block
swapped for libwebp's three knobs: `lossless`, `quality`, `method`.
"""

import io
import os

import numpy as np
import torch
from PIL import Image

import folder_paths

from .progress import Progress
from .save_frames import (
    MAX_DIMENSION,
    MAX_GAMMA,
    MIN_GAMMA,
    RESAMPLERS,
    SIZE_MODES,
    decimate,
    frame_durations,
    prepare_frames,
    resolve_source,
)

# WebP stores frame durations in milliseconds, so there is no centisecond grid
# to land on. The floor is one past the 10 ms at which browsers re-time a GIF
# frame to 100 ms; whether every WebP decoder applies the same rule is not
# documented, and staying clear of it costs nothing. The `fps` widget's cap
# (60) keeps every real duration well above it, so the floor is only a guard.
_MIN_DURATION_MS = 11
MAX_FPS = 60.0

# libwebp's `method`: 0 is fastest, 6 is slowest and smallest. Pillow's own
# default is 4, which is also what the stock SaveAnimatedWEBP calls "default".
MIN_METHOD = 0
MAX_METHOD = 6
DEFAULT_METHOD = 4


class _Page(Image.Image):
    """A page of the animation that reports when the encoder reaches it.

    Pillow hands the whole animation to libwebp's `WebPAnimEncoder` inside one
    `save(save_all=True)` call, and that call offers no per-frame hook — which
    is why Cancel used to be ignored until the file was finished. But its
    writer (`WebPImagePlugin._save_all`) does call `seek(idx)` on every image in
    `append_images` immediately before encoding it, in every Pillow release
    that can write animated WebP. A page that overrides `seek` therefore gets a
    word in between frames, and that word is the progress bar's interrupt check.
    The private `_webp` encoder could be driven directly instead, but its
    constructor and `add` changed shape between Pillow 11 and 12; `seek` did not.
    """

    def __init__(self, image, on_seek):
        super().__init__()
        self.__dict__.update(image.__dict__)
        self._on_seek = on_seek

    def seek(self, frame):
        self._on_seek()
        super().seek(frame)


def _rgba_pages(frames, layout, on_seek):
    """The frames as Pillow images on the canvas, with transparent bars if padded.

    Under `fit`, or with no box at all, the canvas is the frame and the pages
    are plain RGB, so libwebp sees no transparency and writes no alpha plane.
    Under `pad` the picture is pasted onto a fully transparent canvas, so the
    bars come out as real alpha-0 pixels rather than a solid color. (Pillow's
    reader reports every animated WebP as RGBA whichever way it was written.)
    `on_seek` is called as the encoder reaches each page; see `_Page`.
    """
    scaled_w, scaled_h, canvas_w, canvas_h, offset_x, offset_y = layout
    letterboxed = (scaled_w, scaled_h) != (canvas_w, canvas_h)
    pages = []
    for frame in frames:
        page = Image.fromarray(frame, mode="RGB")
        if letterboxed:
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            canvas.paste(page, (offset_x, offset_y))
            page = canvas
        pages.append(_Page(page, on_seek))
    return pages, letterboxed


def _encode(pages, durations, loop_count, lossless, quality, method):
    """The animation as WebP bytes.

    Encoded into memory rather than straight to the file so that a cancel in
    the middle (raised out of `_Page.seek`) leaves nothing behind on disk — not
    even an empty file with the counter's name.
    """
    buffer = io.BytesIO()
    pages[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=pages[1:],
        duration=durations,
        loop=int(loop_count),
        lossless=bool(lossless),
        quality=int(quality),
        method=int(method),
    )
    return buffer.getvalue()


def _read_back(path, count, canvas_w, canvas_h, progress=None):
    """The written file decoded to a uint8 [N, H, W, 3] batch, alpha composited on black.

    This is what the `images` output shows: under `lossless` it is the input
    frames bit for bit, under lossy it is what the file actually looks like,
    which is the thing worth comparing `quality` settings on. Transparent bars
    come back as black, the way they do from the GIF node.
    """
    result = np.empty((count, canvas_h, canvas_w, 3), dtype=np.uint8)
    with Image.open(path) as image:
        held = getattr(image, "n_frames", 1)
        if held != count:
            raise RuntimeError(f"Wrote {count} frames but the file holds {held}")
        for i in range(count):
            image.seek(i)
            page = np.asarray(image.convert("RGBA"), dtype=np.uint16)
            alpha = page[..., 3:4]
            result[i] = ((page[..., :3] * alpha + 127) // 255).astype(np.uint8)
            if progress is not None:
                progress(i + 1, count)
    return result


class SaveAsAnimatedWebP:
    """Write a video or image batch as an animated WebP, lossless or lossy."""

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
                        "max": MAX_FPS,
                        "step": 0.1,
                        "tooltip": "Frame rate of the WebP. Set it below the source rate to drop frames: the animation runs for the same length of time on fewer frames, which is the cheapest way to shrink the file. Raising it above the source rate does nothing.",
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
                        "tooltip": "What `width` x `height` means when the source has a different aspect ratio. pad: the box is the canvas — the frame is centred and the leftover bars are written transparent, so the WebP is exactly the requested size. fit: the frame is scaled to the largest size that fits inside the box and written at that size, with no bars. Same thing whenever only one of width/height is set.",
                    },
                ),
                "resample": (
                    list(RESAMPLERS),
                    {
                        "default": "lanczos",
                        "tooltip": "Filter used when scaling. lanczos is the sharpest downscale; box and hamming are softer and compress a little better; nearest keeps pixel art and hard-edged animation crisp instead of blurring it.",
                    },
                ),
                "lossless": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Lossless WebP stores every pixel exactly, so the file decodes back to the `images` output bit for bit; `quality` then only trades encode time for file size. Off writes lossy WebP (VP8), where `quality` is the usual quality/size trade and is much smaller on photographic material.",
                    },
                ),
                "quality": (
                    "INT",
                    {
                        "default": 80,
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "tooltip": "Lossy: picture quality, 0 (smallest, ugliest) to 100 (largest, nearly lossless). Lossless: compression effort, 0 (fastest) to 100 (smallest) — the pixels are exact either way.",
                    },
                ),
                "method": (
                    "INT",
                    {
                        "default": DEFAULT_METHOD,
                        "min": MIN_METHOD,
                        "max": MAX_METHOD,
                        "step": 1,
                        "tooltip": "How hard libwebp tries, 0 (fastest) to 6 (slowest, smallest). Never changes the picture at a given quality, only the time and the size; 6 can take several times as long as 4 for a few percent off the file.",
                    },
                ),
                "loop_count": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000,
                        "step": 1,
                        "tooltip": "How many times the animation plays. 0 loops forever.",
                    },
                ),
                "gamma": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": MIN_GAMMA,
                        "max": MAX_GAMMA,
                        "step": 0.05,
                        "tooltip": "Tone correction applied to the frames before they are encoded. 1.0 leaves them alone; above it brightens the midtones, below it darkens them, and black and white stay put. The `images` output shows the corrected result.",
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
                        "tooltip": "Frames to save, timed by the `source_fps` widget. Connect this or `video`, not both."
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "load-gif-as-video"
    DESCRIPTION = (
        "Saves a VIDEO or an image batch as an animated WebP, lossless or lossy, "
        "with the same frame-dropping, scaling and tone controls as Save as GIF. "
        "The frames as written come back out as an IMAGE batch so the result "
        "can be inspected without opening the file."
    )

    def save(
        self,
        filename_prefix,
        source_fps,
        fps,
        width,
        height,
        size_mode,
        resample,
        lossless,
        quality,
        method,
        loop_count,
        gamma,
        video=None,
        images=None,
    ):
        if not MIN_METHOD <= int(method) <= MAX_METHOD:
            raise ValueError(f"method must be between {MIN_METHOD} and {MAX_METHOD}")
        if not 0 <= int(quality) <= 100:
            raise ValueError("quality must be between 0 and 100")

        source, source_rate = resolve_source(video, images, source_fps)
        # Three passes over the frames — convert, encode, read back — on one
        # bar, each of which is also where a cancel takes effect. The count is
        # only known after decimation, so the bar is sized from a dry run of it.
        kept, _ = decimate(int(source.shape[0]), source_rate, fps)
        count = int(source.shape[0]) if kept is None else int(kept.shape[0])
        progress = Progress(3 * count)
        frames, frame_rate, layout = prepare_frames(
            source,
            source_rate,
            fps,
            width,
            height,
            size_mode,
            resample,
            gamma,
            progress.stage(count),
        )
        count = frames.shape[0]
        canvas_w, canvas_h = layout[2], layout[3]

        encoded = progress.stage(count)
        reached = [0]

        def on_seek():
            reached[0] += 1
            encoded(reached[0] - 1, count)

        pages, letterboxed = _rgba_pages(frames, layout, on_seek)
        durations = frame_durations(count, float(frame_rate), 1000, _MIN_DURATION_MS)
        data = _encode(pages, durations, loop_count, lossless, quality, method)
        encoded(count, count)

        output_dir = folder_paths.get_output_directory()
        full_output_folder, filename, counter, subfolder, _ = (
            folder_paths.get_save_image_path(
                filename_prefix, output_dir, canvas_w, canvas_h
            )
        )
        file = f"{filename}_{counter:05}_.webp"
        path = os.path.join(full_output_folder, file)
        with open(path, "wb") as handle:
            handle.write(data)

        written = len(data)
        # What the viewer will see, not what the encoder was handed: under lossy
        # the two differ, and the difference is the thing `quality` is tuned on.
        result = _read_back(path, count, canvas_w, canvas_h, progress.stage(count))

        info = "\n".join(
            [
                file,
                f"{written / 1024:,.1f} KB ({written:,} bytes)",
                f"{count} frames · {canvas_w}×{canvas_h} · "
                f"{float(frame_rate):.4g} fps · {sum(durations) / 1000.0:.2f} s",
                ("lossless" if lossless else "lossy")
                + f" · quality {int(quality)} · method {int(method)}"
                + (" · transparent bars" if letterboxed else ""),
            ]
        )

        return {
            # `animated` is set here, unlike in SaveAsGif, because the frontend's
            # video check exempts `.webp` by name (isVideoOutput: animated and
            # not .webp and not .png), so the flag reaches the image path and
            # the stock SaveAnimatedWEBP sets it the same way. The VIDEO input
            # slot would still route the node to the video player on its own;
            # web/save_as_gif_preview.js pins `previewMediaType` for that.
            "ui": {
                "images": [
                    {"filename": file, "subfolder": subfolder, "type": "output"}
                ],
                "animated": (True,),
                "text": (info,),
            },
            "result": (torch.from_numpy(result.astype(np.float32) / 255.0), info),
        }


NODE_CLASS_MAPPINGS = {
    "SaveAsAnimatedWebP": SaveAsAnimatedWebP,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveAsAnimatedWebP": "Save as Animated WebP",
}
