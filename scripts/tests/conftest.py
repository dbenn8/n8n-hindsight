"""Pytest path setup for the sync-script suite.

Helpers live in ``_sync_test_helpers`` (a uniquely named module rather than
``conftest`` itself) so this suite can be collected in the same pytest run as
other test directories without module-name collisions.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (_HERE, os.path.dirname(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
