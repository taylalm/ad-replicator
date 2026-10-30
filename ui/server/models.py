"""Video and image models a job can use. `version` selects the script's rules (durations, resolutions, features);
`endpoint` is the account's ModelArk inference endpoint sent as the model ID (None = the public model ID). The 2.0 and
flash models need an endpoint of your own (guide step A2): put its id (ep-...) in place of None."""
from __future__ import annotations

VIDEO_MODELS = {
    "seedance-2.5": {"label": "Seedance 2.5", "version": "2.5", "endpoint": None},
    "seedance-2.0": {"label": "Seedance 2.0", "version": "2.0", "endpoint": None},
    "seedance-2.0-fast": {"label": "Seedance 2.0 fast", "version": "2.0-fast", "endpoint": None},
    "seedance-2.0-mini": {"label": "Seedance 2.0 mini", "version": "2.0-mini", "endpoint": None},
}
IMAGE_MODELS = {
    "seedream-5.0-pro": {"label": "Seedream 5.0 pro", "version": "5.0-pro", "endpoint": None},
    "seedream-5.0-flash": {"label": "Seedream 5.0 flash", "version": "5.0-flash", "endpoint": None},
}
DEFAULT_VIDEO = "seedance-2.5"
DEFAULT_IMAGE = "seedream-5.0-pro"


def flags(entry: dict) -> str:
    """The command-line flags every generate-with-seedance / generate-with-seedream call must carry."""
    return f"--version {entry['version']}" + (f" --model {entry['endpoint']}" if entry.get("endpoint") else "")
