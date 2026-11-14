"""Runtime configuration.

The database location is configurable through the ``TRACE_DB_PATH``
environment variable so that tests can point the service at a throwaway
database while Docker keeps the real one on a mounted volume.
"""

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("TRACE_DB_PATH", "/data/trace.db"))

# Serial numbers of finished products (成品) share this prefix.
PRODUCT_PREFIX = "P-"
