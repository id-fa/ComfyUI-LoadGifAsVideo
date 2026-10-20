"""LoadVideoTrim — load a frame range of a video, cropped, with a loop-point finder in the UI.

The node itself is small: pick a file, a `[start_frame, end_frame)` range and a
crop box, and emit the frames as a VIDEO (with the audio of the same range) and
an IMAGE batch. The interesting part is the UI in `web/load_video_trim.js`,
which scrubs the file, draws the crop box, and finds frames that look like the
one under the playhead so a seamless loop can be cut. That search runs over
HTTP routes registered here, so it works without queueing the graph.
"""

import asyncio
import io
import math
import os
from collections import OrderedDict
from fractions import Fraction

from PIL import Image

import folder_paths

from . import video_source
from .progress import Progress
from .video_length import MAX_OUTPUT_FRAMES, speed_baked_indices

try:
    from comfy_api.input_impl import VideoFromComponents
    from comfy_api.util import VideoComponents

    _VIDEO_IMPORT_ERROR = None
except ImportError as exc:  # ComfyUI too old to have the VIDEO type
    VideoFromComponents = None
    VideoComponents = None
    _VIDEO_IMPORT_ERROR = exc

_MAX_FRAME_INDEX = 10_000_000
_MAX_CROP = 16384

# `speed` is a slider: 4 either way at 0.05 steps is 160 positions, which the
# widget can place by eye, and anything faster is `Loop Video`'s job (it goes
# to 100). The sign is the direction.
MAX_SPEED = 4.0


def _list_video_files():
    input_dir = folder_paths.get_input_directory()
    try:
        names = [
            f
            for f in os.listdir(input_dir)
            if os.path.isfile(os.path.join(input_dir, f))
        ]
    except OSError:
        return []
    try:
        files = set(folder_paths.filter_files_content_types(names, ["video"]))
    except Exception:
        files = set()
    # The MIME table does not know every container; the extension list catches
    # the rest (.mkv on a bare Windows install, for one).
    files.update(f for f in names if video_source.is_video_file(f))
    return sorted(files)


def speed_order(count, speed):
    """Row order that bakes `speed` into a range of `count` frames, or None at 1.0.

    The frame rate the node reports never changes, so speed has to be expressed
    as frames: `speed_baked_indices` steps `|speed|` source frames per output
    frame over `round(count / |speed|)` frames — 2 keeps every other frame and
    halves the running time, 0.5 repeats each frame and doubles it — and a
    negative speed walks that same sequence from the end of the range back to
    the start. 0 is refused rather than read as "still", and 1.0 returns None
    so the caller can skip the gather and keep the audio.
    """
    speed = float(speed)
    if speed == 1.0:
        return None
    if not math.isfinite(speed) or speed == 0.0:
        raise ValueError(
            f"speed must be a non-zero number (got {speed}); use a negative "
            "value to play the range backwards"
        )
    order = speed_baked_indices(count, count, abs(speed))
    if speed < 0:
        order = (count - 1) - order
    return order


