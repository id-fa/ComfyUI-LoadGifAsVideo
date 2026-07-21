"""LoopVideo — repeat any VIDEO until a requested length is filled, at an adjustable speed."""

import math

import torch

from .video_length import length_inputs, resolve_frame_count, scaled_frame_rate

try:
    from comfy_api.input_impl import VideoFromComponents
    from comfy_api.util import VideoComponents

    _VIDEO_IMPORT_ERROR = None
except ImportError as exc:  # ComfyUI too old to have the VIDEO type
    VideoFromComponents = None
    VideoComponents = None
    _VIDEO_IMPORT_ERROR = exc


def _loop_audio(audio, source_length, count, fps):
    """Repeat the audio alongside the looped frames.

    The source audio is first fitted (truncated or zero-padded) to exactly one
    video loop, so every repeat restarts in sync with frame 0 even when the
    source's audio and video tracks are not exactly the same length. Only valid
    at speed 1.0 — the caller drops the audio otherwise.
    """
    waveform = audio.get("waveform")
    sample_rate = int(audio.get("sample_rate") or 0)
    if waveform is None or sample_rate <= 0 or waveform.shape[-1] == 0:
        return None

    period = max(1, round(source_length * sample_rate / float(fps)))
    if waveform.shape[-1] < period:
        pad = torch.zeros(
            *waveform.shape[:-1],
            period - waveform.shape[-1],
            dtype=waveform.dtype,
            device=waveform.device,
        )
        one_loop = torch.cat([waveform, pad], dim=-1)
    else:
        one_loop = waveform[..., :period]

    target = max(1, round(count * sample_rate / float(fps)))
    repeats = math.ceil(target / period)
    tiled = one_loop.repeat(*([1] * (one_loop.dim() - 1)), repeats)[..., :target]
    return {"waveform": tiled, "sample_rate": sample_rate}


class LoopVideo:
    """Loop a VIDEO to a requested frame count, duration or number of playthroughs."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": (
                    "VIDEO",
                    {
                        "tooltip": "The video to loop. Short clips are the point — MP4, WEBM, or another node's output."
                    },
                ),
                **length_inputs("video"),
            },
        }

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    FUNCTION = "loop"
    CATEGORY = "load-gif-as-video"
    DESCRIPTION = (
        "Loops a video until the requested frame count, duration or number of "
        "playthroughs is filled, with an adjustable playback speed. Audio is "
        "looped along with the frames at speed 1.0, and dropped otherwise."
    )

    def loop(self, video, length_mode, frames, seconds, loops, speed):
        if VideoFromComponents is None or VideoComponents is None:
            raise RuntimeError(
                "This ComfyUI build has no VIDEO support (comfy_api.input_impl is "
                f"unavailable): {_VIDEO_IMPORT_ERROR}"
            )

        components = video.get_components()
        images = components.images
        source_length = int(images.shape[0])
        if source_length == 0:
            raise ValueError("The input video has no frames")

        fps = scaled_frame_rate(components.frame_rate, speed)
        count = resolve_frame_count(
            length_mode, frames, seconds, loops, fps, source_length
        )

        # Looping is just a modulo over the source frames.
        looped = images[torch.arange(count) % source_length]

        # Changing the speed only changes the frame rate, so the audio would no
        # longer line up with the frames. Dropping it beats shipping it out of sync.
        audio = None
        if float(speed) == 1.0 and components.audio is not None:
            audio = _loop_audio(components.audio, source_length, count, fps)

        return (
            VideoFromComponents(
                VideoComponents(images=looped, frame_rate=fps, audio=audio)
            ),
        )


NODE_CLASS_MAPPINGS = {
    "LoopVideo": LoopVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoopVideo": "Loop Video",
}
