"""Wire function that intentionally never returns.

Used to verify warm-process timeout handling. Never ship this pattern in
production bundles.
"""

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


def wire_function(entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, bytes]:
    logger.info(
        "Hung wire invoked: entrypoint=%s seed=%s params=%s. Sleeping indefinitely.",
        entrypoint,
        seed,
        params,
    )
    while True:
        time.sleep(1)
