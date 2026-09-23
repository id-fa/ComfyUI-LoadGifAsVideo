"""Color quantization for SaveAsGif — turning a frame batch into a GIF palette.

These are clean-room implementations written from the published descriptions of
the algorithms, not translations of anyone's source:

- `median_cut_aforge` follows Heckbert's median cut as AForge.NET's
  `MedianCutQuantizer` exposes it — round-robin slot selection, longest raw RGB
  side, split at the median pixel, mean color per cube.
- `median_cut_gifsicle` follows the ppmquant-derived variant gifsicle uses for
  `--color-method=median-cut` — split the slot holding the most pixels, pick the
  axis by luminance-weighted extent, and nudge the split point to even the halves.
- `diversity` / `blend_diversity` follow XV's modified diversity method
  (Bradley/Lane), which is gifsicle's default `--color-method`.

Everything works in plain 8-bit sRGB with a squared Euclidean distance, which is
what AForge does. gifsicle instead measures distance in a 15-bit gamma-corrected
space; keeping this package in sRGB puts every quantizer and every dither here on
one comparable footing, and the color ordering that comes out is very close.
"""

import numpy as np

MAX_COLORS = 256

# Levels per channel the uniform grid stops at: 6^3 = 216 fits a GIF palette,
# 7^3 = 343 does not.
MAX_UNIFORM_LEVELS = 6

QUANTIZERS = [
    "median_cut_aforge",
    "median_cut_gifsicle",
    "diversity",
    "blend_diversity",
]

# gifsicle's kc_luminance weights: Rec.709 primaries as the integer proportions
# 55/183/19 over 256. Used to order dither plans and to weight median-cut axes.
_LUMA = np.array([55.0, 183.0, 19.0]) / 256.0

# A full histogram of every pixel of a long animation is mostly redundant and can
# cost hundreds of MB to sort. Above this many pixels the frames are strided
# instead; the palette that comes out is indistinguishable.
_MAX_HISTOGRAM_SAMPLES = 4_000_000

# Histogram size the diversity choosers are run at. See `_coarsen` for why they,
# unlike median cut, cannot be handed a true-color histogram as it stands.
_MAX_DIVERSITY_COLORS = 65536


def uniform_palette(colors):
    """An evenly spaced RGB grid, and the number of levels per channel.

    The channel-independent dithers (ImageMagick's `-ordered-dither` family) never
    search a palette: they round each channel on its own, so the only colors they
    can produce are the points of a grid. Handing them anything else would be a
    lie about what comes out. `colors` is rounded *down* to the largest grid that
    fits, so 256 gives 6 levels (216 colors) and 8 gives 2 (the eight corners of
    the RGB cube, which is where the poster look comes from).
    """
    levels = 2
    while (levels + 1) ** 3 <= colors and levels < MAX_UNIFORM_LEVELS:
        levels += 1
    steps = np.rint(np.arange(levels) * 255.0 / (levels - 1)).astype(np.uint8)
    # Index order r*levels^2 + g*levels + b, which is what the dither computes.
    grid = np.stack(np.meshgrid(steps, steps, steps, indexing="ij"), axis=-1)
    return grid.reshape(-1, 3), levels


def luminance(colors):
    """Rec.709 luminance of 8-bit RGB colors, 0..255."""
    return np.asarray(colors, dtype=np.float64) @ _LUMA


def nearest_index(colors, palette, palette_sq=None):
    """Index of the closest palette entry to each color, by squared RGB distance.

    Expanded as |c|^2 - 2 c.p + |p|^2 and reduced to one matrix product, because
    the error-diffusion loop calls this once per wavefront.
    """
    colors = np.asarray(colors, dtype=np.float32)
    palette = np.asarray(palette, dtype=np.float32)
    if palette_sq is None:
        palette_sq = (palette * palette).sum(1)
    # |c|^2 is constant across the row, so it does not affect the argmin.
    return np.argmin(palette_sq - 2.0 * (colors @ palette.T), axis=1)


