# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""Where the command line finds config.yaml without --config.

Measured: after a regular `pip install .`, `fue-monitor` looked for
site-packages/config.yaml and died with a FileNotFoundError traceback.
"""

from __future__ import annotations

import logging

import pytest

from fue_monitoring import config as config_module
from fue_monitoring import main


@pytest.fixture(autouse=True)
def restore_package_logger():
    """main.main() configures the CLI logger (propagate=False, own handler). Left in
    place, that hides every later test's records from caplog."""
    logger = logging.getLogger("fue_monitoring")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers[:], logger.level, logger.propagate = saved


def test_the_file_next_to_the_package_wins(tmp_path, monkeypatch):
    beside = tmp_path / "repo" / "config.yaml"
    beside.parent.mkdir()
    beside.write_text("{}")
    (tmp_path / "config.yaml").write_text("{}")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", beside)
    monkeypatch.chdir(tmp_path)
    assert config_module.find_default_config() == beside


def test_the_current_directory_is_used_after_an_install(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "site-packages" / "config.yaml")
    (tmp_path / "config.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    assert config_module.find_default_config().resolve() == (tmp_path / "config.yaml").resolve()


def test_none_when_there_is_no_config_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "site-packages" / "config.yaml")
    monkeypatch.chdir(tmp_path)
    assert config_module.find_default_config() is None


def test_cli_reports_a_missing_config_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "find_default_config", lambda: None)
    assert main.main([]) == 2
    assert "--config" in capsys.readouterr().err


def test_cli_reports_a_wrong_config_path(tmp_path, capsys):
    missing = tmp_path / "nope.yaml"
    assert main.main(["--config", str(missing)]) == 2
    assert str(missing) in capsys.readouterr().err
