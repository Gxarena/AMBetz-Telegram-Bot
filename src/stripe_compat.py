"""
Stripe Python v7+ StripeObject routes attribute access through __getattr__.
Calling `.get(...)` on metadata objects resolves the key "get" as an API field and fails.

Use metadata_get() instead of `.metadata.get(...)`.
"""

from typing import Any, Optional


def metadata_get(metadata_obj: Any, key: str, default: Optional[Any] = None) -> Any:
    if metadata_obj is None:
        return default
    if isinstance(metadata_obj, dict):
        return metadata_obj.get(key, default)
    try:
        return metadata_obj[key]
    except (KeyError, TypeError):
        pass
    try:
        return getattr(metadata_obj, key)
    except AttributeError:
        return default
