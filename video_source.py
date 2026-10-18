"""Frame-exact access to a video file, shared by the Load Video node and its UI routes.

Everything here is keyed on *frame indices in decode order*, never on timestamps.
The browser, the thumbnail route, the loop-point search and the node itself all
have to agree on what "frame 340" is, and the only way to guarantee that across
seeks is to decode the file once, remember every frame's pts, and seek to the
remembered pts afterwards. That one pass also produces the fingerprints the loop
assist searches, so the index is built once per file and cached.
"""

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction

import av
import numpy as np
import torch
from av.error import FFmpegError, InvalidDataError

# Fingerprints are the frame area-averaged down to this square, RGB, 8-bit. The
# averaging is what makes the comparison tolerant of codec noise and banding;
# the size is a balance between discrimination and memory (3 KB per frame).
FINGERPRINT_SIZE = 32

# A low threshold on a long clip can match thousands of frames; the list is
# capped so the UI never has to render that.
MAX_MATCHES = 100

# How many indexed files to keep. An index is roughly 3 KB per frame.
_INDEX_CACHE_LIMIT = 4

# Seconds to seek short of an audio range's start; see `decode_audio`.
_AUDIO_SEEK_MARGIN = 1.0

_VIDEO_EXTENSIONS = (
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4v",
    ".avi",
    ".wmv",
    ".flv",
    ".gif",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
)


@dataclass
class VideoIndex:
    """Everything about a file that the frame-exact paths need, built in one decode pass."""

    path: str
    width: int  # after display rotation
    height: int
    frame_rate: Fraction
    time_base: Fraction
    rotation: int  # quadrants, as `np.rot90` wants them
    pts: np.ndarray  # int64 [N]; -1 where the decoder gave no timestamp
    fingerprints: np.ndarray  # uint8 [N, FINGERPRINT_SIZE * FINGERPRINT_SIZE * 3]

    @property
    def frame_count(self):
        return int(self.pts.shape[0])

    @property
    def seekable(self):
        """Seeking by remembered pts is only safe when every frame has one and they increase."""
        if self.frame_count == 0 or bool((self.pts < 0).any()):
            return False
        return bool((np.diff(self.pts) > 0).all())

    @property
    def duration(self):
        return float(self.frame_count / self.frame_rate) if self.frame_rate else 0.0

    def frame_time(self, frame):
        """Presentation time of `frame` in seconds, on the container's own clock."""
        if self.seekable:
            return float(self.pts[frame] * self.time_base)
        return float(frame / self.frame_rate)


def is_video_file(name):
    return name.lower().endswith(_VIDEO_EXTENSIONS)


