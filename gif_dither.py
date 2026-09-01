"""Dithering for SaveAsGif — mapping true-color frames onto a fixed palette.

Two families, both clean-room implementations from the published descriptions:

- **Error diffusion.** The classic kernels, in raster order, exactly as AForge.NET
  applies them (`FloydSteinbergColorDithering` and friends), plus Atkinson.
- **Ordered.** gifsicle's `--dither=ordered` approach, which is Joel Yliluoma's
  positional dithering against an arbitrary palette: rather than perturbing the
  pixel by a threshold, it builds a *plan* — an ordered list of palette entries
  whose mixture approximates the source color — and the matrix picks which entry
  of the plan each pixel takes. That is what makes ordered dithering hold up
  against a 32-color palette, where threshold perturbation falls apart.

Distances are squared Euclidean in 8-bit sRGB, matching `gif_palette`.
"""

import numpy as np

from .gif_palette import luminance, nearest_index

# Error diffusion, as (dx, dy, weight) with a shared denominator. Only forward
# neighbors: dy > 0, or dy == 0 and dx > 0.
_DIFFUSION = {
    "floyd-steinberg": (
        16,
        ((1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)),
    ),
    "burkes": (
        32,
        ((1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2)),
    ),
    "stucki": (
        42,
        (
            (1, 0, 8),
            (2, 0, 4),
            (-2, 1, 2),
            (-1, 1, 4),
            (0, 1, 8),
            (1, 1, 4),
            (2, 1, 2),
            (-2, 2, 1),
            (-1, 2, 2),
            (0, 2, 4),
            (1, 2, 2),
            (2, 2, 1),
        ),
    ),
    "jarvis-judice-ninke": (
        48,
        (
            (1, 0, 7),
            (2, 0, 5),
            (-2, 1, 3),
            (-1, 1, 5),
            (0, 1, 7),
            (1, 1, 5),
            (2, 1, 3),
            (-2, 2, 1),
            (-1, 2, 3),
            (0, 2, 5),
            (1, 2, 3),
            (2, 2, 1),
        ),
    ),
    "sierra": (
        32,
        (
            (1, 0, 5),
            (2, 0, 3),
            (-2, 1, 2),
            (-1, 1, 4),
            (0, 1, 5),
            (1, 1, 4),
            (2, 1, 2),
            (-1, 2, 2),
            (0, 2, 3),
            (1, 2, 2),
        ),
    ),
    "sierra-2": (
        16,
        (
            (1, 0, 4),
            (2, 0, 3),
            (-2, 1, 1),
            (-1, 1, 2),
            (0, 1, 3),
            (1, 1, 2),
            (2, 1, 1),
        ),
    ),
    "sierra-lite": (
        4,
        ((1, 0, 2), (-1, 1, 1), (0, 1, 1)),
    ),
    # Atkinson deliberately propagates only 6/8 of the error; the missing quarter
    # is what gives it its high-contrast look. (gifsicle's own Atkinson adds one
    # neighbor twice and drops the lower-left one — an implementation slip, so the
    # published kernel is used here instead.)
    "atkinson": (
        8,
        ((1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)),
    ),
}

# Ordered dithering matrices, built rather than tabulated. Each entry is
# (matrix [mh, mw] of plan positions, plan length, max colors per plan).
_ORDERED = [
    "bayer-2x2",
    "bayer-4x4",
    "bayer-8x8",
    "random-64x64",
    "halftone",
    "halftone-square",
    "halftone-ordered",
    "halftone-square-ordered",
    "halftone-poster",
    "halftone-square-poster",
]

# The halftone screens, as (triangular lattice?, colors allowed per cell,
# channel-independent?). Three families share one dot geometry:
#
# - The two-color pair are true halftone *screens*: a cell alternates between
#   exactly two palette entries, which is what makes it read as printed ink.
# - The `-ordered` pair lifts that limit, so a cell may hold as many palette
#   entries as the plan does. The dots stay but carry tone as well as coverage —
#   an ordinary ordered dither on a halftone lattice.
# - The `-poster` pair is ImageMagick's `-ordered-dither`: it does not search the
#   palette at all, it rounds each channel on its own against the screen. That is
#   what collapses the picture onto a handful of primaries and throws away most
#   of the original tone, which is the whole point of it.
_HALFTONE_SCREENS = {
    "halftone": (True, 2, False),
    "halftone-square": (False, 2, False),
    "halftone-ordered": (True, None, False),
    "halftone-square-ordered": (False, None, False),
    "halftone-poster": (True, None, True),
    "halftone-square-poster": (False, None, True),
}

