"""Run after configuring .env: python scripts/smoke_test.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.client import WordPressClient
from app.config import get_settings

settings = get_settings()


async def main():
    c = WordPressClient(settings)
    rows = await c.woo("GET", "products", params={"per_page": 1})
    print(f"WooCommerce OK. Returned {len(rows)} product(s).")
    try:
        diag = await c.wp("GET", "cxg-mcp/v1/diagnostics")
        print("Helper plugin OK:", diag)
        langs = await c.wp("GET", "cxg-mcp/v1/languages")
        print("WPML languages:", langs)
    except Exception as e:
        print("Helper check failed (optional until installed):", e)


if __name__ == "__main__":
    asyncio.run(main())