def color_histogram(frames):
    """Unique colors and pixel counts over `frames`, a uint8 [N, H, W, 3] array."""
    flat = np.asarray(frames).reshape(-1, 3)
    if len(flat) > _MAX_HISTOGRAM_SAMPLES:
        step = -(-len(flat) // _MAX_HISTOGRAM_SAMPLES)
        flat = flat[::step]

    packed = (
        (flat[:, 0].astype(np.uint32) << 16)
        | (flat[:, 1].astype(np.uint32) << 8)
        | flat[:, 2].astype(np.uint32)
    )
    keys, counts = np.unique(packed, return_counts=True)
    colors = np.stack(
        [(keys >> 16) & 0xFF, (keys >> 8) & 0xFF, keys & 0xFF], axis=1
    ).astype(np.int64)
    return colors, counts.astype(np.int64)


def _coarsen(colors, counts, limit):
    """Merge the histogram onto a coarser RGB lattice until it fits in `limit` bins.

    The diversity choosers are quadratic in the histogram: they score every
    candidate against every color already chosen, and against the midpoints of
    pairs of chosen colors. gifsicle only ever runs them on a GIF, so its
    histogram is at most 256 entries; a true-color frame batch hands them
    millions, which turns a fraction of a second into minutes. Binning to a
    coarser lattice — and keeping each bin's pixel-weighted mean as its color, so
    the bin still sits where its pixels are — leaves the palette unchanged in
    practice.
    """
    if len(colors) <= limit:
        return colors, counts

    for bits in (7, 6, 5, 4):
        shift = 8 - bits
        binned = colors >> shift
        keys = (binned[:, 0] << (2 * bits)) | (binned[:, 1] << bits) | binned[:, 2]
        unique, inverse = np.unique(keys, return_inverse=True)
        if len(unique) <= limit or bits == 4:
            merged_counts = np.bincount(
                inverse, weights=counts, minlength=len(unique)
            ).astype(np.int64)
            merged_colors = np.stack(
                [
                    np.bincount(
                        inverse, weights=colors[:, k] * counts, minlength=len(unique)
                    )
                    for k in range(3)
                ],
                axis=1,
            )
            merged_colors = (
                merged_colors / np.maximum(merged_counts, 1)[:, None]
            ).astype(np.int64)
            return merged_colors, merged_counts

    return colors, counts


def _mean_color(colors, counts):
    """Pixel-weighted mean of a cube, truncated — both references truncate."""
    total = int(counts.sum())
    if total <= 0:
        return colors[0].astype(np.int64)
    return (colors * counts[:, None]).sum(0) // total


def _median_cut_aforge(colors, counts, size):
    """AForge's MedianCutQuantizer.

    AForge feeds the quantizer every pixel of the image as a flat list, so its
    "split at element `Count / 2`" is a split at the median *pixel*; the unique
    colors and counts here express the same list without the duplicates. Its
    round-robin `cubeIndexToSplit` walk is reproduced as-is: which cube gets split
    next is part of the result, not an implementation detail.
    """
    cubes = [(colors, counts)]
    index = 0  # AForge starts at cubes.Count - 1, which is 0 for the single cube

    # AForge spins forever on a cube it cannot split; bail out after a full lap
    # that produced nothing instead.
    stalled = 0
    while len(cubes) < size and stalled <= len(cubes):
        cube_colors, cube_counts = cubes[index]
        if len(cube_colors) < 2:
            index = index - 1 if index > 0 else len(cubes) - 1
            stalled += 1
            continue
        stalled = 0

        # Longest raw RGB side, ties resolved R, then G — AForge's comparison order.
        extent = cube_colors.max(0) - cube_colors.min(0)
        axis = int(np.argmax(extent))
        order = np.argsort(cube_colors[:, axis], kind="stable")
        cube_colors, cube_counts = cube_colors[order], cube_counts[order]

        cumulative = np.cumsum(cube_counts)
        half = int(cumulative[-1]) // 2
        split = int(np.searchsorted(cumulative, half, side="left")) + 1
        split = min(max(split, 1), len(cube_colors) - 1)

        first = (cube_colors[:split], cube_counts[:split])
        second = (cube_colors[split:], cube_counts[split:])
        # RemoveAt(i) then Insert(i, cube1), Insert(i, cube2) leaves cube2 first.
        cubes[index : index + 1] = [second, first]

        index = index - 1 if index > 0 else len(cubes) - 1

    return np.stack([_mean_color(c, n) for c, n in cubes])


def _median_cut_gifsicle(colors, counts, size):
    """The ppmquant-derived median cut gifsicle uses for --color-method=median-cut.

    Slots are index ranges into one array that gets sorted in place per split,
    which is how gifsicle holds them, and it matters: the sort order of an earlier
    split is what a later split of the same region starts from.
    """
    colors = colors.copy()
    counts = counts.copy()
    slots = [[0, len(colors), int(counts.sum())]]

    while len(slots) < size:
        # Split whichever slot covers the most pixels and still holds >= 2 colors.
        split = None
        most = 0
        for slot in slots:
            if slot[1] >= 2 and slot[2] > most:
                split, most = slot, slot[2]
        if split is None:
            break

        first, count, pixels = split
        region = slice(first, first + count)
        block = colors[region]

        # Axis by luminance-weighted extent, so a wide green spread outranks an
        # equally wide blue one.
        extent = (block.max(0) - block.min(0)) * np.array([0.299, 0.587, 0.114])
        if extent[0] >= extent[1] and extent[0] >= extent[2]:
            axis = 0
        elif extent[1] >= extent[2]:
            axis = 1
        else:
            axis = 2
        order = np.argsort(block[:, axis], kind="stable")
        colors[region] = block[order]
        counts[region] = counts[region][order]

        # Split at the median pixel. gifsicle's loop stops one short of the end so
        # neither half comes out empty; searchsorted finds the same index.
        block_counts = counts[region]
        cumulative = np.cumsum(block_counts)
        half = pixels // 2
        at = min(int(np.searchsorted(cumulative, half, side="left")) + 1, count - 1)
        at = max(at, 1)
        accumulated = int(cumulative[at - 1])

        # The half before the split always has at least half the pixels, sometimes
        # by a wide margin; stepping back one color can even them out. gifsicle
        # compares these as uint32, so a negative diff2 wraps and loses — keep that,
        # since it is what decides the split on lopsided slots.
        diff1 = (2 * accumulated - pixels) & 0xFFFFFFFF
        diff2 = (pixels - 2 * (accumulated - int(block_counts[at - 1]))) & 0xFFFFFFFF
        if diff2 < diff1 and at > 1:
            at -= 1
            accumulated -= int(block_counts[at])

        slots.append([first + at, count - at, pixels - accumulated])
        split[1] = at
        split[2] = accumulated

    return np.stack(
        [
            _mean_color(colors[s[0] : s[0] + s[1]], counts[s[0] : s[0] + s[1]])
            for s in slots
        ]
    )


def _diversity(colors, counts, size, blend, dodither):
    """XV's modified diversity method, gifsicle's default color chooser.

    Alternates between "take the most popular color left" and "take the color
    furthest from everything already chosen". When dithering is on, a candidate
    also scores for how well it would let *pairs* of chosen colors dither into
    colors that are still unrepresented — which is why the palette a dithered GIF
    wants is not the palette an undithered one wants.
    """
    order = np.argsort(-counts, kind="stable")
    colors = colors[order]
    counts = counts[order]
    n = len(colors)
    size = min(size, n)

    colors_f = colors.astype(np.float64)
    colors_sq = (colors_f * colors_f).sum(1)
    lum = luminance(colors)

    min_dist = np.full(n, np.inf)
    min_dither = np.full(n, np.inf)
    closest = np.zeros(n, dtype=np.int64)
    chosen = []

    for step in range(size):
        live = min_dist > 0
        if not live.any():
            break

        if step == 0 or (step >= 10 and step % 2 == 0):
            # Most popular color not yet represented.
            pick = int(np.flatnonzero(live)[0])
        elif not dodither:
            pick = int(np.argmax(np.where(live, min_dist, -np.inf)))
        else:
            # The weight on dithered stand-ins decays as the palette fills up.
            weight = 0.05 + 0.25 ** (1.0 + (step - 1) / 3.0)
            score = min_dist + weight * min_dither
            pick = int(np.argmax(np.where(live, score, -np.inf)))

        min_dist[pick] = 0.0
        min_dither[pick] = 0.0
        closest[pick] = pick

        dist = ((colors_f - colors_f[pick]) ** 2).sum(1)
        nearer = (min_dist > 0) & (dist < min_dist)
        min_dist[nearer] = dist[nearer]
        closest[nearer] = pick

        # gifsicle only tracks dither combinations for the first 64 colors; past
        # that the palette is dense enough that they stop telling it anything.
        if dodither and 0 < step < 64 and chosen:
            previous = np.array(chosen)
            # Penalize pairs whose luminance is far apart — they dither into a
            # visible checkerboard rather than into a color.
            delta_l = np.abs(lum[pick] - lum[previous])
            penalty = np.where(delta_l > 64.0, delta_l * 4.0 / 255.0, 1.0)
            mixes = ((colors[pick] + colors[previous]) // 2).astype(np.float64)
            # Squared distance from every mix to every histogram color, expanded
            # as |c|^2 - 2 c.m + |m|^2 so it is one matrix product rather than a
            # (mixes, histogram, 3) difference block. Every operand is an
            # integer well inside float64's exact range, so this is the same
            # number the direct subtraction gives, at a fraction of the cost —
            # this block is where a dithered per-frame palette spends its time.
            mixes_sq = (mixes * mixes).sum(1)
            spread = colors_sq[None, :] - 2.0 * (mixes @ colors_f.T) + mixes_sq[:, None]
            candidate = (spread * penalty[:, None]).min(0)
            np.minimum(min_dither, candidate, out=min_dither, where=min_dist > 0)

        chosen.append(pick)

    chosen = np.array(chosen, dtype=np.int64)
    if not blend or len(chosen) < 4:
        # Blending has bad effects when there are very few colors.
        return colors[chosen]

    # Replace each chosen color by the mean of everything that mapped to it, with
    # the chosen color itself weighted triple so the slot does not drift off it.
    weights = counts.astype(np.float64)
    weights = np.where(closest == np.arange(n), weights * 3.0, weights)
    slot_of = np.full(n, -1, dtype=np.int64)
    slot_of[chosen] = np.arange(len(chosen))
    slot = slot_of[closest]

    totals = np.bincount(slot, weights=weights, minlength=len(chosen))
    sums = np.stack(
        [
            np.bincount(slot, weights=colors_f[:, k] * weights, minlength=len(chosen))
            for k in range(3)
        ],
        axis=1,
    )

    blended = colors[chosen].copy()
    # Only blend a slot that actually collected a crowd; otherwise the chosen
    # color is already the right answer for it.
    worth_it = totals >= 5.0 * counts[chosen]
    blended[worth_it] = (sums[worth_it] / totals[worth_it, None]).astype(np.int64)
    return blended


def build_palette(frames, colors, quantizer, dithered):
    """Palette for `frames` (uint8 [N, H, W, 3]) as a uint8 [P, 3] array.

    `dithered` only reaches the diversity choosers, which pick differently when
    they know the result will be dithered.
    """
    if not 2 <= colors <= MAX_COLORS:
        raise ValueError(f"colors must be between 2 and {MAX_COLORS}, got {colors}")

    hist_colors, hist_counts = color_histogram(frames)
    if len(hist_colors) <= colors:
        # Fewer distinct colors than palette slots: nothing to quantize.
        return hist_colors.astype(np.uint8)

    if quantizer == "median_cut_aforge":
        palette = _median_cut_aforge(hist_colors, hist_counts, colors)
    elif quantizer == "median_cut_gifsicle":
        palette = _median_cut_gifsicle(hist_colors, hist_counts, colors)
    elif quantizer in ("diversity", "blend_diversity"):
        hist_colors, hist_counts = _coarsen(
            hist_colors, hist_counts, _MAX_DIVERSITY_COLORS
        )
        palette = _diversity(
            hist_colors, hist_counts, colors, quantizer == "blend_diversity", dithered
        )
    else:
        raise ValueError(f"Unknown quantizer: {quantizer}")

    palette = np.clip(palette, 0, 255).astype(np.uint8)
    # Two cubes can average to the same color. A duplicate entry buys nothing, and
    # Pillow's writer needs the palette to be a set to map a frame onto it without
    # renumbering, so drop repeats while keeping the order the quantizer chose.
    _, first = np.unique(palette, axis=0, return_index=True)
    return palette[np.sort(first)]
