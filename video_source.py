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

# Fingerprints are the frame area-averaged down to a square, RGB, 8-bit. The
# averaging is what makes the comparison tolerant of codec noise and banding,
# and it is also what hides motion finer than one cell: a 32-cell grid cannot
# tell two frames apart whose difference averages away inside a cell. The size
# is therefore a per-search choice (the panel re-indexes the file at the size it
# asks for); the default balances discrimination against memory (3 KB per frame
# at 32, 48 KB at 128).
FINGERPRINT_SIZE = 32
FINGERPRINT_SIZES = (16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 640)
MAX_FINGERPRINT_SIZE = 640

# Hard ceiling on one fingerprint set. 640x640 is 1.2 MB per frame, which is
# only sane over a short range; past this the build refuses with advice rather
# than eating the machine.
_MAX_FINGERPRINT_BYTES = 1536 << 20

# A low threshold on a long clip can match thousands of frames; the list is
# capped so the UI never has to render that.
MAX_MATCHES = 100

# How much fingerprint memory to keep across indexed files (each size of each
# file is its own entry). Evicted least-recently-used; the one being used
# always stays.
_INDEX_CACHE_BYTES = 768 * 1024 * 1024

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
    fingerprints: np.ndarray  # uint8 [N, fingerprint_size * fingerprint_size * 3]
    fingerprint_size: int = FINGERPRINT_SIZE

    def fingerprint_set(self):
        return FingerprintSet(
            0, self.frame_count, self.fingerprint_size, self.fingerprints
        )

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


@dataclass
class FingerprintSet:
    """Fingerprints for frames `start <= i < end`, the unit the search runs over.

    A whole-file index yields one covering every frame; `get_range_fingerprints`
    builds one over just the trimmed range, which is what makes a very fine
    grid affordable — the memory is per frame *fingerprinted*, not per frame in
    the file.
    """

    start: int
    end: int
    fingerprint_size: int
    fingerprints: (
        np.ndarray
    )  # uint8 [end - start, fingerprint_size * fingerprint_size * 3]

    @property
    def count(self):
        return int(self.fingerprints.shape[0])


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


