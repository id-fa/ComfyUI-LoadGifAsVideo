from .load_gif_as_video_node import (
    NODE_CLASS_MAPPINGS as _GIF_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _GIF_NAMES,
)
from .loop_video_node import (
    NODE_CLASS_MAPPINGS as _LOOP_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _LOOP_NAMES,
)
from .save_as_gif_node import (
    NODE_CLASS_MAPPINGS as _SAVE_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _SAVE_NAMES,
)
from .save_as_animated_webp_node import (
    NODE_CLASS_MAPPINGS as _WEBP_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _WEBP_NAMES,
)
from .load_video_trim_node import (
    NODE_CLASS_MAPPINGS as _TRIM_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _TRIM_NAMES,
)

NODE_CLASS_MAPPINGS = {
    **_GIF_CLASSES,
    **_LOOP_CLASSES,
    **_SAVE_CLASSES,
    **_WEBP_CLASSES,
    **_TRIM_CLASSES,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    **_GIF_NAMES,
    **_LOOP_NAMES,
    **_SAVE_NAMES,
    **_WEBP_NAMES,
    **_TRIM_NAMES,
}

# Two scripts. `save_as_gif_preview.js` exists only because `SaveAsGif` and
# `SaveAsAnimatedWebP` have a VIDEO input: both node UIs read that slot as
# "preview this node as a video", which breaks on a .gif or .webp. `load_video_trim.js` is the Load Video node's panel:
# scrubbing, the crop box and the loop-point search.
WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
