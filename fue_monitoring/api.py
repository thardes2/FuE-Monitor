# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""The supported surface for using this project as a library.

Everything re-exported here is what a second application — a web service, say — may
rely on. Anything else in the package is internal and gets refactored without
notice; this module exists so that a consumer has one place to import from and one
contract to read.

Three things it guarantees:

1. **Nothing is written.** `collect()` and `match_profiles()` return data. Putting
   reports on disk is a separate, optional step (`write_reports`), which the CLI
   uses and a web service need not.
2. **Nothing is printed.** Progress goes through `logging` under the
   "fue_monitoring" logger. Attach your own handler; the package never configures
   one, so it cannot take over your application's output.
3. **Configuration is explicit.** `load_config()` takes a path and/or an overrides
   mapping, so the library does not insist on a file next to its own source.

A minimal run:

    import logging
    from fue_monitoring import api

    logging.basicConfig(level=logging.INFO)
    config = api.load_config("/srv/fue/config.yaml", overrides={"llm": {"backend": "ollama"}})

    profiles, skipped = api.running_profiles(config)
    collection = api.collect(api.search_config_for(config, profiles))
    for result in api.match_profiles(config, collection, profiles):
        markdown = api.render_markdown(result.profile, result.matches,
                                       frameworks=result.frameworks,
                                       failed=result.failed)
        ...  # store it, serve it, mail it

Profile data is confidential, and `running_profiles()` enforces that: a profile only
runs against a hosted backend if it sets `cloud_erlaubt: true`. Profiles without
that consent come back in `skipped`, and it is on the caller not to feed them to a
hosted model by some other route.
"""

from __future__ import annotations

from .cache import PageCache
from .config import load_config
from .dedup import merge_duplicates
from .export import write_excel
from .matching import BackendNotLocalError, Match, prefilter
from .models import FundingCall
from .pipeline import (
    CollectionResult,
    ProfileResult,
    apply_filters,
    collect,
    match_profiles,
    running_profiles,
    search_config_for,
)
from .profile_report import render_html, render_markdown, write_reports
from .profiles import Profile, ProfileError, Theme, load_profile, load_profiles
from .review import ReviewState
from .themes import suggest_themes

__all__ = [
    # configuration
    "load_config",
    # data types
    "FundingCall",
    "Profile",
    "Theme",
    "Match",
    "CollectionResult",
    "ProfileResult",
    # the pipeline
    "running_profiles",
    "search_config_for",
    "collect",
    "apply_filters",
    "match_profiles",
    "prefilter",
    "merge_duplicates",
    # helping to write a profile: suggestions only, nothing is stored
    "suggest_themes",
    # rendering, which returns strings
    "render_markdown",
    "render_html",
    # optional writers, for callers that do want files
    "write_reports",
    "write_excel",
    # state and caching
    "PageCache",
    "ReviewState",
    "load_profile",
    "load_profiles",
    # errors worth catching by name
    "BackendNotLocalError",
    "ProfileError",
]
