"""Length/speed widgets and frame-count math shared by both nodes in this package."""

from fractions import Fraction

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
                "tooltip": "Playback speed multiplier. Scales the output frame rate; frames are never interpolated or dropped.",
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
