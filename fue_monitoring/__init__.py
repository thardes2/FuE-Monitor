# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Tobias Hardes

"""FuE-Monitor: collect German and EU R&D funding calls and match them to profiles.

Usable two ways, and both are supported:

  * as a command-line tool — `fue-monitor`, or `python -m fue_monitoring`
  * as a library — see `fue_monitoring.api`, which is the one module a consumer
    should import from

The package deliberately does not configure logging on import. Progress is logged
under the "fue_monitoring" logger and goes nowhere until someone attaches a handler,
so importing this package cannot change a host application's output.
"""

from __future__ import annotations

__version__ = "0.4.0"

__all__ = ["api", "__version__"]
