"""Shared pytest fixtures and helpers for the sync-script test-suite.

The sync scripts live in the parent ``scripts/`` directory and have
hyphenated filenames (e.g. ``sync-docs.py``) which are not importable with a
normal ``import`` statement.  We load them via ``importlib`` from an explicit
file path so the characterization tests can exercise their module-level
functions directly.
"""
import importlib.util
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure ``import sync_common`` inside the scripts resolves when the scripts
# are loaded via importlib from the test-suite (mirrors how sys.path[0] is the
# script's own directory when a script is run directly).
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def load_script(filename, module_name=None):
    """Import a (possibly hyphenated) script file as a module object.

    The script's module-level code runs on import — that includes reading the
    ``HINDSIGHT_URL`` / ``HINDSIGHT_API_TENANT_API_KEY`` / ``STATE_FILE``
    environment variables into module globals.  Set whatever env you need
    *before* calling this, or patch the resulting module's attributes after.
    """
    path = os.path.join(SCRIPTS_DIR, filename)
    name = module_name or filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register so dataclasses / pickling / "from X import" style lookups work.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