class LoadVideoTrim:
    """Load a frame range of a video file, optionally cropped, as a VIDEO and an IMAGE batch."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file": (
                    # The leading blank is the default, so a freshly added node
                    # never silently points at whatever file happens to sort first.
                    ["", *_list_video_files()],
                    {
                        "default": "",
                        "tooltip": "Video file in the ComfyUI input directory. Upload or drop one onto the node.",
                    },
                ),
                "start_frame": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_FRAME_INDEX,
                        "step": 1,
                        "tooltip": "First frame of the range, counting from 0.",
                    },
                ),
                "end_frame": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_FRAME_INDEX,
                        "step": 1,
                        "tooltip": "Frame the range stops before (it is not included); 0 means the end of the file. For a seamless loop, set it to the frame that looks like start_frame — the loop then returns to start_frame instead of showing that frame twice.",
                    },
                ),
                "crop_x": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_CROP,
                        "step": 1,
                        "tooltip": "Left edge of the crop box in pixels.",
                    },
                ),
                "crop_y": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_CROP,
                        "step": 1,
                        "tooltip": "Top edge of the crop box in pixels.",
                    },
                ),
                "crop_w": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_CROP,
                        "step": 1,
                        "tooltip": "Width of the crop box in pixels; 0 runs to the right edge.",
                    },
                ),
                "crop_h": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_CROP,
                        "step": 1,
                        "tooltip": "Height of the crop box in pixels; 0 runs to the bottom edge.",
                    },
                ),
                "speed": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": -MAX_SPEED,
                        "max": MAX_SPEED,
                        "step": 0.05,
                        "round": 0.01,
                        "display": "slider",
                        "tooltip": "Playback speed, baked into the frames: the frame rate stays the file's own. 2 keeps every other frame of the range (half the frames, half the time), 0.5 repeats each frame (twice the frames), and a negative value plays the range backwards. Anything other than 1 drops the audio. 0 is not allowed.",
                    },
                ),
            },
        }

    # `fps`, `width` and `height` are appended after the two original outputs so
    # saved workflows keep their links. `width`/`height` are the frames as
    # emitted, i.e. after the crop; `fps` is the file's rate, which `speed`
    # deliberately does not change.
    RETURN_TYPES = ("VIDEO", "IMAGE", "FLOAT", "INT", "INT")
    RETURN_NAMES = ("video", "images", "fps", "width", "height")
    FUNCTION = "load"
    CATEGORY = "load-gif-as-video"
    DESCRIPTION = (
        "Loads a frame range of a video, optionally cropped, as a VIDEO with its "
        "audio and as an IMAGE batch, plus the frame rate and the cropped size. "
        "The node's panel scrubs the file, sets the range and crop box, and "
        "finds frames that match the one under the playhead, so a loop can be "
        "cut where the picture repeats. `speed` drops or repeats frames (or "
        "reverses them) at the file's own frame rate."
    )

    def load(
        self,
        file,
        start_frame,
        end_frame,
        crop_x,
        crop_y,
        crop_w,
        crop_h,
        speed=1.0,
    ):
        if VideoFromComponents is None or VideoComponents is None:
            raise RuntimeError(
                "This ComfyUI build has no VIDEO support (comfy_api.input_impl is "
                f"unavailable): {_VIDEO_IMPORT_ERROR}"
            )

        path = folder_paths.get_annotated_filepath(file)
        index = video_source.get_index(path, Progress(_claimed_frames(path)))
        total = index.frame_count

        start = int(start_frame)
        end = int(end_frame) if int(end_frame) > 0 else total
        end = min(end, total)
        if start >= total:
            raise ValueError(
                f"start_frame {start} is past the last frame ({total - 1}) of '{file}'"
            )
        if end <= start:
            raise ValueError(
                f"end_frame {end} must be after start_frame {start} (the range is "
                "start_frame <= frame < end_frame)"
            )
        count = end - start
        if count > MAX_OUTPUT_FRAMES:
            raise ValueError(
                f"The range {start}..{end} is {count} frames, which exceeds the "
                f"{MAX_OUTPUT_FRAMES} frame limit"
            )
        order = speed_order(count, speed)  # also enforces the limit on the output

        images = video_source.decode_range(
            path,
            start,
            end,
            crop=(crop_x, crop_y, crop_w, crop_h),
            index=index,
            progress=Progress(count),
            order=order,
        )

        fps = Fraction(index.frame_rate)
        # Audio only at speed 1: the frame rate does not move, so a resampled or
        # reversed waveform would be needed to stay in step, and no audio beats
        # wrong audio (the same call `Loop Video` makes).
        audio = None
        if order is None:
            audio = video_source.decode_audio(
                path, index.frame_time(start), float(count / fps)
            )
        video = VideoFromComponents(
            VideoComponents(images=images, frame_rate=fps, audio=audio)
        )
        return (video, images, float(fps), int(images.shape[2]), int(images.shape[1]))

    @classmethod
    def IS_CHANGED(cls, file, **kwargs):
        if not file:
            return float("nan")
        path = folder_paths.get_annotated_filepath(file)
        return os.path.getmtime(path)

    @classmethod
    def VALIDATE_INPUTS(cls, file, **kwargs):
        if not file:
            return "No video file selected"
        if not folder_paths.exists_annotated_filepath(file):
            return f"Invalid video file: {file}"
        return True


def _claimed_frames(path):
    """The container's own frame count, for sizing the index progress bar."""
    try:
        import av

        with av.open(path) as container:
            for stream in container.streams:
                if stream.type == "video":
                    return int(stream.frames or 0)
    except Exception:
        pass
    return 0


# --- UI routes ------------------------------------------------------------------
#
# The panel needs three things the graph cannot give it without being queued:
# the file's frame count and rate, an exact frame to look at, and the similar
# frame search. All three go through the index cache, so the file is decoded
# once and every later call is a seek.

_THUMB_CACHE_LIMIT = 128
_thumb_cache = OrderedDict()


def _resolve_file(name):
    """The on-disk path for an input-directory file name, or None if it is not one."""
    if not name or not folder_paths.exists_annotated_filepath(name):
        return None
    return folder_paths.get_annotated_filepath(name)