def _rotation_quadrant(frame):
    rotation = getattr(frame, "rotation", 0) or 0
    return int(round(rotation // 90)) % 4


def _first_video_stream(container):
    if not container.streams.video:
        raise ValueError("The file has no video stream")
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    return stream


def _decoded_frames(container, stream):
    """Yield frames packet by packet, skipping packets the decoder rejects.

    `container.decode()` would abort on the first corrupt packet; demuxing and
    decoding per packet lets a damaged file still yield the frames it has.
    """
    for packet in container.demux(stream):
        try:
            frames = packet.decode()
        except InvalidDataError:
            logging.info("pyav decode error, skipping packet")
            continue
        yield from frames


def _to_rgb(frame, rotation):
    rgb = frame.to_ndarray(format="rgb24")
    if rotation:
        rgb = np.rot90(rgb, k=rotation, axes=(0, 1))
    return np.ascontiguousarray(rgb)


# --- index ---------------------------------------------------------------------

_cache = OrderedDict()
_cache_guard = threading.Lock()
_build_locks = {}


def _cache_key(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def build_index(path, progress=None):
    """Decode the whole file once, recording each frame's pts and fingerprint.

    `progress(done, total)` is called per frame; `total` is the container's own
    frame count claim, which may be 0 when the format does not record one.
    """
    pts = []
    fingerprints = []
    rotation = None
    with av.open(path) as container:
        stream = _first_video_stream(container)
        total = int(stream.frames or 0)
        time_base = (
            Fraction(stream.time_base) if stream.time_base else Fraction(1, 1000)
        )
        frame_rate = Fraction(stream.average_rate) if stream.average_rate else None
        width, height = int(stream.width), int(stream.height)

        for frame in _decoded_frames(container, stream):
            if rotation is None:
                rotation = _rotation_quadrant(frame)
            pts.append(-1 if frame.pts is None else int(frame.pts))
            small = frame.reformat(
                width=FINGERPRINT_SIZE,
                height=FINGERPRINT_SIZE,
                format="rgb24",
                interpolation="AREA",
            )
            fingerprints.append(small.to_ndarray().reshape(-1))
            if progress is not None:
                progress(len(pts), total)

    if not pts:
        raise ValueError(f"No frames could be decoded from '{path}'")

    pts = np.asarray(pts, dtype=np.int64)
    if frame_rate is None or frame_rate <= 0:
        # No average rate in the header: derive one from the timestamps, or give up
        # and call it 1 fps the way ComfyUI's own loader does.
        span = (
            (pts[-1] - pts[0]) * time_base if len(pts) > 1 and (pts >= 0).all() else 0
        )
        frame_rate = (
            Fraction(len(pts) - 1) / span if span > 0 else Fraction(1)
        ).limit_denominator(10000)

    rotation = rotation or 0
    if rotation % 2:
        width, height = height, width

    return VideoIndex(
        path=os.path.abspath(path),
        width=width,
        height=height,
        frame_rate=frame_rate,
        time_base=time_base,
        rotation=rotation,
        pts=pts,
        fingerprints=np.stack(fingerprints).astype(np.uint8),
    )


def get_index(path, progress=None):
    """The cached index for `path`, building it on first use.

    Concurrent requests for the same file (the UI asks for info and a thumbnail
    at once) share one build instead of decoding the file twice.
    """
    key = _cache_key(path)
    with _cache_guard:
        index = _cache.get(key)
        if index is not None:
            _cache.move_to_end(key)
            return index
        lock = _build_locks.setdefault(key, threading.Lock())

    with lock:
        with _cache_guard:
            index = _cache.get(key)
        if index is None:
            index = build_index(path, progress)
            with _cache_guard:
                _cache[key] = index
                _cache.move_to_end(key)
                while len(_cache) > _INDEX_CACHE_LIMIT:
                    _cache.popitem(last=False)
                _build_locks.pop(key, None)
    return index


def cached_index(path):
    """The index if it is already built, else None. Never decodes."""
    try:
        key = _cache_key(path)
    except OSError:
        return None
    with _cache_guard:
        return _cache.get(key)


# --- loop-point search ---------------------------------------------------------


def _similarity_rows(rows, query):
    """Per-row similarity in percent: 100 means identical fingerprints."""
    out = np.empty(rows.shape[0], dtype=np.float32)
    query = query.astype(np.int16)
    step = 4096
    for start in range(0, rows.shape[0], step):
        block = rows[start : start + step].astype(np.int16)
        block -= query
        np.abs(block, out=block)
        out[start : start + step] = block.mean(axis=1, dtype=np.float32)
    return 100.0 * (1.0 - out / 255.0)


def find_similar(index, frame, match_frames=1, threshold=98.0, limit=MAX_MATCHES):
    """Frames whose fingerprint (and the next `match_frames - 1`) match `frame`'s.

    A match is scored by the *worst* of its `match_frames` pairs, so every frame
    of the window has to clear the threshold. Consecutive matching frames are
    collapsed into one candidate (the best of the run) because a slow scene
    matches itself for dozens of frames and would otherwise drown the list; the
    run that contains the query frame is dropped for the same reason. The
    result is capped at `limit` by score, then listed in frame order.
    """
    count = index.frame_count
    if count == 0:
        raise ValueError("The video has no frames")
    frame = int(frame)
    if frame < 0 or frame >= count:
        raise ValueError(f"frame {frame} is outside 0..{count - 1}")

    match_frames = max(1, min(int(match_frames), count - frame))
    windows = count - match_frames + 1
    fingerprints = index.fingerprints

    score = np.full(windows, 100.0, dtype=np.float32)
    for k in range(match_frames):
        similarity = _similarity_rows(
            fingerprints[k : k + windows], fingerprints[frame + k]
        )
        np.minimum(score, similarity, out=score)

    matched = score >= float(threshold)
    matches = []
    total = 0
    if matched.any():
        padded = np.concatenate(([False], matched, [False]))
        edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
        for run_start, run_end in zip(edges[::2], edges[1::2]):
            if run_start <= frame < run_end:
                continue
            total += 1
            best = run_start + int(np.argmax(score[run_start:run_end]))
            matches.append(
                {
                    "frame": int(best),
                    "score": round(float(score[best]), 2),
                    "run_start": int(run_start),
                    "run_end": int(run_end - 1),
                }
            )

    matches.sort(key=lambda m: (-m["score"], m["frame"]))
    truncated = len(matches) > limit
    matches = sorted(matches[:limit], key=lambda m: m["frame"])
    return {
        "query": frame,
        "match_frames": match_frames,
        "threshold": float(threshold),
        "total": total,
        "truncated": truncated,
        "matches": matches,
    }


# --- frames --------------------------------------------------------------------


def resolve_crop(width, height, x, y, w, h):
    """Clamp a crop box to the frame; a zero width or height runs to the edge."""
    x = max(0, min(int(x), width - 1))
    y = max(0, min(int(y), height - 1))
    w = width - x if int(w) <= 0 else min(int(w), width - x)
    h = height - y if int(h) <= 0 else min(int(h), height - y)
    return x, y, w, h


def _frames_from(container, stream, index, start):
    """Yield (frame_index, av.VideoFrame) from `start` onward, seeking when the index allows.

    One generator is used throughout: a packet can decode to several frames, and
    starting a second generator after the seek loop would drop the rest of the
    packet the loop stopped in.
    """
    frames = _decoded_frames(container, stream)
    i = 0
    if index.seekable and start > 0:
        target = int(index.pts[start])
        container.seek(target, stream=stream, backward=True, any_frame=False)
        for frame in frames:
            if frame.pts is not None and frame.pts >= target:
                if frame.pts != target:
                    logging.warning(
                        "Seek landed on pts %s instead of %s in '%s'",
                        frame.pts,
                        target,
                        index.path,
                    )
                yield start, frame
                i = start + 1
                break
        else:
            return
    for frame in frames:
        if i >= start:
            yield i, frame
        i += 1


def read_frame(path, frame, index=None):
    """One frame as uint8 RGB [H, W, 3], display-rotated, uncropped."""
    index = index or get_index(path)
    if frame < 0 or frame >= index.frame_count:
        raise ValueError(f"frame {frame} is outside 0..{index.frame_count - 1}")
    with av.open(path) as container:
        stream = _first_video_stream(container)
        for i, decoded in _frames_from(container, stream, index, frame):
            if i == frame:
                return _to_rgb(decoded, index.rotation)
    raise ValueError(f"Frame {frame} could not be decoded from '{path}'")


def decode_range(path, start, end, crop=None, index=None, progress=None):
    """Frames `start <= i < end` as a float32 tensor [N, H, W, 3] in 0..1.

    The tensor is allocated up front and filled frame by frame, so the full
    batch never exists twice (once as uint8, once as float).
    """
    index = index or get_index(path)
    if not 0 <= start < end <= index.frame_count:
        raise ValueError(
            f"Frame range {start}..{end} is not within 0..{index.frame_count}"
        )
    x, y, w, h = resolve_crop(index.width, index.height, *(crop or (0, 0, 0, 0)))
    count = end - start
    out = torch.empty((count, h, w, 3), dtype=torch.float32)

    filled = 0
    with av.open(path) as container:
        stream = _first_video_stream(container)
        for i, decoded in _frames_from(container, stream, index, start):
            if i >= end:
                break
            rgb = _to_rgb(decoded, index.rotation)[y : y + h, x : x + w]
            out[i - start] = (
                torch.from_numpy(np.ascontiguousarray(rgb)).float().div_(255.0)
            )
            filled += 1
            if progress is not None:
                progress(filled, count)

    if filled != count:
        raise ValueError(
            f"Decoded {filled} of the {count} requested frames from '{path}'"
        )
    return out


# --- audio ---------------------------------------------------------------------


def _decodable_audio_stream(container):
    # Streams FFmpeg has no decoder for carry no codec context, and decoding
    # their packets crashes the process (iPhone APAC spatial audio, for one).
    for stream in reversed(container.streams.audio):
        if stream.codec_context is not None:
            return stream
    return None


def decode_audio(path, start_time, duration):
    """The audio between `start_time` and `start_time + duration` as a ComfyUI AUDIO dict.

    Times are on the container's clock, the same one `VideoIndex.frame_time`
    reports, so the waveform lines up with the frames of the same range. Returns
    None when there is no usable audio; a missing track is never an error.
    """
    try:
        with av.open(path) as container:
            stream = _decodable_audio_stream(container)
            if stream is None:
                return None
            stream.thread_type = "AUTO"
            sample_rate = int(stream.sample_rate or 0)
            if sample_rate <= 0:
                return None
            resampler = av.audio.resampler.AudioResampler(format="fltp")
            end_time = start_time + duration
            wanted = int(round(duration * sample_rate))

            # Seek a little short of the start: an AAC decoder needs a packet or
            # two of priming after a seek, and some demuxers land on the packet
            # *after* the target. Frames before `start_time` are skipped below.
            if start_time > 0 and stream.time_base:
                container.seek(
                    int(max(0.0, start_time - _AUDIO_SEEK_MARGIN) / stream.time_base),
                    stream=stream,
                    backward=True,
                )

            chunks = []
            have = 0
            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except InvalidDataError:
                    continue
                for frame in frames:
                    for resampled in resampler.resample(frame):
                        t0 = resampled.time
                        if t0 is None:
                            continue
                        samples = resampled.to_ndarray()
                        if t0 >= end_time:
                            break
                        skip = int(round((start_time - t0) * sample_rate))
                        if skip >= samples.shape[-1]:
                            continue
                        if skip > 0:
                            samples = samples[..., skip:]
                        chunks.append(samples)
                        have += samples.shape[-1]
                    if have >= wanted:
                        break
                if have >= wanted:
                    break
    except FFmpegError as exc:
        logging.warning("Audio could not be read from '%s': %s", path, exc)
        return None

    if not chunks:
        return None
    waveform = np.concatenate(chunks, axis=1)[..., :wanted]
    if waveform.shape[-1] == 0:
        return None
    return {
        "waveform": torch.from_numpy(np.ascontiguousarray(waveform)).unsqueeze(0),
        "sample_rate": sample_rate,
    }
