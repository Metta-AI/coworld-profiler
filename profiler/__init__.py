"""coworld-profiler: a Coworld that measures hosted episode timing.

The first monotonic and wall-clock marks are taken here, at import of the
package, so every module that imports `profiler` can report time relative to
the earliest point Python ran our code.
"""

import time

FIRST_MARK_MONO_NS = time.monotonic_ns()
FIRST_MARK_WALL_NS = time.time_ns()

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