def _info(path):
    index = video_source.get_index(path)
    return {
        "fingerprint_sizes": list(video_source.FINGERPRINT_SIZES),
        "frames": index.frame_count,
        "fps": float(index.frame_rate),
        "fps_fraction": [index.frame_rate.numerator, index.frame_rate.denominator],
        "width": index.width,
        "height": index.height,
        "duration": index.duration,
        "seekable": index.seekable,
        "key": f"{os.path.getmtime(path):.0f}-{os.path.getsize(path)}",
    }


def _thumbnail(path, frame, size, crop=None):
    """JPEG bytes of one frame, bounded to `size`, cut to `crop` (x, y, w, h) if given.

    The crop is applied so the panel's Start/End thumbnails show what the node
    will actually cut, not the whole frame with a box drawn over it.
    """
    key = (video_source._cache_key(path), int(frame), int(size), crop)
    cached = _thumb_cache.get(key)
    if cached is not None:
        _thumb_cache.move_to_end(key)
        return cached
    rgb = video_source.read_frame(path, int(frame))
    if crop is not None:
        x, y, w, h = video_source.resolve_crop(rgb.shape[1], rgb.shape[0], *crop)
        rgb = rgb[y : y + h, x : x + w]
    image = Image.fromarray(rgb)
    if size > 0 and max(image.size) > size:
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    data = buffer.getvalue()
    _thumb_cache[key] = data
    while len(_thumb_cache) > _THUMB_CACHE_LIMIT:
        _thumb_cache.popitem(last=False)
    return data


def _register_routes():
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    instance = getattr(PromptServer, "instance", None)
    if instance is None:  # imported outside a running server (tests)
        return
    routes = instance.routes

    async def run(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def bad_request(message):
        return web.json_response({"error": message}, status=400)

    @routes.get("/loadgifasvideo/video/info")
    async def video_info(request):
        path = _resolve_file(request.query.get("file", ""))
        if path is None:
            return web.json_response({"error": "No such input file"}, status=404)
        try:
            return web.json_response(await run(_info, path))
        except Exception as exc:
            return bad_request(str(exc))

    @routes.get("/loadgifasvideo/video/frame")
    async def video_frame(request):
        path = _resolve_file(request.query.get("file", ""))
        if path is None:
            return web.json_response({"error": "No such input file"}, status=404)
        try:
            frame = int(request.query.get("frame", "0"))
            size = max(0, min(int(request.query.get("size", "512")), 2048))
            crop = request.query.get("crop")
            if crop:
                crop = tuple(int(v) for v in crop.split(","))
                if len(crop) != 4:
                    raise ValueError("crop must be x,y,w,h")
            data = await run(_thumbnail, path, frame, size, crop or None)
        except (ValueError, OSError) as exc:
            return bad_request(str(exc))
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @routes.get("/loadgifasvideo/video/search")
    async def video_search(request):
        path = _resolve_file(request.query.get("file", ""))
        if path is None:
            return web.json_response({"error": "No such input file"}, status=404)
        try:
            frame = int(request.query.get("frame", "0"))
            match_frames = int(request.query.get("match_frames", "1"))
            similarity = float(request.query.get("similarity", "98"))
            fingerprint = video_source.normalize_fingerprint_size(
                request.query.get("fingerprint", video_source.FINGERPRINT_SIZE)
            )
            start = int(request.query.get("start", "0"))
            end = int(request.query.get("end", "0"))
            metric = request.query.get("metric", "pixels")
            blur = int(request.query.get("blur", "0"))
            crop = request.query.get("crop")
            if crop:
                crop = tuple(int(v) for v in crop.split(","))
                if len(crop) != 4:
                    raise ValueError("crop must be x,y,w,h")
            # A size (or range, or crop) not indexed yet re-reads the frames;
            # the panel says so. With a range, only those frames are
            # fingerprinted, which is what makes the large grids affordable.
            if start > 0 or end > 0:
                fingerprints = await run(
                    video_source.get_range_fingerprints,
                    path,
                    start,
                    end,
                    fingerprint,
                    crop or None,
                )
            else:
                fingerprints = await run(
                    video_source.get_index, path, None, fingerprint, crop or None
                )
            result = await run(
                video_source.find_similar,
                fingerprints,
                frame,
                match_frames,
                similarity,
                video_source.MAX_MATCHES,
                metric,
                blur,
            )
        except (ValueError, OSError) as exc:
            return bad_request(str(exc))
        return web.json_response(result)


_register_routes()


NODE_CLASS_MAPPINGS = {
    "LoadVideoTrim": LoadVideoTrim,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadVideoTrim": "Load Video (Trim & Loop Assist)",
}
