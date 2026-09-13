"""
TCG Engine: the middleware core.

Reads SortSwift's CSV exports and keeps the catalogue they describe. Writes
no CSV of its own: eBay is reached through its APIs, and nothing is ever
written back to SortSwift.
"""

from .db import Database
from .batches import process_batch_csv, process_batch_file
from .sync import sync_active_listings_csv, sync_active_listings_file
from .plans import PlanError, approve_plan, build_plan, plan_blockers

__all__ = [
    "Database",
    "process_batch_csv",
    "process_batch_file",
    "sync_active_listings_csv",
    "sync_active_listings_file",
    "build_plan",
    "approve_plan",
    "plan_blockers",
    "PlanError",
]

__version__ = "0.1.0"
