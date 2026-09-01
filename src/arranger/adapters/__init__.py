"""External format adapters.

Adapters translate files, APIs, and other outside-world artifacts into the
small internal types used by the rest of the system.
"""

from .score_json import dump_score_json, load_score_json

__all__ = ["dump_score_json", "load_score_json"]