def _file_key(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def _cache_key(path, fingerprint_size=FINGERPRINT_SIZE):
    return (*_file_key(path), int(fingerprint_size))


def normalize_fingerprint_size(size):
    """Clamp a requested fingerprint size to the supported range (8..MAX_FINGERPRINT_SIZE)."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return FINGERPRINT_SIZE
    return max(8, min(size, MAX_FINGERPRINT_SIZE))


def _check_fingerprint_budget(count, size, what):
    needed = count * size * size * 3
    if needed > _MAX_FINGERPRINT_BYTES:
        raise ValueError(
            f"A {size}x{size} fingerprint over {count} frames ({what}) needs "
            f"{needed >> 20} MB, more than the {_MAX_FINGERPRINT_BYTES >> 20} MB "
            "limit. Choose a smaller grid, or search within a shorter range."
        )


def _fingerprint(frame, size):
    return (
        frame.reformat(width=size, height=size, format="rgb24", interpolation="AREA")
        .to_ndarray()
        .reshape(-1)
    )


def build_index(path, progress=None, fingerprint_size=FINGERPRINT_SIZE):
    """Decode the whole file once, recording each frame's pts and fingerprint.

    `progress(done, total)` is called per frame; `total` is the container's own
    frame count claim, which may be 0 when the format does not record one.
    """
    fingerprint_size = normalize_fingerprint_size(fingerprint_size)
    pts = []
    fingerprints = []
    rotation = None
    with av.open(path) as container:
        stream = _first_video_stream(container)
        total = int(stream.frames or 0)
        if total:
            _check_fingerprint_budget(total, fingerprint_size, "the whole file")
        time_base = (
            Fraction(stream.time_base) if stream.time_base else Fraction(1, 1000)
        )
        frame_rate = Fraction(stream.average_rate) if stream.average_rate else None
        width, height = int(stream.width), int(stream.height)

        for frame in _decoded_frames(container, stream):
            if rotation is None:
                rotation = _rotation_quadrant(frame)
            pts.append(-1 if frame.pts is None else int(frame.pts))
            fingerprints.append(_fingerprint(frame, fingerprint_size))
            if not total and len(pts) % 256 == 0:
                _check_fingerprint_budget(len(pts), fingerprint_size, "the whole file")
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
        fingerprint_size=fingerprint_size,
    )


def _any_cached(file_key):
    """Any cached whole-file index of this file, whatever its fingerprint size. Caller holds the guard."""
    for key in reversed(_cache):
        if len(key) == 4 and key[:3] == file_key:
            return _cache[key]
    return None


def _trim_cache(keep):
    """Evict least-recently-used indexes until the fingerprints fit the budget."""
    while len(_cache) > 1:
        total = sum(i.fingerprints.nbytes for i in _cache.values())
        if total <= _INDEX_CACHE_BYTES:
            break
        oldest = next(iter(_cache))
        if oldest == keep:
            break
        _cache.popitem(last=False)


def get_index(path, progress=None, fingerprint_size=None):
    """The cached index for `path`, building it on first use.

    `fingerprint_size=None` means "any size": the frame-exact paths (thumbnails,
    the node) only need the pts, so they take whatever is cached and build at
    the default otherwise. The search asks for a specific size and gets a fresh
    decode when that size is not cached yet — that is the "re-index" the panel
    offers for motion too fine for the default grid.

    Concurrent requests for the same file (the UI asks for info and a thumbnail
    at once) share one build instead of decoding the file twice.
    """
    file_key = _file_key(path)
    with _cache_guard:
        if fingerprint_size is None:
            index = _any_cached(file_key)
            if index is not None:
                _cache.move_to_end((*file_key, index.fingerprint_size))
                return index
            fingerprint_size = FINGERPRINT_SIZE
        fingerprint_size = normalize_fingerprint_size(fingerprint_size)
        key = (*file_key, fingerprint_size)
        index = _cache.get(key)
        if index is not None:
            _cache.move_to_end(key)
            return index
        lock = _build_locks.setdefault(key, threading.Lock())

    with lock:
        with _cache_guard:
            index = _cache.get(key)
        if index is None:
            index = build_index(path, progress, fingerprint_size)
            with _cache_guard:
                _cache[key] = index
                _cache.move_to_end(key)
                _trim_cache(key)
                _build_locks.pop(key, None)
    return index


def build_range_fingerprints(path, index, start, end, fingerprint_size):
    """Fingerprint only frames `start <= i < end`, seeking there through `index`."""
    fingerprint_size = normalize_fingerprint_size(fingerprint_size)
    if not 0 <= start < end <= index.frame_count:
        raise ValueError(
            f"Frame range {start}..{end} is not within 0..{index.frame_count}"
        )
    _check_fingerprint_budget(
        end - start, fingerprint_size, f"frames {start}-{end - 1}"
    )
    rows = []
    with av.open(path) as container:
        stream = _first_video_stream(container)
        for i, frame in _frames_from(container, stream, index, start):
            if i >= end:
                break
            rows.append(_fingerprint(frame, fingerprint_size))
    if len(rows) != end - start:
        raise ValueError(
            f"Decoded {len(rows)} of the {end - start} frames in {start}..{end} of '{path}'"
        )
    return FingerprintSet(start, end, fingerprint_size, np.stack(rows).astype(np.uint8))


def get_range_fingerprints(path, start, end, fingerprint_size):
    """Cached `FingerprintSet` over `[start, end)` at `fingerprint_size`, built on first use.

    Shares the cache (and its byte budget) with the whole-file indexes; the key
    carries the range, so a different range is a different build.
    """
    index = get_index(path)  # any grid: only the pts are needed for seeking
    start = max(0, int(start))
    end = index.frame_count if int(end) <= 0 else min(int(end), index.frame_count)
    fingerprint_size = normalize_fingerprint_size(fingerprint_size)
    if (start, end) == (0, index.frame_count):
        whole = cached_index(path, fingerprint_size)
        if whole is not None:
            return whole.fingerprint_set()
    key = (*_file_key(path), fingerprint_size, start, end)
    with _cache_guard:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        with _cache_guard:
            cached = _cache.get(key)
        if cached is None:
            cached = build_range_fingerprints(path, index, start, end, fingerprint_size)
            with _cache_guard:
                _cache[key] = cached
                _cache.move_to_end(key)
                _trim_cache(key)
                _build_locks.pop(key, None)
    return cached


def cached_index(path, fingerprint_size=None):
    """The index if it is already built (at `fingerprint_size`, or any size), else None. Never decodes."""
    try:
        file_key = _file_key(path)
    except OSError:
        return None
    with _cache_guard:
        if fingerprint_size is None:
            return _any_cached(file_key)
        return _cache.get((*file_key, normalize_fingerprint_size(fingerprint_size)))


# --- loop-point search ---------------------------------------------------------


def _similarity_rows(rows, query):
    """Per-row similarity in percent: 100 means identical fingerprints."""
    out = np.empty(rows.shape[0], dtype=np.float32)
    query = query.astype(np.int16)
    # Chunk by bytes, not rows: at 128x128 a row is 49 KB and 4096 rows of
    # int16 would be a 400 MB temporary.
    step = max(64, (8 << 20) // max(1, rows.shape[1] * 2))
    for start in range(0, rows.shape[0], step):
        block = rows[start : start + step].astype(np.int16)
        block -= query
        np.abs(block, out=block)
        out[start : start + step] = block.mean(axis=1, dtype=np.float32)
    return 100.0 * (1.0 - out / 255.0)


def find_similar(
    fingerprint_set, frame, match_frames=1, threshold=98.0, limit=MAX_MATCHES
):
    """Frames whose fingerprint (and the next `match_frames - 1`) match `frame`'s.

    `fingerprint_set` is a `FingerprintSet` (or a `VideoIndex`, taken whole);
    frame numbers in and out are absolute, and the search never leaves the
    set's range. A match is scored by the *worst* of its `match_frames` pairs,
    so every frame of the window has to clear the threshold. Consecutive
    matching frames are collapsed into one candidate (the best of the run)
    because a slow scene matches itself for dozens of frames and would
    otherwise drown the list; the run that contains the query frame is dropped
    for the same reason. The result is capped at `limit` by score, then listed
    in frame order.
    """
    if isinstance(fingerprint_set, VideoIndex):
        fingerprint_set = fingerprint_set.fingerprint_set()
    offset = fingerprint_set.start
    count = fingerprint_set.count
    if count == 0:
        raise ValueError("The video has no frames")
    absolute = int(frame)
    frame = absolute - offset
    if frame < 0 or frame >= count:
        raise ValueError(
            f"frame {absolute} is outside the searched range "
            f"{offset}..{fingerprint_set.end - 1}; move the playhead inside the "
            "range or search the whole file"
        )

    match_frames = max(1, min(int(match_frames), count - frame))
    windows = count - match_frames + 1
    fingerprints = fingerprint_set.fingerprints

    score = np.full(windows, 100.0, dtype=np.float32)
    for k in range(match_frames):
        similarity = _similarity_rows(
            fingerprints[k : k + windows], fingerprints[frame + k]
        )
        np.minimum(score, similarity, out=score)

    matched = score >= float(threshold)
    matches = []
    total = 0
    # The run the query sits in is reported so the UI can say *why* a low
    # threshold returns nothing: once every frame clears the bar, the whole
    # clip is that one run, and it is dropped as the query's own neighbourhood.
    query_run = (frame, frame)
    if matched.any():
        padded = np.concatenate(([False], matched, [False]))
        edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
        for run_start, run_end in zip(edges[::2], edges[1::2]):
            if run_start <= frame < run_end:
                query_run = (int(run_start), int(run_end - 1))
                continue
            total += 1
            best = run_start + int(np.argmax(score[run_start:run_end]))
            matches.append(
                {
                    "frame": int(best) + offset,
                    "score": round(float(score[best]), 2),
                    "run_start": int(run_start) + offset,
                    "run_end": int(run_end - 1) + offset,
                }
            )

    matches.sort(key=lambda m: (-m["score"], m["frame"]))
    truncated = len(matches) > limit
    matches = sorted(matches[:limit], key=lambda m: m["frame"])
    return {
        "query": absolute,
        "query_run_start": query_run[0] + offset,
        "query_run_end": query_run[1] + offset,
        "start": offset,
        "end": int(fingerprint_set.end),
        "match_frames": match_frames,
        "fingerprint_size": int(fingerprint_set.fingerprint_size),
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
