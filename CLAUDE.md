# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A ComfyUI custom node package with two nodes. `README.md` is the user-facing spec.

- `LoadGifAsVideo` ("Load GIF as Video", `load_gif_as_video_node.py`) — reads an animated GIF/APNG/WEBP from the ComfyUI `input` directory and emits a `VIDEO`.
- `LoopVideo` ("Loop Video", `loop_video_node.py`) — takes any `VIDEO` in and loops it to the requested length. This is how short MP4s are handled: `Load Video` → `LoopVideo`.

`video_length.py` holds what they share: the `length_mode`/`frames`/`seconds`/`loops`/`speed` widget block (`length_inputs`) and the count math (`scaled_frame_rate`, `resolve_frame_count`). `__init__.py` merges both modules' mappings. There is no frontend JS and no `WEB_DIRECTORY`. No Python dependencies beyond what ComfyUI already ships (Pillow, NumPy, PyTorch).

**Why MP4 is a second node rather than more extensions in the file combo.** The decode problem (frame delays, disposal, alpha, variable timing) is entirely GIF-specific and shares no code with PyAV decoding; the loop/length/speed problem is entirely source-agnostic. Splitting along that seam also avoids reimplementing a worse `Load Video`, and lets `LoopVideo` loop *any* VIDEO, including generated ones. Do not add `.mp4`/`.webm` to `_ANIMATION_EXTENSIONS`.

## Design decisions

These were chosen deliberately; re-read before "improving" them.

- **Classic `INPUT_TYPES` style, not the V3 `io.Schema` API.** Matches the sibling ComfyUI-Lenient-Switch package and works on older ComfyUI builds. The V3 API in `comfy_extras/nodes_video.py` is *not* what this node uses.
- **`comfy_api` is imported under try/except** (`comfy_api.input_impl.VideoFromComponents`, `comfy_api.util.VideoComponents` — the backwards-compat shims, not the `comfy_api.latest._*` internals). Pyright flags them as unresolved, which is expected. On a ComfyUI without VIDEO support the node still registers and raises a readable error from `load`. The `if VideoFromComponents is None or VideoComponents is None` guard checks **both** names so Pyright narrows both — checking only one reintroduces `reportOptionalCall`.
- **Only one output, `VIDEO`.** No IMAGE/MASK/fps side outputs — users go through the stock `Get Video Components`.
- **Frame rate is always derived from the source; there is no fps widget.** `speed` multiplies that derived rate. Frames are never interpolated or dropped, so `speed = 1.0` is a frame-exact copy of the source. Do not add an fps input or resampling-by-speed; that was explicitly rejected.
- **`LoopVideo` drops audio whenever `speed != 1.0`.** Since only the frame rate changes, kept audio would drift out of sync; shipping no audio beats shipping wrong audio. Retiming the waveform was considered and rejected as out of scope.

## Architecture notes

### LoadGifAsVideo

The pipeline is `_read_animation` → `_to_uniform_timebase` → modulo-index in `load`.

- **`_read_animation`** walks `ImageSequence.Iterator` and `convert("RGBA")` on each frame. Pillow handles GIF/APNG disposal and blending internally during `seek`, and composites partial frames onto the full canvas — do not try to reimplement disposal. Alpha is composited over `background` (VIDEO has no alpha channel) and the result is float32 `[H, W, 3]` in 0..1, which is what `VideoComponents.images` expects. A frame-size consistency check raises rather than letting `np.stack` fail cryptically.
- **Delay clamping.** `_MIN_DELAY_MS = 20` / `_DEFAULT_DELAY_MS = 100`: GIFs that declare 0ms or 10ms mean "as fast as possible" and every browser renders them at 100ms. Matching that is intentional, not a rounding bug.
- **`_to_uniform_timebase`** returns `(frames, fps)`. When all delays are equal it returns the frames **untouched** — that is the common case and the reason the output is bit-exact. Only variable-delay animations are nearest-hold resampled, sampling at each output frame's *midpoint* (`(i + 0.5) * step_ms`) against the cumulative delay ends. The resample deliberately keeps the frame count equal to the source count, so the total duration is preserved. There is no blending — nearest frame only.
- **Looping is `np.arange(count) % len(source_frames)`** applied to the stacked array. This covers looping, truncation and the single-frame (static image) case in one expression; don't special-case them. Every `length_mode` therefore only has to compute `count`.
- **`seconds` mode multiplies by the *speed-scaled* fps**, so 2s at `speed=2` yields twice the frames of 2s at `speed=1` — more of the animation in the same wall-clock time. That is the intended semantics of "faster"; it is not a double-application of `speed`.
- **`loops` mode is `loops * len(source_frames)`** — deliberately *not* speed-scaled, so the output is always a whole number of passes and `speed` only changes the duration. Note it uses the length **after** `_to_uniform_timebase`, which for variable-delay sources equals the original frame count anyway; that equality is why the resample preserves the count.
- **`_MAX_OUTPUT_FRAMES = 10000`** caps the `frames`/`loops` widgets and the final computed count for every mode. Without it, `seconds=3600, speed=100` silently tries to allocate hundreds of GB.

### LoopVideo

`loop()` is `get_components()` → `scaled_frame_rate` → `resolve_frame_count` → `images[torch.arange(count) % source_length]` → rebuild a `VideoFromComponents`. The frame path is deliberately the same shape as the GIF node's.

`_loop_audio` is the only non-obvious part. The source audio is first **fitted to exactly one video loop** — `period = round(source_length * sample_rate / fps)` samples, truncating or zero-padding — and only then tiled. Tiling the raw waveform by *its own* length instead would drift against the video loops whenever the container's audio and video tracks are not exactly the same duration (common in real MP4s), so every repeat would start a little further off frame 0. Do not "simplify" it to a plain `repeat` of the source waveform.

`_loop_audio` returns `None` (rather than raising) for a missing/empty waveform or a nonsense sample rate, so a video with a degenerate audio track still loops.

### ComfyUI decoder caveat (affects tests, not the node)

`VideoFromFile.get_components()` builds a `pad`+`fillborders` filter graph for any video whose width is not a multiple of 32, and that graph fails with `av.error.ArgumentError` on very small frames. Test fixtures fed through `VideoFromFile` must therefore be reasonably sized (64x64 works; 4x4 does not). This is upstream behavior, not something this package can fix.

## File listing

`_list_animation_files` filters by extension (`.gif/.webp/.png/.apng`) rather than using `folder_paths.filter_files_content_types(files, ["image"])`, which would also list JPEGs that can never be animations. `.png` is in the list because APNG shares the extension; a plain PNG loads as a single-frame animation and still works.

The `file` combo uses `{"image_upload": True}` — animated GIF/WEBP/APNG go through ComfyUI's *image* upload endpoint, not the video one.

## Commands

Lint/format:

```bash
ruff check .
ruff format .
```

There are no tests in the repo. Both nodes were verified against the real ComfyUI at `E:\_BIN\StabilityMatrix\Data\Packages\ComfyUI` by stubbing `folder_paths`, loading this directory as a package via `importlib.util.spec_from_file_location(..., submodule_search_locations=[PKG])` (needed now that the modules use relative imports — the hyphenated directory name is not importable directly) with that ComfyUI on `sys.path`, and round-tripping results through `save_to()` + PyAV readback.

To exercise the node, drop the repo into `ComfyUI/custom_nodes/` and restart ComfyUI — there is no build step. Note that `INPUT_TYPES` changes require a **server restart**, not just a browser reload.
