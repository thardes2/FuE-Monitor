# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def find_default_config() -> Path | None:
    """Where the command line looks for config.yaml when --config isn't given.

    Next to the package first, which is the repository root in a source checkout or
    an editable install. After a regular `pip install .` that is site-packages,
    where no config.yaml is shipped (deliberately, see pyproject.toml), so the
    current directory is tried next. None if neither has one.
    """
    for candidate in (DEFAULT_CONFIG_PATH, Path.cwd() / "config.yaml"):
        if candidate.is_file():
            return candidate
    return None


def _deep_merge(base: dict, overrides: dict) -> dict:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(
    path: Path | str = DEFAULT_CONFIG_PATH,
    overrides: dict | None = None,
) -> dict:
    """Loads config.yaml, config.local.yaml on top of it, then `overrides`.

    `overrides` is for a host application that keeps its settings somewhere else
    entirely — a database, environment variables, a request — and only wants to
    bend a few keys. It is deep-merged last, so it beats both files, and it is
    applied *before* the relative paths below are resolved, so an override may give
    a plain relative path just as the files do.
    """
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

    if overrides:
        _deep_merge(config, overrides)

    base_dir = path.resolve().parent
    config["_base_dir"] = base_dir
    config["manual_import"]["input_dir"] = str(base_dir / config["manual_import"]["input_dir"])
    config["output"]["path"] = str(base_dir / config["output"]["path"])
    config["cache"]["path"] = str(base_dir / config["cache"]["path"])
    if config["cache"].get("search_path"):
        config["cache"]["search_path"] = str(base_dir / config["cache"]["search_path"])
    config["review"]["state_path"] = str(base_dir / config["review"]["state_path"])

    newsletter = config.get("foerderinfo_newsletter")
    if newsletter:
        newsletter["state_path"] = str(base_dir / newsletter["state_path"])

    profiles = config.get("profiles")
    if profiles:
        for key in ("directory", "output_dir", "static_catalogue", "cache_path"):
            profiles[key] = str(base_dir / profiles[key])
        # Keep the {profile} placeholder intact while making the path absolute.
        profiles["review_state_template"] = str(base_dir / profiles["review_state_template"])

    return config
