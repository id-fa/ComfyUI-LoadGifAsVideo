"""Length/speed widgets and frame-count math shared by both nodes in this package."""

from fractions import Fraction

import numpy as np

LENGTH_MODES = ["frames", "seconds", "loops"]
MAX_OUTPUT_FRAMES = 10000


def length_inputs(source_name="animation"):
    """The `length_mode` / `frames` / `seconds` / `loops` / `speed` widget block.

    `source_name` only appears in tooltips.
    """
    return {
        "length_mode": (
            LENGTH_MODES,
            {
                "default": "frames",
                "tooltip": "How the output length is specified: a frame count, a duration in seconds, or whole playthroughs.",
            },
        ),
        "frames": (
            "INT",
            {
                "default": 16,
                "min": 1,
                "max": MAX_OUTPUT_FRAMES,
                "step": 1,
                "tooltip": "Number of frames to output (length_mode = frames).",
            },
        ),
        "seconds": (
            "FLOAT",
            {
                "default": 2.0,
                "min": 0.01,
                "max": 3600.0,
                "step": 0.1,
                "tooltip": "Duration to output in seconds (length_mode = seconds).",
            },
        ),
        "loops": (
            "INT",
            {
                "default": 1,
                "min": 1,
                "max": MAX_OUTPUT_FRAMES,
                "step": 1,
                "tooltip": f"How many times the {source_name} plays through (length_mode = loops).",
            },
        ),
        "speed": (
            "FLOAT",
            {
                "default": 1.0,
                "min": 0.01,
                "max": 100.0,
                "step": 0.05,
                "tooltip": "Playback speed multiplier. Scales the VIDEO output's frame rate; the IMAGE batch, which has no frame rate, gets the speed baked into its frames instead.",
            },
        ),
    }


def scaled_frame_rate(base_fps, speed):
    """The source's own frame rate multiplied by `speed`, as an exact Fraction."""
    fps = Fraction(base_fps) * Fraction(speed).limit_denominator(1000)
    if fps <= 0:
        raise ValueError(f"Resulting frame rate is not positive (speed={speed})")
    return fps


def resolve_frame_count(length_mode, frames, seconds, loops, fps, source_length):
    """Number of output frames for the chosen mode, capped at MAX_OUTPUT_FRAMES.

    `loops` is deliberately not speed-scaled: N loops is always exactly N whole
    playthroughs, and `speed` only changes how long they take.
    """
    if length_mode == "seconds":
        count = max(1, round(seconds * float(fps)))
    elif length_mode == "loops":
        count = int(loops) * source_length
    else:
        count = int(frames)

    if count > MAX_OUTPUT_FRAMES:
        raise ValueError(
            f"Requested {count} output frames, which exceeds the {MAX_OUTPUT_FRAMES} frame limit"
        )
    return count


def speed_baked_indices(count, source_length, speed):
    """Source-frame indices for the IMAGE output, with `speed` baked into the frames.

    The VIDEO output expresses `speed` as a frame rate, but an IMAGE batch carries
    no frame rate at all — hand it to `Create Video` or a Video Combine node and
    the speed is simply lost. So the batch expresses the same speed change as
    frames instead: it steps `speed` source frames per output frame, over however
    many output frames fill the duration the VIDEO covers. Played back at the
    source's own rate the batch is then the same length, and shows the same
    motion, as the VIDEO output.

    At `speed = 1.0` this is exactly `arange(count) % source_length`, so the batch
    stays a frame-exact copy of the VIDEO frames.
    """
    ratio = Fraction(speed).limit_denominator(1000)
    if ratio <= 0:
        raise ValueError(f"Speed must be positive (speed={speed})")

    image_count = max(1, round(Fraction(count) / ratio))
    if image_count > MAX_OUTPUT_FRAMES:
        raise ValueError(
            f"speed={speed} needs {image_count} image frames to match the video's "
            f"duration, which exceeds the {MAX_OUTPUT_FRAMES} frame limit"
        )

    # Integer math on the exact ratio: float `i * speed` can land a hair below an
    # integer boundary and hold a frame one step too long.
    steps = np.arange(image_count, dtype=np.int64) * ratio.numerator
    return (steps // ratio.denominator) % source_length
