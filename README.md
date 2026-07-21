# ComfyUI-LoadGifAsVideo

![Screenshot1](docs/screenshot.webp)

Two nodes for turning short looping material into a VIDEO stream of the length you actually need:

- **Load GIF as Video** — reads an animated GIF / APNG / animated WEBP from the ComfyUI input directory.
- **Loop Video** — takes any existing VIDEO (e.g. a short MP4 from the stock `Load Video`) and loops it.

The stock `Load Video` node cannot open animated images, and turning a GIF into a video usually means chaining an image-batch loader, a frame counter and `Create Video`. And neither of those can stretch a 1-second clip to fill 5 seconds. Both nodes here share the same length controls: loop until the requested length is filled and an adjustable playback speed.

## Nodes

### Load GIF as Video

| Input | Type | Description |
| --- | --- | --- |
| `file` | COMBO (upload) | Animated GIF / APNG / WEBP in the ComfyUI `input` directory. Drag-and-drop upload is supported. A new node starts blank and reports "No animation file selected" until you pick one. |
| `length_mode` | `frames` / `seconds` / `loops` | How the output length is specified. |
| `frames` | INT | Number of frames to output. Used when `length_mode = frames`. |
| `seconds` | FLOAT | Duration to output in seconds. Used when `length_mode = seconds`. |
| `loops` | INT | Number of times the animation plays through. Used when `length_mode = loops`. |
| `speed` | FLOAT | Playback speed multiplier (`1.0` = original speed, `2.0` = twice as fast). |
| `background` | `black` / `white` | Color that transparent pixels are composited over. |

| Output | Type | Description |
| --- | --- | --- |
| `video` | VIDEO | The animation as a video stream. Feed it to `Save Video` or any VIDEO input. |
| `images` | IMAGE | The same frames as an image batch, for feeding a sampler directly. Saves wiring up `Get Video Components`. |

### Loop Video

Loops any VIDEO — an MP4 or WEBM via the stock `Load Video`, a generated video, or the output of `Load GIF as Video`.

| Input | Type | Description |
| --- | --- | --- |
| `video` | VIDEO | The video to loop. |
| `length_mode` | `frames` / `seconds` / `loops` | How the output length is specified. |
| `frames` | INT | Number of frames to output. Used when `length_mode = frames`. |
| `seconds` | FLOAT | Duration to output in seconds. Used when `length_mode = seconds`. |
| `loops` | INT | Number of times the video plays through. Used when `length_mode = loops`. |
| `speed` | FLOAT | Playback speed multiplier. |

| Output | Type | Description |
| --- | --- | --- |
| `video` | VIDEO | The looped video. |
| `images` | IMAGE | The looped frames as an image batch. |

**Audio.** At `speed = 1.0` the audio is looped along with the frames: the source's audio is fitted to exactly one video loop (truncated or zero-padded) and then repeated, so every loop restarts in sync with frame 0. At any other speed only the frame rate changes, which would put the audio out of sync — so the audio is dropped. If you need the audio at a different speed, retime it separately.

## Behavior

The length and speed controls behave identically in both nodes.

**Frame rate is automatic.** The output frame rate comes from the source itself — its own rate multiplied by `speed`. There is no fps widget. Frames are never interpolated or dropped, so at `speed = 1.0` the output is a frame-for-frame copy of the source at its original timing.

**Looping.** If the requested length is longer than the source, it repeats from the beginning as many times as needed (and is cut off mid-loop if the length does not divide evenly). If the requested length is shorter, the source is simply truncated.

**`length_mode = loops`** gives you whole passes with no partial loop at the end: `loops = 3` on a 12-frame source is always exactly 36 frames. `speed` does not change that count — it only changes the frame rate, and therefore how long those 36 frames take to play.

**`seconds` and `speed` interact.** Because `speed` scales the output frame rate, asking for 2 seconds at `speed = 2.0` produces twice as many frames as at `speed = 1.0` — i.e. you see twice as much of the source in the same 2 seconds. That is the intended meaning of "faster".

**Frame limit.** Output is capped at 10000 frames; exceeding it raises rather than trying to allocate the memory.

### Load GIF as Video only

**Variable frame delays.** GIFs may give every frame a different delay. Since a video has one frame rate, such an animation is resampled (nearest frame, no blending) onto its own average rate; the frame count and the total duration are preserved. Animations with a single uniform delay pass through untouched.

**Transparency.** VIDEO carries no alpha channel, so transparent pixels are composited over the `background` color.

**Very short delays.** GIFs that declare a delay of 0ms or 10ms mean "as fast as possible"; browsers render those at 100ms and so does this node.

## Installation

Via ComfyUI Manager, or clone into `ComfyUI/custom_nodes/`:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/id-fa/ComfyUI-LoadGifAsVideo
```

Then restart ComfyUI. There are no extra Python dependencies — Pillow, NumPy and PyTorch all ship with ComfyUI.

Requires a ComfyUI version that has the `VIDEO` type (`comfy_api.input_impl`). On older builds the nodes load but raise a clear error when run.

## License

MIT
