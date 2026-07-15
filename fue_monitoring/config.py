# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _deep_merge(base: dict, overrides: dict) -> dict:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Secrets (API keys) live in a gitignored config.local.yaml next to this file,
    # never in config.yaml itself, so they can't end up committed. Deep-merged on
    # top so config.local.yaml only needs to contain the keys it overrides.
    local_path = path.resolve().parent / "config.local.yaml"
    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as f:
            local_config = yaml.safe_load(f) or {}
        _deep_merge(config, local_config)

    base_dir = path.resolve().parent
    config["_base_dir"] = base_dir
    config["manual_import"]["input_dir"] = str(base_dir / config["manual_import"]["input_dir"])
    config["output"]["path"] = str(base_dir / config["output"]["path"])
    return config
