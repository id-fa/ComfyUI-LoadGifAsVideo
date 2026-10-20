"""ComfyUI's progress bar and its cancel check, behind one small object.

Every long loop in this package reports through a `Progress`, for two reasons.
The bar is the obvious one. The other is that **cancelling a queued job only
works if the running node checks for it**: ComfyUI sets a flag and raises
`InterruptProcessingException` from `throw_exception_if_processing_interrupted`,
which the executor calls *between* nodes and nowhere else. `comfy.utils.ProgressBar`
used to call it on every update and no longer does (checked against 0.42.1), so
a node that only moves the bar runs to completion however many times Cancel is
pressed. `Progress` checks on every report, so the latency of a cancel is one
frame of whatever stage is running.

Outside ComfyUI (the test harness) both imports fail and the object degrades to
a counter: reports are accepted, nothing is drawn, nothing is raised.
"""

try:
    from comfy.utils import ProgressBar
except ImportError:  # pragma: no cover - only outside ComfyUI
    ProgressBar = None

try:
    from comfy.model_management import throw_exception_if_processing_interrupted
except ImportError:  # pragma: no cover - only outside ComfyUI

    def throw_exception_if_processing_interrupted():
        return None


def check_interrupt():
    """Raise ComfyUI's `InterruptProcessingException` if the job was cancelled."""
    throw_exception_if_processing_interrupted()


class Progress:
    """A progress bar over `total` steps that also honours Cancel.

    Three ways to drive it, all of which check for an interrupt first:

    - `progress(done, total)` — the callback shape `video_source` and
      `save_frames` take; a `total` of None keeps the current one. Both the
      decoders report a total they only learn while running, so the bar
      follows it.
    - `progress.step(n)` — advance by `n`.
    - `progress.stage(size)` — a `(done, total)` callback for one stage of a
      multi-stage job, offset by whatever came before it, so a bar of
      `3 * count` can be fed by three loops that each count from 0.

    A `total` of 0 draws no bar at all but still checks; that is what a loop
    whose length is unknown passes.
    """

    def __init__(self, total):
        self.total = max(0, int(total))
        self.done = 0
        self._base = 0
        self._bar = (
            ProgressBar(self.total)
            if ProgressBar is not None and self.total > 0
            else None
        )

    def set(self, done, total=None):
        check_interrupt()
        if total is not None and int(total) > 0:
            self.total = int(total)
        self.done = max(0, min(int(done), self.total)) if self.total else int(done)
        if self._bar is not None:
            self._bar.update_absolute(self.done, self.total)
        elif ProgressBar is not None and self.total > 0:
            self._bar = ProgressBar(self.total)
            self._bar.update_absolute(self.done, self.total)

    def __call__(self, done, total=None):
        self.set(done, total)

    def step(self, n=1):
        self.set(self.done + int(n))

    def stage(self, size):
        """A `(done, total)` callback covering the next `size` steps of this bar."""
        base = self._base
        size = max(0, int(size))
        self._base += size

        def update(done, _total=None):
            self.set(base + max(0, min(int(done), size)))

        return update
