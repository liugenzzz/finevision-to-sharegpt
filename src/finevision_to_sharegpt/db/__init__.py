"""Optional MySQL-backed consumption ledger.

Importing this package never requires a database driver. ``PyMySQL`` is
imported lazily inside :mod:`.mysql_ledger`, so the file-only pipelines keep
working on installs without it.
"""

from .config import MysqlConfig, load_mysql_config
from .fingerprint import ZipFingerprint, zip_fingerprint
from .ledger import ConsumptionLedger, DatasetVersion, JsonlLedger, ScanPlan, open_ledger

__all__ = [
    "ConsumptionLedger",
    "DatasetVersion",
    "JsonlLedger",
    "MysqlConfig",
    "ScanPlan",
    "ZipFingerprint",
    "load_mysql_config",
    "open_ledger",
    "zip_fingerprint",
]