# The dithers that quantize each channel on its own and therefore dictate their
# own palette: an even RGB grid, not an adaptive one. `SaveAsGif` checks this.
POSTER_DITHERS = [m for m, (_, _, poster) in _HALFTONE_SCREENS.items() if poster]

DITHER_METHODS = ["none", *_DIFFUSION, *_ORDERED]

# Which end of the plan the halftone dot grows from. See `_ordered_matrix`.
HALFTONE_INKS = ["black", "white"]

# Halftone screen size, in pixels across one cell. 6 is gifsicle's default.
DEFAULT_HALFTONE_SIZE = 6
MIN_HALFTONE_SIZE = 2
MAX_HALFTONE_SIZE = 64

# Threshold steps a halftone screen resolves. 0 means "one per cell", which is
# what gifsicle does and what the screen geometry implies on its own.
DEFAULT_HALFTONE_STEPS = 0
MAX_HALFTONE_STEPS = 255

# Longest plan any matrix may ask for. gifsicle caps halftone screens here too:
# past 255 cells the screen starts sharing plan entries between cells rather than
# growing the plan, so a 64-pixel cell costs no more to build plans for than a
# 16-pixel one.
_MAX_PLAN_LENGTH = 255

# Source colors are snapped to 5 bits per channel before a plan is built for them,
# which caps the plan cache at 32768 rows and keeps it affordable. The snap error
# is at most ~4/255, far below the palette spacing any dither is working against.
_SNAP_BITS = 5
_SNAP_LEVELS = 1 << _SNAP_BITS

# How many distinct colors of a plan are considered when a matrix limits a plan to
# fewer colors than it has slots (the halftone matrices). Plans rarely hold more
# than a handful of distinct colors, so this is not a real ceiling in practice.
_MAX_PLAN_CANDIDATES = 16


def _bayer(size):
    """The recursive Bayer / ordered threshold matrix of the given power-of-two size."""
    matrix = np.zeros((1, 1), dtype=np.int64)
    while matrix.shape[0] < size:
        matrix = np.block(
            [
                [4 * matrix, 4 * matrix + 2],
                [4 * matrix + 3, 4 * matrix + 1],
            ]
        )
    return matrix


def _random_ordered(size=64, levels=16, seed=0x5EED):
    """A large "random" ordered matrix, in the spirit of gifsicle's `ro64`.

    Every levels-sized tile holds a full permutation of the levels, so the matrix
    is locally uniform like a Bayer matrix but carries none of its cross-hatching.
    Generated from a fixed seed, so a given build always dithers identically.
    """
    rng = np.random.default_rng(seed)
    tile = int(round(levels**0.5))
    matrix = np.empty((size, size), dtype=np.int64)
    for y in range(0, size, tile):
        for x in range(0, size, tile):
            matrix[y : y + tile, x : x + tile] = rng.permutation(levels).reshape(
                tile, tile
            )
    return matrix


