"""SQLite mapping between user emails and ROM subdirectory names.

Implementation lives in gb_mcp.storage.db; this module is a compatibility alias.
"""
from gb_mcp.storage.db import *  # noqa: F403
import gb_mcp.storage.db as _impl
import sys
sys.modules[__name__] = _impl
