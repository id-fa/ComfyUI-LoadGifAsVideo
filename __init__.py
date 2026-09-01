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

NODE_CLASS_MAPPINGS = {**_GIF_CLASSES, **_LOOP_CLASSES, **_SAVE_CLASSES}
NODE_DISPLAY_NAME_MAPPINGS = {**_GIF_NAMES, **_LOOP_NAMES, **_SAVE_NAMES}

# One small script, and only because `SaveAsGif` has a VIDEO input: both node UIs
# read that slot as "preview this node as a video", which breaks on a .gif. See
# web/save_as_gif_preview.js.
WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
