"""Load a Lambda's modules in isolation.

Every function directory contains `handler.py` / `processor.py` /
`validator.py`, exactly as it will be unpacked in Lambda. In one pytest process
those names collide: whichever test imports first wins, and later tests
silently exercise the wrong function while still passing. Clearing the names
before each load is what keeps a post_standings test from testing post_scores.
"""

import importlib
import sys
from pathlib import Path

FUNCTIONS = Path(__file__).resolve().parent.parent / "functions"
_SIBLINGS = ("handler", "processor", "validator")


def load_function(name: str):
    """Return (processor, validator, handler) for `functions/<name>/`."""
    for mod in _SIBLINGS:
        sys.modules.pop(mod, None)

    path = str(FUNCTIONS / name)
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

    processor = importlib.import_module("processor")
    validator = importlib.import_module("validator")
    handler = importlib.import_module("handler")

    # Guard against the exact failure this helper exists to prevent.
    for mod in (processor, validator, handler):
        assert Path(mod.__file__).parent.name == name, (
            f"expected {name}, loaded {Path(mod.__file__).parent.name}"
        )
    return processor, validator, handler
