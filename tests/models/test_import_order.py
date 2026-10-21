# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Every geo and domain-model module can be the first module a process imports.

``api.models.project`` imports ``api.geo.models``, which imports
``api.models.location``, a loop kept safe by ``api/models/__init__.py``
importing none of its modules; each case runs in a clean interpreter state,
since this process has long imported both.
"""

import subprocess
import sys

_SCRIPT = """
import importlib, pkgutil, sys
import api.geo, api.models
names = sorted(m.name for pkg in (api.geo, api.models) for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."))
names += ["api.geo", "api.models", "api.providers.sdk.strategies", "api.feed.models", "api.jobs.messages"]
failed = []
for name in names:
    for loaded in [m for m in sys.modules if m == "api" or m.startswith("api.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
print("\\n".join(failed))
"""


def test_every_geo_and_model_module_imports_first() -> None:
    result = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True, text=True, timeout=120, check=True)
    assert result.stdout.strip() == ""
