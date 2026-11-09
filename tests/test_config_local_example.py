# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""config.local.yaml.example: a template anyone can copy, never a place for secrets."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from fue_monitoring import config as config_module

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.local.yaml.example"
SECRET_KEY = re.compile(r"(api_key|password|token|secret)$", re.IGNORECASE)
# Only localhost may appear as an address; an internal host belongs in the real file.
ADDRESS = re.compile(r"https?://(?!localhost[:/]|127\.0\.0\.1[:/])|\b\d{1,3}(\.\d{1,3}){3}\b")


def leaves(data, path=()):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from leaves(value, path + (key,))
    else:
        yield path, data


def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_secrets_in_the_template_are_empty():
    for path, value in leaves(example()):
        if SECRET_KEY.search(path[-1]):
            assert value in ("", None), f"{'.'.join(path)} must stay empty in the template"


def test_the_template_names_no_internal_address():
    text = "\n".join(line.split("#", 1)[0] for line in EXAMPLE.read_text(encoding="utf-8").splitlines())
    assert not ADDRESS.search(text)


def test_every_key_in_the_template_exists_in_config_yaml():
    # The deep merge accepts anything; a misspelt key would silently do nothing.
    base = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for path, _ in leaves(example()):
        node = base
        for key in path:
            assert isinstance(node, dict) and key in node, f"{'.'.join(path)} is not a config.yaml key"
            node = node[key]


def test_a_copy_of_the_template_loads(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(EXAMPLE, tmp_path / "config.local.yaml")
    config = config_module.load_config(tmp_path / "config.yaml")
    assert config["llm"]["backend"] == "ollama"
    assert config["gov_bekanntmachungen"]["brave_api_key"] == ""


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="no git checkout")
def test_the_real_file_is_ignored_and_the_template_is_not():
    def ignored(name):
        return subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0
    assert ignored("config.local.yaml")
    assert not ignored("config.local.yaml.example")
