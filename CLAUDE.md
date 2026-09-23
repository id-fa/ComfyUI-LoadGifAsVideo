# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A ComfyUI custom node package with three nodes. `README.md` is the user-facing spec.

- `LoadGifAsVideo` ("Load GIF as Video", `load_gif_as_video_node.py`) — reads an animated GIF/APNG/WEBP from the ComfyUI `input` directory and emits a `VIDEO`.
- `LoopVideo` ("Loop Video", `loop_video_node.py`) — takes any `VIDEO` in and loops it to the requested length. This is how short MP4s are handled: `Load Video` → `LoopVideo`.
- `SaveAsGif` ("Save as GIF", `save_as_gif_node.py`) — the other direction: quantizes and dithers a `VIDEO` or an `IMAGE` batch and writes an animated GIF. `gif_palette.py` holds the quantizers, `gif_dither.py` the dithers.

`video_length.py` holds what they share: the `length_mode`/`frames`/`seconds`/`loops`/`speed` widget block (`length_inputs`) and the count math (`scaled_frame_rate`, `resolve_frame_count`, `speed_baked_indices`). `__init__.py` merges all three modules' mappings. The one piece of frontend JS is `web/save_as_gif_preview.js`, which exists solely to stop the preview treating `SaveAsGif` as a video node — see *The preview payload*. No Python dependencies beyond what ComfyUI already ships (Pillow, NumPy, PyTorch).

**Why MP4 is a second node rather than more extensions in the file combo.** The decode problem (frame delays, disposal, alpha, variable timing) is entirely GIF-specific and shares no code with PyAV decoding; the loop/length/speed problem is entirely source-agnostic. Splitting along that seam also avoids reimplementing a worse `Load Video`, and lets `LoopVideo` loop *any* VIDEO, including generated ones. Do not add `.mp4`/`.webm` to `_ANIMATION_EXTENSIONS`.

## Design decisions

These were chosen deliberately; re-read before "improving" them.