def _halftone(width, height, triangular, steps=0):
    """A halftone screen: cells ordered outward from the dot center(s).

    This is gifsicle's halftone matrix generator. Cells are ranked by distance to
    the nearest dot center, ties broken by angle around it, so the ordering grows
    a round dot rather than a square block. The triangular variant adds centers at
    the four corners, which offsets alternate rows into the classic newsprint
    lattice instead of a square grid.
    """
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    centers = [((width - 1) / 2.0, (height - 1) / 2.0)]
    if triangular:
        centers += [
            (-0.5, -0.5),
            (width - 0.5, -0.5),
            (-0.5, height - 0.5),
            (width - 0.5, height - 0.5),
        ]

    cx, cy = centers[0]
    best_distance = (xs - cx) ** 2 + (ys - cy) ** 2
    best_angle = np.arctan2(ys - cy, xs - cx)
    for cx, cy in centers[1:]:
        distance = (xs - cx) ** 2 + (ys - cy) ** 2
        nearer = distance < best_distance
        best_angle = np.where(nearer, np.arctan2(ys - cy, xs - cx), best_angle)
        best_distance = np.where(nearer, distance, best_distance)

    # gifsicle treats distances within 0.01 as equal and falls back to the angle;
    # quantizing the distance to that tolerance gives the same ordering from a
    # comparison that is actually transitive.
    order = np.lexsort(
        (best_angle.ravel(), np.round(best_distance.ravel() / 0.01).astype(np.int64))
    )
    cells = width * height
    # Cells share a step once there are more cells than steps, which is exactly
    # the loss of tonal resolution a coarse screen is asking for. gifsicle only
    # ever does this at its own 255 ceiling; `steps` lets a caller ask for it
    # sooner, which is what separates "big dots" from "fine tone" — the two are
    # otherwise welded together by the cell size.
    limit = _MAX_PLAN_LENGTH if steps <= 0 else min(steps, _MAX_PLAN_LENGTH)
    matrix = np.empty(cells, dtype=np.int64)
    if cells > limit:
        matrix[order] = (np.arange(cells) * (limit / cells)).astype(np.int64)
        nplan = limit
    else:
        matrix[order] = np.arange(cells)
        nplan = cells
    return matrix.reshape(height, width), nplan


def _ordered_matrix(method, halftone_size, halftone_ink, halftone_steps=0):
    """(matrix, plan length, max colors per plan) for an ordered method.

    The `halftone_*` arguments only reach the halftone screens.
    """
    if method == "bayer-2x2":
        return _bayer(2), 4, 4
    if method == "bayer-4x4":
        return _bayer(4), 16, 16
    if method == "bayer-8x8":
        return _bayer(8), 64, 64
    if method == "random-64x64":
        return _random_ordered(), 16, 16
    if method not in _HALFTONE_SCREENS:
        raise ValueError(f"Unknown ordered dither: {method}")
    triangular, max_colors, _ = _HALFTONE_SCREENS[method]

    size = int(halftone_size)
    if not MIN_HALFTONE_SIZE <= size <= MAX_HALFTONE_SIZE:
        raise ValueError(
            f"halftone_size must be between {MIN_HALFTONE_SIZE} and "
            f"{MAX_HALFTONE_SIZE}, got {size}"
        )
    if halftone_ink not in HALFTONE_INKS:
        raise ValueError(f"Unknown halftone ink: {halftone_ink}")
    steps = int(halftone_steps)
    if not 0 <= steps <= MAX_HALFTONE_STEPS:
        raise ValueError(
            f"halftone_steps must be between 0 and {MAX_HALFTONE_STEPS}, got {steps}"
        )

    if triangular:
        # gifsicle's triangular screen is `size` wide by size*sqrt(3) tall, which
        # is what lands the dots on a hexagonal lattice rather than a square one.
        matrix, nplan = _halftone(size, int(round(size * 3**0.5)), True, steps)
    else:
        matrix, nplan = _halftone(size, size, False, steps)

    if halftone_ink == "white":
        # Plans run dark to light and the matrix runs outward from the dot center,
        # so cell 0 normally takes the darkest color: a dark dot growing on a
        # light ground, the way ink sits on paper. Reversing the matrix grows the
        # light color out of a dark ground instead.
        matrix = nplan - 1 - matrix

    # `None` means no limit: the plan keeps whatever colors approximate the source
    # best, so the dots carry tone. A limit of 2 is what turns the same geometry
    # into a printed screen, where a cell only ever alternates ink and ground.
    return matrix, nplan, nplan if max_colors is None else max_colors


def _wavefront_slope(offsets):
    """`a` such that every error target has a strictly larger `a*y + x` than its source.

    Error diffusion is sequential in raster order, but only through these offsets.
    Pixels sharing one value of `a*y + x` therefore cannot reach each other and can
    be quantized together — which is what makes the loop below vectorize while
    still producing exactly the raster-order result.
    """
    lateral = [dx for dx, dy, _ in offsets if dy >= 1]
    return 1 + max(0, -min(lateral)) if lateral else 1


