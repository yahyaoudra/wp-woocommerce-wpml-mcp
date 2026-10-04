from __future__ import annotations

from typing import Any
from .config import Settings
from .errors import PolicyError

CONTENT_FIELDS = {"name", "description", "short_description", "slug"}
STATUS_FIELDS = {"status"}
PRICE_FIELDS = {"regular_price", "sale_price"}
STOCK_FIELDS = {"manage_stock", "stock_quantity", "stock_status", "backorders"}
IMAGE_FIELDS = {"images"}
CATEGORY_FIELDS = {"categories"}


class Policy:
    def __init__(self, settings: Settings):
        self.s = settings

    def require_write(self) -> None:
        if not self.s.allow_writes:
            raise PolicyError("Writes are disabled. Set ALLOW_WRITES=true after validating read-only access.")

    def require_price_write(self) -> None:
        self.require_write()
        if not self.s.allow_price_writes:
            raise PolicyError("Price writes are disabled by ALLOW_PRICE_WRITES=false.")

    def require_stock_write(self) -> None:
        self.require_write()
        if not self.s.allow_stock_writes:
            raise PolicyError("Stock writes are disabled by ALLOW_STOCK_WRITES=false.")

    def require_publish(self) -> None:
        self.require_write()
        if not self.s.allow_publish:
            raise PolicyError("Publishing is disabled by ALLOW_PUBLISH=false.")

    def validate_bulk_size(self, count: int) -> None:
        if count > self.s.max_bulk_write:
            raise PolicyError(f"Bulk write contains {count} items; MAX_BULK_WRITE is {self.s.max_bulk_write}.")

    def validate_content_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        bad = set(patch) - CONTENT_FIELDS
        if bad:
            raise PolicyError(f"Content patch contains blocked fields: {sorted(bad)}")
        return {k: v for k, v in patch.items() if v is not None}

    def validate_meta_patch(self, meta: dict[str, str]) -> list[dict[str, str]]:
        blocked = set(meta) - self.s.meta_key_allowlist
        if blocked:
            raise PolicyError(f"Meta keys not allowlisted: {sorted(blocked)}")
        return [{"key": k, "value": v} for k, v in meta.items()]
