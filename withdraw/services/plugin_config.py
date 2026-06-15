"""Single home for reading the withdraw plugin's runtime config (DRY).

Reads fresh from the shared `config_store` on every call (multi-worker
safe, admin changes take effect without restart) and falls back to the
plugin's `DEFAULT_CONFIG` for any missing key.
"""
from typing import Any, Dict

from flask import current_app


def withdraw_config() -> Dict[str, Any]:
    from plugins.withdraw import DEFAULT_CONFIG

    merged = {**DEFAULT_CONFIG}
    config_store = getattr(current_app, "config_store", None)
    if config_store is not None:
        merged.update(config_store.get_config("withdraw") or {})
    return merged
