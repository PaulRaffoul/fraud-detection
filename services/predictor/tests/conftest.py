"""Add predictor service to sys.path so 'from app.features import ...' works."""

import sys
from pathlib import Path

_service_dir = str(Path(__file__).resolve().parent.parent)
if _service_dir not in sys.path:
    sys.path.insert(0, _service_dir)

# Force reimport of 'app' from this service's directory
if "app" in sys.modules:
    del sys.modules["app"]
    for key in list(sys.modules.keys()):
        if key.startswith("app."):
            del sys.modules[key]
