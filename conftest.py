# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

# Present so pytest puts the repository root on sys.path and `tests/` can import
# the fue_monitoring package without the project needing to be installed.


import pytest


@pytest.fixture
def config(tmp_path) -> dict:
    """A minimal run configuration, shared by the orchestration tests."""
    return {
        "keywords": ["GLOBAL-KEYWORD"],
        "min_funding_rate_percent": 0,
        "open_deadlines_only": True,
        "deadline_warning_days": 14,
        "llm": {"enabled": True, "backend": "gemini", "gemini": {"api_key": "k", "model": "m"}},
        "cache": {"enabled": False, "path": str(tmp_path / "pages.json"), "max_age_days": 30},
        "review": {"enabled": False, "hide_dismissed": False, "dismissed_values": [],
                   "state_path": str(tmp_path / "review.json")},
        "profiles": {
            "directory": str(tmp_path / "profiles"), "output_dir": str(tmp_path / "out"),
            "cache_path": str(tmp_path / "matches.json"), "cache_max_age_days": 30,
            "min_score": 0, "max_evaluations_per_profile": None, "max_text_chars": 20000,
            "review_state_template": str(tmp_path / "review_{profile}.json"),
        },
    }