- **Classic `INPUT_TYPES` style, not the V3 `io.Schema` API.** Matches the sibling ComfyUI-Lenient-Switch package and works on older ComfyUI builds. The V3 API in `comfy_extras/nodes_video.py` is *not* what this node uses.
- **`comfy_api` is imported under try/except** (`comfy_api.input_impl.VideoFromComponents`, `comfy_api.util.VideoComponents` — the backwards-compat shims, not the `comfy_api.latest._*` internals). Pyright flags them as unresolved, which is expected. On a ComfyUI without VIDEO support the node still registers and raises a readable error from `load`. The `if VideoFromComponents is None or VideoComponents is None` guard checks **both** names so Pyright narrows both — checking only one reintroduces `reportOptionalCall`.
- **`SaveAsGif`'s `info` output is a STRING, not an on-node label, because there is no public way to put one there.** The frontend does render `ui.text`, but only for one node: `src/extensions/core/previewAny.ts` guards on `nodeData.name !== 'PreviewAny'` and calls `addTextPreviewWidgets`, which lives in an internal module a custom extension cannot import. A DOM widget is not a way around it either — ComfyUI's own animated preview passes `canvasOnly: true`, i.e. the old canvas UI. So the size goes out as a STRING that the stock `PreviewAny` ("Preview as Text") renders. `ui.text` is emitted anyway, in the same shape `PreviewAny` returns, in case that ever generalizes; it costs nothing and the frontend ignores unknown `ui` keys.
- **Two outputs, `VIDEO` and `IMAGE`, in that order.** The frames tensor is already built before it is wrapped in `VideoFromComponents`, so re-emitting it as `images` is nearly free and spares the near-universal `Get Video Components` hop into a sampler. `images` is **appended**, never inserted, so old workflows keep their links. Still no MASK/fps/bit_depth outputs — alpha is already flattened onto `background`, and the rest is what `Get Video Components` is for.
- **Frame rate is always derived from the source; there is no fps widget.** `speed` multiplies that derived rate for the `VIDEO` output. Do not add an fps input; that was explicitly rejected.
- **`speed` is expressed as a frame rate in `VIDEO` and as frames in `IMAGE`.** See *Speed in the IMAGE output* below. The `VIDEO` path still never duplicates or drops a frame.
- **`SaveAsGif` takes `video` and `images` as two *optional* inputs, not one required one.** A VIDEO carries its own frame rate and an IMAGE batch does not, so `fps` only means anything for the latter; making both optional and rejecting the both-connected case in `save` keeps one node useful from either side of the package. The check is in `save`, not `VALIDATE_INPUTS`, because an unconnected optional input is not something `VALIDATE_INPUTS` sees.
- **`SaveAsGif` splits the frame rate into `source_fps` and `fps`, and `fps` drops frames rather than restretching time.** Lowering `fps` past the source rate writes fewer frames over the same running time, which is the most effective file-size control the node has and the reason the split exists: one number cannot be both "what the input is" and "what to write". A VIDEO supplies its own source rate, so `source_fps` is only read for an `images` batch. Raising `fps` above the source rate deliberately does nothing — duplicating frames costs bytes and shows nothing new. Do not restore the old behavior where `fps` only restretched the delays.
- **`SaveAsGif` never stretches; `size_mode` picks between letterboxing and shrinking the box.** `width`/`height` are a box the frame is scaled into keeping its aspect ratio. `pad` (default, the original behavior) treats the box as the canvas, centres the frame and writes the leftover as GIF transparency; `fit` writes the scaled frame at its own size, so the box is only an upper bound and nothing is padded. Both live in `_fit`, which returns the same tuple either way — under `fit` the canvas simply equals the scaled size, so `letterboxed` is False and the whole transparent path switches off with no extra branches in `save`. Either axis at 0 derives from the other, both at 0 leaves the source alone, so the widgets default to a no-op; with fewer than two axes given the two modes are identical. `size_mode` is a keyword with a default in `save` so prompts queued before the widget existed still run. Do not add a "stretch to fill" mode without being asked — silently changing an animation's aspect ratio is the thing this design is avoiding.
- **The `-poster` dithers invert the node's palette/dither split, and `SaveAsGif` special-cases them for it.** Every other dither takes a palette and searches it; `halftone-poster` / `halftone-square-poster` are ImageMagick's `-ordered-dither`, which rounds each channel independently and therefore *dictates* its palette — an even RGB grid from `gif_palette.uniform_palette`. So `save()` builds that grid instead of calling `build_palette`, and `quantizer` / `palette_scope` become no-ops. `Ditherer` re-derives the level count from the palette length and raises if it is not a cube, which keeps the two halves honest. This is the only place where the dither chooses the palette; do not generalize it without need.
- **Everything in the GIF path measures color in plain 8-bit sRGB.** AForge does; gifsicle works in a 15-bit gamma-corrected space instead. One space throughout is what lets any quantizer be swapped for any other and any dither for any other and still have the comparison mean something. Do not add a gamma option to one of them alone.
- **The algorithms are clean-room, and the README says so.** AForge.NET is LGPLv3 and gifsicle is GPLv2; this package is MIT and depends on neither. Both references are themselves re-implementations of published work (gifsicle's own comments credit ppmquant, XV and Joel Yliluoma), so the published descriptions are what these follow. **Do not paste code or literal data tables from either project** — that is why `ro64` is regenerated rather than copied, and why the halftone matrices go through gifsicle's *generator* rather than its output.
- **`LoopVideo` drops audio whenever `speed != 1.0`.** Since only the frame rate changes, kept audio would drift out of sync; shipping no audio beats shipping wrong audio. Retiming the waveform was considered and rejected as out of scope.

### Speed in the IMAGE output

An IMAGE batch has no frame rate, so the `VIDEO` output's trick of expressing `speed` as one is lost the moment the batch reaches `Create Video` or a Video Combine node with its own fps widget. `speed_baked_indices(count, source_length, speed)` therefore re-expresses the same speed change as frames for the `images` output only: `image_count = round(count / speed)` frames, index `floor(i * speed) % source_length`. At the source's own rate that batch has the same duration and the same motion as the `VIDEO` output.

- **The two outputs therefore have different frame counts whenever `speed != 1.0`** — that is the deliberate trade, chosen over the alternatives of holding the count fixed (speed then only half-applies) or baking speed into `VIDEO` too (fps scaling is strictly higher quality there). `speed < 1` duplicates frames into `images`, `speed > 1` drops them.
- **At `speed == 1.0` the node returns the *same tensor object* for both outputs.** `round(count / 1) == count` and `floor(i * 1) == i`, so the indices are identical; the explicit `if float(speed) == 1.0` branch just avoids materialising a second copy of what can be a 10000-frame batch. Do not remove it thinking it is redundant — it is a memory guard, not a correctness one.
- **The indices are computed with integer math on `Fraction(speed).limit_denominator(1000)`**, matching `scaled_frame_rate`'s treatment of `speed`. Plain `floor(i * float(speed))` lands a hair under an integer boundary for values like `0.1` and holds a frame one step too long.
- **`MAX_OUTPUT_FRAMES` is re-checked against `image_count`**, since a low `speed` can push the batch over the cap while `VIDEO` stays well under it.

## Architecture notes

### LoadGifAsVideo

The pipeline is `_read_animation` → `_to_uniform_timebase` → modulo-index in `load`.

- **`_read_animation`** walks `ImageSequence.Iterator` and `convert("RGBA")` on each frame. Pillow handles GIF/APNG disposal and blending internally during `seek`, and composites partial frames onto the full canvas — do not try to reimplement disposal. Alpha is composited over `background` (VIDEO has no alpha channel) and the result is float32 `[H, W, 3]` in 0..1, which is what `VideoComponents.images` expects. A frame-size consistency check raises rather than letting `np.stack` fail cryptically.
- **Delay clamping.** `_MIN_DELAY_MS = 20` / `_DEFAULT_DELAY_MS = 100`: GIFs that declare 0ms or 10ms mean "as fast as possible" and every browser renders them at 100ms. Matching that is intentional, not a rounding bug.
- **`_to_uniform_timebase`** returns `(frames, fps)`. When all delays are equal it returns the frames **untouched** — that is the common case and the reason the output is bit-exact. Only variable-delay animations are nearest-hold resampled, sampling at each output frame's *midpoint* (`(i + 0.5) * step_ms`) against the cumulative delay ends. The resample deliberately keeps the frame count equal to the source count, so the total duration is preserved. There is no blending — nearest frame only.
- **Looping is `np.arange(count) % len(source_frames)`** applied to the stacked array. This covers looping, truncation and the single-frame (static image) case in one expression; don't special-case them. Every `length_mode` therefore only has to compute `count`. `speed_baked_indices` is the same expression with `speed` folded into the step, and the modulo applied *after* accumulating the step so repeats stay aligned to frame 0.
- **`seconds` mode multiplies by the *speed-scaled* fps**, so 2s at `speed=2` yields twice the frames of 2s at `speed=1` — more of the animation in the same wall-clock time. That is the intended semantics of "faster"; it is not a double-application of `speed`.
- **`loops` mode is `loops * len(source_frames)`** — deliberately *not* speed-scaled, so the output is always a whole number of passes and `speed` only changes the duration. Note it uses the length **after** `_to_uniform_timebase`, which for variable-delay sources equals the original frame count anyway; that equality is why the resample preserves the count.
- **`_MAX_OUTPUT_FRAMES = 10000`** caps the `frames`/`loops` widgets and the final computed count for every mode. Without it, `seconds=3600, speed=100` silently tries to allocate hundreds of GB.

### LoopVideo

`loop()` is `get_components()` → `scaled_frame_rate` → `resolve_frame_count` → `images[torch.arange(count) % source_length]` → rebuild a `VideoFromComponents`, then the separate `speed_baked_indices` gather for the `images` output. The frame path is deliberately the same shape as the GIF node's.

`_loop_audio` is the only non-obvious part. The source audio is first **fitted to exactly one video loop** — `period = round(source_length * sample_rate / fps)` samples, truncating or zero-padding — and only then tiled. Tiling the raw waveform by *its own* length instead would drift against the video loops whenever the container's audio and video tracks are not exactly the same duration (common in real MP4s), so every repeat would start a little further off frame 0. Do not "simplify" it to a plain `repeat` of the source waveform.

`_loop_audio` returns `None` (rather than raising) for a missing/empty waveform or a nonsense sample rate, so a video with a degenerate audio track still loops.

### SaveAsGif

`save()` is: resolve the frames and frame rate from whichever input is connected → build a palette → dither each frame to indices → wrap each as a P-mode `Image` → one Pillow `save(save_all=True)`. The dithered RGB also comes back as the `images` output, which is free since `palette[indices]` is one gather.

- **Error diffusion is vectorized by wavefront, and this is the single least obvious thing in the package.** Error diffusion is defined sequentially in raster order, but a pixel only ever writes to neighbors at the kernel's offsets. `_wavefront_slope` picks `a` so that every offset strictly increases `a*y + x`; pixels sharing one value of `a*y + x` therefore cannot reach each other and can be quantized in one NumPy call. The result is **exactly** the raster-order result, not an approximation — `scratchpad/test_core.py` checks that against a plain nested-loop reference for every kernel, and it passes for all eight. A 512×512 frame costs ~0.1–0.3 s instead of the ~1 s a Python pixel loop would. Do not "simplify" this back into a per-pixel loop.
- **The work buffer is padded by the kernel's reach** (`pad_x = max|dx|`, `pad_y = max dy`) so error falling off an edge lands in the margin. That is what removes the bounds mask from the inner scatter; without the padding every offset would need a per-wavefront boolean mask.
- **Ordered dithering uses gifsicle's plan approach (Yliluoma), not threshold perturbation.** For each source color it builds `nplan` palette entries whose mixture approximates it, sorted by luminance, and the matrix cell selects which entry a pixel takes. This is what makes ordered dithering survive a 32-color palette. Threshold perturbation — which is what AForge's `OrderedColorDithering` does — falls apart there, so it is deliberately not the implementation here even though AForge is the primary reference elsewhere.
- **Plans are built on a 5-bit-per-channel source lattice** (`_SNAP_BITS`), capping the cache at 32768 rows. gifsicle can afford exact plans because its input is an indexed GIF with ≤256 colors; a true-color frame has up to 16M. The snap costs at most ~4/255, well under the palette spacing. The cache lives on the `Ditherer`, so a global palette pays for its plans once across the whole animation rather than once per frame (measured: 0.33 s for the first frame, 0.07 s for each one after).
- **`halftone_size` / `halftone_ink` are the only ordered-dither parameters exposed, and both go through `_ordered_matrix`.** `size` is the dot pitch on every lattice: the triangular screen is `size` × `round(size*sqrt(3))`, which is what puts the dots on a hexagonal lattice; the square one is `size` × `size`; the diamond one is a `round(size*sqrt(2))` square tile with the same centre-plus-corners construction as the triangular screen, which is a square lattice turned 45° with its diagonal pitch back at `size`. The diamond exists because the hexagonal screen's rows of dots run horizontally and, once the dots are large (a mask screen at full strength), nearly touch and read as horizontal stripes; the 45° lattice's rows run diagonally and stay a mesh. The brick one is a `size` × `2*size` tile with the same construction: rows a full pitch apart, alternate rows shifted by half, so every dot sits under the gap in the row above — the hexagonal screen's staggering with the rows opened up to the pitch. `_HALFTONE_SCREENS` keys the lattice by name (`hex` / `square` / `diamond` / `brick`), and every family gets all four. `ink` is implemented as `matrix = nplan - 1 - matrix`: plans run dark to light and the matrix runs outward from the dot center, so reversing the matrix is exactly "grow the light color instead of the dark one". It moves the dots without changing the duty cycle, so average tone is preserved — verified in the tests, and a useful invariant to keep.
- **File size is not monotonic in `halftone_size` below the plan cap, and that is expected.** A larger cell buys a longer tonal ladder as well as a coarser dot, and for the uncapped `-ordered` screens the extra tone can add back more than the coarser dot removes (measured: size 6 → 10 grows the file ~1%). Past the 255-entry cap the ladder is fixed and only the dot size is left to move, so the curve is monotonic from `size` 16 up. Assert monotonicity only over the saturated range, plus smallest-vs-largest overall.
- **`_MAX_PLAN_LENGTH = 255` caps the tonal ladder, matching gifsicle.** Past 255 cells the screen shares plan entries between cells rather than lengthening the plan, so a 64-pixel cell costs no more to build plans for than a 16-pixel one. Removing the cap would make `halftone_size = 64` build 7104-entry plans — hundreds of times slower for tone nobody can see at that dot pitch.
- **The plan is looked up per *pixel*, not per cell, and that is deliberate.** A "true" halftone screen resolves one tone per cell; doing that here (averaging each cell before the lookup) shrinks the output far more — 10x fewer horizontal runs at `size` 40 versus 1.3x — but it destroys the image, because cell averaging is just a resolution drop: at `size` 24+ a circle comes out as a rectangle and fine texture disappears entirely. The per-pixel lookup keeps edges and detail sharp while the dots coarsen, which is both gifsicle's behavior and the better picture. **Do not "fix" the modest size-vs-filesize curve by averaging cells.** If a measurement seems to show cell size doing nothing, check the test material first: noise-over-gradient has no flat area anywhere and hides the effect completely (1.1x), while material with real flat areas shows it (2.7x smaller than `floyd-steinberg` at `size` 40).
- **`_limit_plans` is what separates the halftone *screens* from the halftone *ordered* dithers, and `_HALFTONE_SCREENS` is the whole difference.** `halftone` / `halftone-square` declare `nc = 2`, so a cell may only alternate ink and ground; `halftone-ordered` / `halftone-square-ordered` declare `nc = nplan`, which skips the limiting step entirely and lets the plan keep whatever colors approximate the source best. Same matrix, same `halftone_size` and `halftone_ink`, different tone — the screens read as printed ink, the ordered ones as a dither on a halftone lattice. The uncapped pair is also the faster of the two, since `_limit_plans` returns immediately. Only the 1-color and 2-color limiting cases are implemented, because 2 is the only `nc` below `nplan` any matrix here declares — gifsicle supports 3 as well, and that path (its `kc_plane_closest`) is deliberately absent.
- **`atkinson` uses the published kernel, not gifsicle's.** gifsicle's `colormap_image_atkinson` adds `err[1][x+1]` twice and never touches the lower-left neighbor. That looks like a slip rather than a choice, so it is not reproduced.
- **The diffused error is kept in float32 and never clamped between pixels**; only the value read for the nearest-color lookup is clipped to 0..255. AForge writes error back into a byte buffer, so it saturates. Keeping it is both simpler and better.

#### Palette construction

- **`_coarsen` exists because the diversity choosers are quadratic in the histogram.** Measured: 2.6M unique colors from a 48×512×512 batch took 155 s; binning to ≤65536 lattice cells brings it to 2.4 s with no visible palette change. Median cut is linear-ish and is handed the full histogram unchanged. Do not apply `_coarsen` to median cut, and do not remove it from diversity.
- **The dithered diversity chooser scores mix candidates with one matrix product**, expanding the squared distance as `|c|² - 2c·m + |m|²` instead of forming a `(mixes, histogram, 3)` difference block. Every operand is an integer, so the result is bit-identical to the direct subtraction (checked palette-for-palette against the old code), and a dithered palette for a 512×512 frame goes 1.45 s → 0.65 s. That per-frame cost is what made `palette_scope = per_frame` with `diversity` look hung on long clips; the remaining time is the `(mixes, histogram)` temporaries and not worth chasing further without changing results.
- **`build_palette` de-duplicates the palette before returning it.** Two cubes can average to the same color, and a repeat is both wasted and actively harmful — see the Pillow note below.

#### The Pillow writer

Three details here are load-bearing and easy to undo by accident:

- **`palette=` is passed to `save()` for a global palette.** Pillow's `_write_multiple_frames` does `if not palette: encoderinfo["include_color_table"] = True` — so without it, *every frame after the first* is written with its own copy of the color table (768 bytes each at 256 colors, ~38% of a short GIF). Passing it drops the local tables entirely. `per_frame` must **not** pass it, since each frame's table really is different.
- **Passing `palette=` makes Pillow run `_normalize_palette`, which remaps through `im.palette.colors` — a color→index dict.** A duplicate palette entry silently collapses in that dict and sends the remap somewhere else. That is the real reason `build_palette` de-duplicates.
- **The palette is written at its true length, not padded to 256.** `putpalette` with 3·n bytes gives a GIF color table of the next power of two ≥ n, so a 16-color GIF pays for 16 entries.
- **`optimize=False` unless `pillow_optimize` is on**, because Pillow's optimize rebuilds the palette from the colors a frame actually uses, undoing a deliberately shared global one. The toggle exists because the user asked for it, with that caveat in its tooltip; it is never the default. When on, Pillow also does its own unchanged-pixels-to-transparent pass (only under `optimize`, see `_write_multiple_frames`), which agrees with `frame_diff`'s, and it may drop the transparent index from a frame that does not use it, which the tests allow for.

`scratchpad`-style verification for this lives in the test scripts described under *Commands*; the round trip (file read back == the `images` output) is exact for every scope × dither combination.

#### The mask screens

`_mask` is the third algorithm sharing the screen matrix, and it exists because of a distinction that is easy to miss: **`nc = 2` limits a *plan* to two colors, not a *cell*.** Pixels under one dot have different source colors, so they look up different plans and a dot ends up multi-colored no matter how tight the plan limit is. The only way to get a flat dot is to stop deriving the covered pixel's color from that pixel — which is what `_mask` does, painting every covered pixel one fixed ink and leaving the rest as the plain nearest color.

- **The ink is a real black or white, reserved in the palette by `save()`.** `_with_ink` appends it when the adaptive palette lacks it, and `palette_size` drops by one to pay for it. Nearest-match to a photographic palette would land on a dark blue and read as a tint, not as ink.
- **The screen does not depend on the frame at all.** `_screen(height, width)` is a function of the frame size and nothing else, cached on the `Ditherer`. This is deliberate and was a fix: an earlier version set coverage from each frame's own luminance, which is what a halftone normally does, and the dot rims then flickered wherever the picture moved — measured at 12246 dot pixels changing across a 12-frame clip, against zero now. **Do not reintroduce tone-following coverage here**; a screen that tracks tone is what the other three families already are.
- **Tone lives in the ground, not in the dot.** Since dot size is constant, the picture comes through the gaps. That is also why `halftone_steps` does nothing for these — there is no tonal ladder to quantize.
- **`dither_strength` is the coverage**, mapped so full strength is `_MASK_FULL_COVERAGE = 0.5`. Half is where a dot screen carries the most (dot and gap equal); mapping 1.0 to a solid fill would make the default setting paint the frame over.
- **The brick mask reaches that coverage by spacing, not by dot size, and does not use the matrix at all.** The user asked for it this way: lowering the strength should keep the dot and widen the gaps. `_brick_screen` stamps one fixed dot — the `round(0.5 * size²)` pixels nearest a pixel corner, ranked by distance then angle exactly as `_halftone` ranks cells, i.e. half a cell at full strength — onto a running-bond lattice whose pitch is `size / sqrt(strength)`, which holds `coverage ≈ strength / 2` like the other masks. The dot is a pixel *count*, not a disc of the right area: a 0.8-pixel-radius disc rasterizes to 4 pixels and at `size` 2 that filled the frame solid. **The pitch is rounded to whole pixels and the alternate-row shift to `pitch // 2`**, so the screen is exactly periodic: every dot the same pixel shape, every gap the same width. Two earlier versions were rejected for looking uneven — the exact lattice, where a 3-pixel-radius disc rasterizes to 32 or 37 pixels depending on where its centre falls within a pixel, and per-dot snapping of centres to pixel corners, where the gaps came out 2 and 3 pixels by turns (and `np.round`'s half-to-even made a `.5` shift alternate along a row). At a 3-pixel dot that jitter *is* the picture. The screen is built as one `2*pitch` × `pitch` tile (two dots, stamped modulo the tile) and indexed out like the matrix screens, so periodicity is by construction. The cost is that strength moves in steps at small sizes (size 2: pitches 2, 3, 4… at strengths 1, 0.44, 0.25), which is documented; at `size` 2 the 2-pixel dot at pitch 2 is a row of dominoes touching end to end, i.e. stripes, which is what a 2-pixel screen is. `_matrix` is still built for the brick mask (`_ordered_matrix` does not know it will go unused) and that is fine — it is cheap and keeps the constructor uniform.
- **`_ordered_matrix` does not flip the matrix for these.** Everywhere else `halftone_ink = white` reverses the screen; here the ink is named directly, and with a fixed screen a flip would only move the lattice, not change the tone.
- The ground keeps the source's colors, so a mask screen masks the picture where a poster dither replaces it. That is the difference worth keeping in mind when picking between them.
- **The `-mask-inverted` methods are a second *kind* in `_HALFTONE_SCREENS` (`"mask-inverted"`), not a toggle.** They were added as methods rather than a widget because every mask-specific branch in the package (the reserved ink entry, `MASK_DITHERS`, the `halftone_ink` polarity skip in `_ordered_matrix`) is keyed on the table's kind, so a new kind reaches all of them through `_MASK_KINDS` without threading a parameter through `Ditherer` and the three `Ditherer(...)` call sites in `save()`. The whole difference is one line at the end of `_screen`: `covered = ~covered`. Everything else — ink index, ground lookup, lattice, cache — is shared, which is what makes the two variants exact pixel-for-pixel complements (asserted in the tests). `dither_strength` therefore reads as the *picture's* share for the inverted kind, and the `strength <= 0` shortcut in `__call__` still wins, so strength 0 removes the screen instead of painting the frame solid ink; that discontinuity (0.05 is nearly all ink, 0 is none) is accepted, because an all-ink frame is never a useful output.

#### The poster (channel-independent) dithers

`_poster` is a different algorithm from `_ordered`, sharing only the screen matrix. It scales each channel onto `levels - 1`, compares the leftover fraction against the screen, and computes the palette index arithmetically (`r*L² + g*L + b`) — no distance search anywhere, which is why it runs in ~0.3 ms against ~20 ms for the plan-based screens.

- **The threshold is `(cell + 1) / (nplan + 1)`.** ImageMagick's maps run 1..divisor-1 over a divisor one larger than the cell count, so no cell ever thresholds at exactly 0 or 1 — a cell that thresholded at 0 would never round up and would be dead.
- **`dither_strength` scales the screen around 0.5**, so strength 0 becomes a plain round-to-nearest. That happens to agree exactly with the `strength <= 0` shortcut at the top of `__call__`, because rounding each channel to a grid *is* the nearest color when the palette is the grid's full product. The tests assert that agreement; if the grid ever stops being a full product, it breaks.
- **`halftone_size` runs the file size the *other* way for these, and the shape is a hump, not a slope.** The cell size also fixes the threshold step count (steps = cells, capped at 255), and more steps means finer tone means a busier pattern. Measured at 8 colors on a 192×192 12-frame clip: size 2 → 10.1 KB, 4 → 20.3, 6 → 26.0, 8 → **28.7** (peak), 10 → 28.2, 16 → 25.0, 24 → 22.5, 40 → 18.8. Only past 16, where the step count pins at 255, does a coarser dot start paying for itself. Do not "fix" this by assuming the halftone-screen rule (bigger is smaller) applies here.
- **`halftone_steps` is what decouples dot size from tone, and it is the answer to "a coarser screen did not shrink the file".** `_halftone` takes a step limit and requantizes the cell ranking to it, so a large cell can carry a small tonal ladder. Measured on `halftone-square-poster` at 8 colors: size 8 goes 31.3 → 19.6 KB at 2 steps, size 40 goes 20.7 → 14.9 KB. `0`, and anything at or above the cell count, both mean "one step per cell", so the default is a no-op and the old behavior is preserved exactly — the tests assert `steps=255` equals `steps=0`.
- **The step count is *not* monotonic in file size for the plan-based screens.** At 2 steps a plan holds two entries and simply alternates, which can cost more than a 4-step plan's longer runs (measured: `halftone` at size 8, 49.1 KB at 2 steps vs 48.5 KB at 4). Only the poster screens fall monotonically. Assert monotonicity for `POSTER_DITHERS` only; for the rest, assert only that 2 steps beats `auto`.
- **`uniform_palette` rounds `colors` down to a cube by integer search**, not `colors ** (1/3)` — the float cube root of 27 is 3.0000000000000004 on some builds and 2.9999999999999996 on others, and `int()` of the second is 2.

#### Output size and the transparent slot

`_fit` returns `(scaled_w, scaled_h, canvas_w, canvas_h, offset_x, offset_y)` and the rest follows from whether `(scaled_w, scaled_h) != (canvas_w, canvas_h)`. Four things about the transparent path are load-bearing:

- **The dither runs on the picture only, never on the padded canvas.** The bars are pasted into the index array *after* dithering. Dithering the padded canvas would let error diffusion bleed the bar color across the picture's edge.
- **The adaptive palette gives up one entry whenever the transparent slot is in use** — `letterboxed` or `frame_diff`, tracked as `transparent`. It is `colors - 1`, not `min(colors, 255)`: a 64-color request with a 65-entry table would be written as a 128-entry one, since GIF color tables come in powers of two. The poster grid keeps `min(colors, 255)`, because a cube plus one entry (217) rounds up to the same 256-entry table a cube alone does, while `uniform_palette(215)` would fall from 6³ to 5³.
- **`_spare_color` must return a color the palette does not already hold.** Pillow's `_normalize_palette` maps a frame onto the palette through a color-to-index dict, so a duplicate entry sends the remap somewhere else — the same reason `build_palette` de-duplicates. Black first (it is what a viewer ignoring transparency composites against anyway), then magenta, then a lattice scan that cannot fail for a palette of ≤255 colors.
- **Transparency goes at index 0, prepended, not appended.** A GIF carries one transparent index for the whole file, and `per_frame` palettes vary in length — appending would put it at a different index per frame. Index 0 is the same number regardless, which is why the dither's own indices get `+ 1`.

Scaling happens inside `_to_uint8` rather than as a second pass, so the full-resolution uint8 copy never exists; and `get_save_image_path` is given `canvas_w`/`canvas_h`, not the raw widget values, which can be 0.

#### Dropping frames

`_decimate` reuses `video_length.speed_baked_indices` rather than open-coding a stride: stepping `source_rate / output_rate` source frames per output frame *is* the gather `LoopVideo` already uses to bake `speed` into an IMAGE batch, rounding included. It runs before `_to_uint8`, so the palette is built from the frames that actually get written and every later stage costs proportionally less.

Two things about verifying this. First, the frame count in the written file can be *lower* than the batch: Pillow merges consecutive identical frames and sums their delays, which is correct GIF output but breaks a naive "file frames == `images` frames" assertion — test material has to keep every frame distinct. Second, the running time is the invariant worth asserting, not the frame count alone; `sum(delays)` should stay within a centisecond or two of `count / source_rate` at every output rate.

#### The preview payload

`save()` returns `{"ui": {"images": [...]}}` and **deliberately does not set `animated`**, even though the output is an animation. The stock `SaveAnimatedWEBP` sets it, but the frontend (checked against comfyui_frontend_package 1.49.6) reads it as a video marker:

```js
function isVideoOutput(e) {
  if (!isAnimatedOutput(e)) return false;                       // animated?.find(Boolean)
  let t = e?.images?.some(f => f.filename?.endsWith(".webp")),
      n = e?.images?.some(f => f.filename?.endsWith(".png"));
  return !t && !n;
}
// isVideoOutput(o) ? useNodeVideo(...) : useNodeImage(...)
```

`.webp` and `.png` are hardcoded exceptions; a `.gif` is on neither list, so setting `animated` routes the result to the video player, whose `getVideoFilename` does `new URL(path)`, throws, and renders **"Invalid URL"** instead of the image. `isAnimatedOutput` also short-circuits `isImageOutputs`, so the image path is skipped entirely. Without the flag the result takes the image path and animates by itself, because that is simply what an `<img>` does with a GIF. Do not add `animated` back by analogy with the WEBP node — the analogy is what breaks it.

**Dropping the flag is not enough on its own, because a VIDEO *input slot* also forces the video path.** Nodes 2.0 decides like this:

```ts
// renderer/extensions/vueNodes/components/LGraphNode.vue
const type =
  isVideoOutput(newOutputs) ||
  node.previewMediaType === 'video' ||
  (!node.previewMediaType && hasVideoInput.value)
    ? 'video' : 'image'

const hasVideoInput = computed(() =>
  lgraphNode.value?.inputs?.some((input) => input.type === 'VIDEO') ?? false)
```

`hasVideoInput` counts the **slot**, not a link, so `SaveAsGif`'s optional `video` input makes every fresh node a video node regardless of what the backend returns. Nothing in the `ui` payload can defeat that. `web/save_as_gif_preview.js` therefore sets `node.previewMediaType = "image"` in `nodeCreated`, which fails the `!node.previewMediaType` guard and keeps the result on the image path; the same property also short-circuits the old LiteGraph UI's `isVideoNode()`, so one line covers both. **That script is the only reason this package has a `WEB_DIRECTORY`** — the alternative was dropping the VIDEO input, which would cost the direct `Load GIF as Video` / `Loop Video` → `Save as GIF` wiring the package exists for.

**The misclassification also sticks to the node, which makes this look unfixed after it is fixed.** The full branch is `isVideoOutput(o) || isVideoNode(this)`, and the preview helpers stamp the node on their way through:

```js
useNodeImage = (e, t) => { e.previewMediaType = `image`; ... }
useNodeVideo = (e, t) => { e.previewMediaType = `video`; ... }
function isVideoNode(e) { return e ? e.previewMediaType === `video` || !!e.videoContainer : false }
```

So a node that was once sent to the video player keeps `previewMediaType = "video"` and takes the video path forever after, no matter what the backend now returns — a self-sustaining loop, since only the image path would clear it. It is a runtime property (not serialized into the workflow) and `videoContainer` is a DOM node, so **a browser reload clears both**; a server restart alone does not. If someone reports "Invalid URL" while the `ui` payload is provably correct, that is the reason: tell them to reload the page, not to change the payload.

#### Frame differencing

Pillow's `_write_multiple_frames` always crops each frame to the bounding box of where it differs from the frame it was *handed* before, and folds a frame identical to the previous one into its delay. That is the default output: rectangular deltas, `disposal` 0. `frame_diff` adds pixel-level deltas on top, and four things about it are load-bearing:

- **Unchanged pixels are found by comparing `result[i]` with `result[i - 1]` as colors, never indices.** Under `per_frame` each frame has its own palette, so equal indices mean nothing. The transparent slot (index 0, the same one the letterbox uses) marks them, and `disposal=1` tells the viewer to leave the previous pixel there; the composited canvas is then always the true frame, so the `images` output needs no change and the file reads back to it exactly.
- **A frame that changed nothing is folded into the previous page's delay by the node, not left to Pillow.** Pillow would compare the *diffed* pages, and an all-transparent page after a page with changes is not identical to it.
- **Under `global`, the page handed to Pillow repeats the previous page outside the changed bounding box, and is the diff inside it.** Handing Pillow the plain diff makes its crop "where either this frame or the last one changed", and the whole canvas for the frame after the opaque first one — measured: the file *grew* (9363 → 10135 bytes on a one-block clip) until this was done, and matches the tight crop after it (9460). Outside the box the repeated indices compare equal, so Pillow crops them away; inside, unchanged pixels are transparent.
- **Under `per_frame` the plain diff is handed over instead, and the spare color is chosen once for all frames.** Pillow compares frames with different palettes as RGBA, so repeating the previous page's *indices* reads as a change wherever the palette moved, and a transparent slot whose color differed between frames would read as a change everywhere it appears. The per-frame `Ditherer`s are therefore built up front so `_spare_color` can be given every palette at once — and they are built up front under `per_frame` regardless of transparency, under a `ProgressBar` sized `2 * count`, because a dithered `diversity` palette costs over half a second per frame and the bar only starting to move after that pass looked like a hang. Even so, `per_frame` gains little from differencing, because a new palette re-colors the static parts of the frame too; that is a property of the mode, not a bug.

Where it pays: scattered changes over a still background with a stable dither (two corner blocks, 64 colors: 40.6 → 11.0 KB with `halftone-square`). Where it does not: a single moving object (the rectangle was already tight), error diffusion (nearly every pixel changes), `per_frame`.

#### Frame delays

`_frame_delays` emits the *difference between consecutive rounded playback times*, not one rounded delay repeated. At 12fps that is 8, 9, 8, 8, 9… centiseconds; repeating `round(100/12) = 8` would run the animation 4% short and drift visibly on a long clip. The 2cs floor matches the `_MIN_DELAY_MS = 20` clamp on the read side and caps output at 50fps, which is the format's real ceiling.

### ComfyUI decoder caveat (affects tests, not the node)

`VideoFromFile.get_components()` builds a `pad`+`fillborders` filter graph for any video whose width is not a multiple of 32, and that graph fails with `av.error.ArgumentError` on very small frames. Test fixtures fed through `VideoFromFile` must therefore be reasonably sized (64x64 works; 4x4 does not). This is upstream behavior, not something this package can fix.

## File listing

`_list_animation_files` filters by extension (`.gif/.webp/.png/.apng`) rather than using `folder_paths.filter_files_content_types(files, ["image"])`, which would also list JPEGs that can never be animations. `.png` is in the list because APNG shares the extension; a plain PNG loads as a single-frame animation and still works.

The `file` combo uses `{"image_upload": True}` — animated GIF/WEBP/APNG go through ComfyUI's *image* upload endpoint, not the video one.

**The options list is `["", *files]` with `"default": ""`.** ComfyUI has no remember-last-value mechanism; a combo simply defaults to its first option, so without the blank a newly added node silently points at whatever file sorts first and looks configured when it is not. `VALIDATE_INPUTS` turns the blank into a readable "No animation file selected", and `IS_CHANGED` returns `nan` for it (validation aborts first, but `getmtime("")` would raise if it ever ran).

## Commands

Lint/format:

```bash
ruff check .
ruff format .
```

There are no tests in the repo. The GIF path was verified with throwaway scripts in the scratchpad that stub `folder_paths` and load this directory as a package (see below); they cover, and should be re-created if this code is touched:

1. every kernel's wavefront result against a plain nested-loop raster-order reference (must be **exactly** equal),
2. `dither_strength = 0` collapsing to the same indices as `dither = none` for every method,
3. the halftone plans really holding at most 2 distinct colors, the cell geometry and 255-entry plan cap at every size, and `halftone_ink` reversing the screen without changing dark coverage,
4. the written GIF read back matching the node's `images` output byte for byte, for every `palette_scope` × `dither`,
5. the GIF byte stream itself, parsed by hand, having zero local color tables under `palette_scope = global`,
6. written file size falling monotonically as `halftone_size` grows — **on material with flat areas**; `make_batch`-style noise over a gradient will not show it,
7. `fps` below `source_fps` producing `round(count * fps / source_fps)` frames with the running time unchanged, and `fps` at or above it changing nothing,
8. mask screens using the exact `halftone_ink` RGB, that color reaching the written GIF, every covered pixel being the ink across wildly different frames (the lattice must not move), coverage rising with `dither_strength` to ~0.5 at full, and the uncovered ground matching a plain nearest-color pass, For the brick mask additionally: the dot's pixel count constant across `dither_strength`, the pitch at `size / sqrt(strength)`, and coverage still `strength / 2`. For the `-mask-inverted` methods: the output being the exact pixel-for-pixel complement of the matching `-mask` at every strength (ink where the mask had ground and vice versa), ink coverage at `1 - strength / 2`, and strength 0 still collapsing to the plain nearest color.
9. `uniform_palette` levels at each cube boundary and its `r*L²+g*L+b` index order, poster output staying on the grid, poster refusing a non-cubic palette, poster at strength 0 matching the plain nearest color, and the written GIF's color table being exactly the grid size,
10. `halftone_steps` clamping the plan to `min(steps, cells, 255)` with a contiguous 0..n-1 matrix for every screen, `steps=255` reproducing `steps=0` byte for byte, and 2 steps beating `auto` on file size,
11. the `ui` payload's exact shape — `images` and `text` only, one image entry, the three expected keys, a `.gif` filename with no path separator, and **no `animated` key**,
12. the canvas size in the GIF header, a transparent index appearing only when the aspect ratios disagree, and — via `convert("RGBA")` — the bars reading alpha 0 while the picture area reads alpha 255,
13. `frame_diff`: every frame flagged transparent-index 0 and `disposal` 1, the written boxes staying the tight changed rectangle under `global`, the composited readback equal to `images` under both scopes and with `pillow_optimize` on, identical frames folded into the previous delay with the total running time unchanged, letterbox bars still alpha 0, the poster grid keeping its cube (216 + slot), and a two-corner clip shrinking to roughly a quarter with a stable dither.

 Both nodes were verified against the real ComfyUI at `E:\_BIN\StabilityMatrix\Data\Packages\ComfyUI` by stubbing `folder_paths`, loading this directory as a package via `importlib.util.spec_from_file_location(..., submodule_search_locations=[PKG])` (needed now that the modules use relative imports — the hyphenated directory name is not importable directly) with that ComfyUI on `sys.path`, and round-tripping results through `save_to()` + PyAV readback.

To exercise the node, drop the repo into `ComfyUI/custom_nodes/` and restart ComfyUI — there is no build step. Note that `INPUT_TYPES` changes require a **server restart**, not just a browser reload.
