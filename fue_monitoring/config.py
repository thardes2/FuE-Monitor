from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    base_dir = path.resolve().parent
    config["_base_dir"] = base_dir
    config["manual_import"]["input_dir"] = str(base_dir / config["manual_import"]["input_dir"])
    config["output"]["path"] = str(base_dir / config["output"]["path"])
    return config