def _diffuse(rgb, palette, offsets, denom, strength):
    """Error-diffusion dither, returning palette indices as uint8 [H, W]."""
    height, width = rgb.shape[:2]
    slope = _wavefront_slope(offsets)
    pad_x = max(abs(dx) for dx, _, _ in offsets)
    pad_y = max(dy for _, dy, _ in offsets)

    # Padding the work buffer lets error that falls off an edge land in the margin
    # instead of needing a bounds mask on every scatter.
    work = np.zeros((height + pad_y, width + 2 * pad_x, 3), dtype=np.float32)
    work[:height, pad_x : pad_x + width] = rgb
    out = np.zeros((height, width), dtype=np.uint8)

    palette_f = palette.astype(np.float32)
    palette_sq = (palette_f * palette_f).sum(1)
    scale = np.float32(strength / denom)

    for k in range(slope * (height - 1) + width):
        low = max(0, -((width - 1 - k) // slope))
        high = min(height - 1, k // slope)
        if low > high:
            continue
        ys = np.arange(low, high + 1)
        xs = k - slope * ys

        current = np.clip(work[ys, xs + pad_x], 0.0, 255.0)
        chosen = nearest_index(current, palette_f, palette_sq)
        out[ys, xs] = chosen

        error = (current - palette_f[chosen]) * scale
        for dx, dy, weight in offsets:
            work[ys + dy, xs + dx + pad_x] += error * weight

    return out


def _build_plans(want, palette, nplan, strength):
    """Yliluoma plans: `nplan` palette entries whose mixture approximates `want`.

    Each step picks the palette entry closest to the color still owed, then adds
    what that entry over- or under-shot to the debt. `strength` scales how much of
    the debt is carried, so 0 collapses every plan to the plain nearest color.
    """
    palette_f = palette.astype(np.float32)
    palette_sq = (palette_f * palette_f).sum(1)

    plans = np.empty((len(want), nplan), dtype=np.uint8)
    owed = np.zeros_like(want)
    for i in range(nplan):
        chosen = nearest_index(np.clip(want + owed, 0.0, 255.0), palette_f, palette_sq)
        plans[:, i] = chosen
        owed += (want - palette_f[chosen]) * np.float32(strength)

    # Sort each plan by luminance so the matrix walks it dark to light; ties fall
    # back to the palette index, as gifsicle's comparator does.
    keys = np.rint(luminance(palette)).astype(np.int64) * 256 + np.arange(len(palette))
    return np.take_along_axis(plans, np.argsort(keys[plans], axis=1), axis=1)


def _limit_plans(plans, want, palette, max_colors):
    """Cut each plan down to a mixture of at most `max_colors` of its own entries.

    A halftone screen only reads as a halftone if each cell alternates between two
    colors; a plan holding five defeats it. For each plan this picks the single
    color, or the blend of two, that lands closest to the source color, then
    rewrites the plan as that blend's duty cycle.
    """
    rows, nplan = plans.shape
    palette_f = palette.astype(np.float32)

    starts = np.ones((rows, nplan), dtype=bool)
    starts[:, 1:] = plans[:, 1:] != plans[:, :-1]
    over_budget = np.flatnonzero(starts.sum(1) > max_colors)
    if len(over_budget) == 0:
        return plans

    plans = plans.copy()
    candidates = _MAX_PLAN_CANDIDATES
    left, right = np.triu_indices(candidates, k=1)

    # Chunked: the (rows, pairs, 3) blocks below are the widest arrays here.
    for begin in range(0, len(over_budget), 4096):
        block = over_budget[begin : begin + 4096]
        block_plans = plans[block]
        target = want[block]
        n = len(block)

        # The distinct colors of each plan, in the luminance order they are in.
        rank = np.cumsum(starts[block], axis=1) - 1
        entry = np.zeros((n, candidates), dtype=np.int64)
        valid = np.zeros((n, candidates), dtype=bool)
        for j in range(candidates):
            at_rank = rank == j
            valid[:, j] = at_rank.any(1)
            entry[:, j] = block_plans[np.arange(n), np.argmax(at_rank, axis=1)]

        colors = palette_f[entry]  # (n, candidates, 3)
        alone = ((colors - target[:, None, :]) ** 2).sum(2)
        alone[~valid] = np.inf

        low, high = colors[:, left, :], colors[:, right, :]
        span = high - low
        offset = target[:, None, :] - low
        squared = (span * span).sum(2)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(squared > 0, (span * offset).sum(2) / squared, -1.0)
        blended = low + span * np.clip(t, 0.0, 1.0)[:, :, None]
        pair = ((blended - target[:, None, :]) ** 2).sum(2)
        # Only a mixture that actually falls between the two is a duty cycle; an
        # endpoint is already covered by the single-color candidates.
        pair[(t < 0.0) | (t > 1.0) | ~(valid[:, left] & valid[:, right])] = np.inf

        best = np.argmin(np.concatenate([alone, pair], axis=1), axis=1)
        single = best < candidates
        pair_at = np.where(single, 0, best - candidates)
        rows_index = np.arange(n)

        first = np.where(
            single,
            entry[rows_index, np.minimum(best, candidates - 1)],
            entry[rows_index, left[pair_at]],
        )
        second = np.where(single, first, entry[rows_index, right[pair_at]])
        share = np.where(single, 0.0, t[rows_index, pair_at])
        # `first` is the darker of the two, so it takes the head of the plan.
        cut = np.clip(np.floor(nplan * (1.0 - share)), 0, nplan).astype(np.int64)

        position = np.arange(nplan)[None, :]
        plans[block] = np.where(
            position < cut[:, None], first[:, None], second[:, None]
        ).astype(np.uint8)

    return plans


class Ditherer:
    """Maps true-color frames onto one palette with a chosen dither.

    Holds the per-palette precomputation, so a global palette pays for its ordered
    dither plans once across the whole animation rather than once per frame.
    """

    def __init__(
        self,
        palette,
        method,
        strength,
        halftone_size=DEFAULT_HALFTONE_SIZE,
        halftone_ink="black",
        halftone_steps=DEFAULT_HALFTONE_STEPS,
    ):
        if method not in DITHER_METHODS:
            raise ValueError(f"Unknown dither method: {method}")
        self.palette = np.asarray(palette, dtype=np.uint8)
        self.method = method
        self.strength = float(strength)

        self._palette_f = self.palette.astype(np.float32)
        self._palette_sq = (self._palette_f * self._palette_f).sum(1)

        self.is_ordered = method in _ORDERED
        if self.is_ordered:
            self._matrix, self._nplan, self._max_plan_colors = _ordered_matrix(
                method, halftone_size, halftone_ink, halftone_steps
            )
        else:
            self._matrix, self._nplan, self._max_plan_colors = (
                np.zeros((1, 1), int),
                1,
                1,
            )
        self._plan_keys = np.zeros(0, dtype=np.int64)
        self._plans = np.zeros((0, self._nplan), dtype=np.uint8)

        # A channel-independent dither can only land on the points of an even RGB
        # grid, so its palette has to be exactly that; the level count is read
        # back from the palette rather than passed in, which also checks the
        # caller handed over the right thing.
        self.is_poster = method in POSTER_DITHERS
        self._levels = 0
        if self.is_poster:
            self._levels = 2
            while self._levels**3 < len(self.palette):
                self._levels += 1
            if self._levels**3 != len(self.palette):
                raise ValueError(
                    f"{method} needs a cubic RGB grid palette; got {len(self.palette)} "
                    "colors, which is not a cube. Build it with "
                    "gif_palette.uniform_palette()."
                )

    def __call__(self, frame):
        """Palette indices as uint8 [H, W] for one uint8 [H, W, 3] frame."""
        # At strength 0 every method degenerates to the plain nearest color, so
        # take the cheap path — and, for the ordered methods, skip the 5-bit snap
        # that would otherwise leave a few pixels off the exact nearest color.
        if self.method == "none" or self.strength <= 0.0:
            flat = frame.reshape(-1, 3).astype(np.float32)
            indices = np.empty(len(flat), dtype=np.uint8)
            # Chunked so a large frame never materializes a full pixels-by-palette
            # distance matrix.
            for begin in range(0, len(flat), 1 << 16):
                block = flat[begin : begin + (1 << 16)]
                indices[begin : begin + len(block)] = nearest_index(
                    block, self._palette_f, self._palette_sq
                )
            return indices.reshape(frame.shape[:2])

        if not self.is_ordered:
            denom, offsets = _DIFFUSION[self.method]
            return _diffuse(
                frame.astype(np.float32),
                self.palette,
                offsets,
                denom,
                self.strength,
            )

        return self._poster(frame) if self.is_poster else self._ordered(frame)

    def _poster(self, frame):
        """ImageMagick's `-ordered-dither`: each channel rounded on its own.

        No palette search happens here at all. Each channel is scaled onto the
        grid's levels and the screen decides whether the leftover fraction rounds
        up, so the output is a point of the grid by construction and its index is
        arithmetic rather than a lookup.

        This is the one dither that throws away most of the source's tone on
        purpose: at 2 levels a photograph collapses onto the eight corners of the
        RGB cube, and the dot screen is left carrying the whole picture. That is
        also why it compresses so far below the palette-searching dithers.

        `dither_strength` scales the screen around its midpoint, so 0 is a plain
        posterize with no pattern and 1 is ImageMagick's own amplitude.
        """
        height, width = frame.shape[:2]
        mh, mw = self._matrix.shape
        cell = self._matrix[
            np.arange(height)[:, None] % mh, np.arange(width)[None, :] % mw
        ]
        # ImageMagick's threshold maps run 1..divisor-1 over a divisor one larger
        # than the number of cells, so no cell ever thresholds at exactly 0 or 1.
        threshold = (cell + 1.0) / (self._nplan + 1.0)
        threshold = 0.5 + (threshold - 0.5) * self.strength

        scaled = frame.astype(np.float32) * ((self._levels - 1) / 255.0)
        below = np.floor(scaled)
        rounded = below + ((scaled - below) > threshold[..., None])
        quantized = np.clip(rounded, 0, self._levels - 1).astype(np.int64)

        return (
            quantized[..., 0] * self._levels**2
            + quantized[..., 1] * self._levels
            + quantized[..., 2]
        ).astype(np.uint8)

    def _ordered(self, frame):
        snapped = frame >> (8 - _SNAP_BITS)
        keys = (
            (snapped[..., 0].astype(np.int64) << (2 * _SNAP_BITS))
            | (snapped[..., 1].astype(np.int64) << _SNAP_BITS)
            | snapped[..., 2].astype(np.int64)
        )
        # Flattened first: NumPy 2.0 briefly made `return_inverse` keep the input
        # shape, so a 2-D `keys` does not reshape the same way on every version.
        unique, inverse = np.unique(keys.ravel(), return_inverse=True)
        plans = self._plans_for(unique)

        height, width = frame.shape[:2]
        mh, mw = self._matrix.shape
        cell = self._matrix[
            np.arange(height)[:, None] % mh, np.arange(width)[None, :] % mw
        ]
        return plans[inverse.reshape(height, width), cell]

    def _plans_for(self, keys):
        """Plan rows for `keys`, building and caching any not seen yet."""
        known = np.isin(keys, self._plan_keys)
        if not known.all():
            missing = keys[~known]
            channel = (np.arange(_SNAP_LEVELS) * 255.0 / (_SNAP_LEVELS - 1)).astype(
                np.float32
            )
            want = np.stack(
                [
                    channel[(missing >> (2 * _SNAP_BITS)) & (_SNAP_LEVELS - 1)],
                    channel[(missing >> _SNAP_BITS) & (_SNAP_LEVELS - 1)],
                    channel[missing & (_SNAP_LEVELS - 1)],
                ],
                axis=1,
            )
            plans = _build_plans(want, self.palette, self._nplan, self.strength)
            if self._max_plan_colors < self._nplan:
                plans = _limit_plans(plans, want, self.palette, self._max_plan_colors)

            merged = np.concatenate([self._plan_keys, missing])
            order = np.argsort(merged, kind="stable")
            self._plan_keys = merged[order]
            self._plans = np.concatenate([self._plans, plans])[order]

        return self._plans[np.searchsorted(self._plan_keys, keys)]
