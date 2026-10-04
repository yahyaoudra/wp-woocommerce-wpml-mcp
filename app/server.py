from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import Annotated

from .audit import AuditLogger
from .client import WordPressClient
from .config import get_settings
from .policy import Policy

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
log = logging.getLogger("wp-mcp")

mcp = MCPServer(
    "Self-hosted WordPress WooCommerce WPML",
    instructions=(
        "Manage WooCommerce conservatively. Read before writing. "
        "All write operations default to dry_run=true. Never invent IDs or language codes. "
        "No WordPress users, payments, orders, plugins, themes, SQL, shell, or arbitrary REST access is exposed."
    ),
)
client = WordPressClient(settings)
policy = Policy(settings)
audit = AuditLogger(settings.audit_log)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
HIGH = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

SUMMARY_KEYS = (
    "id","name","slug","status","type","sku","price","regular_price","sale_price",
    "stock_status","stock_quantity","manage_stock","lang","translations","permalink",
    "date_modified","categories","images",
)
DETAIL_KEYS = SUMMARY_KEYS + (
    "description","short_description","featured","catalog_visibility","virtual",
    "downloadable","tax_status","tax_class","weight","dimensions","shipping_class",
    "attributes","default_attributes","parent_id","menu_order",
)

def pick(d: dict[str, Any], keys=DETAIL_KEYS) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if k in d}

def fail(action: str, e: Exception, target=None, dry_run=None) -> dict[str, Any]:
    audit.record(action, target=target, dry_run=dry_run, error=str(e))
    return {"ok": False, "error": str(e), "error_type": type(e).__name__}

@mcp.tool(title="Store connection check", annotations=READ)
async def store_connection_check() -> dict[str, Any]:
    try:
        products = await client.woo("GET", "products", params={"per_page": 1})
        helper = None
        try:
            helper = await client.wp("GET", "cxg-mcp/v1/diagnostics")
        except Exception as e:
            helper = {"available": False, "note": str(e)}
        return {
            "ok": True,
            "site": settings.base_url,
            "woocommerce_read": True,
            "sample_product_count": len(products),
            "helper": helper,
            "write_policy": {
                "allow_writes": settings.allow_writes,
                "allow_price_writes": settings.allow_price_writes,
                "allow_stock_writes": settings.allow_stock_writes,
                "allow_publish": settings.allow_publish,
                "max_bulk_write": settings.max_bulk_write,
            },
        }
    except Exception as e:
        return fail("store_connection_check", e)

@mcp.tool(title="List WPML languages", annotations=READ)
async def wpml_languages() -> dict[str, Any]:
    try:
        data = await client.wp("GET", "cxg-mcp/v1/languages")
        return {"ok": True, **data}
    except Exception as e:
        return fail("wpml_languages", e)

@mcp.tool(title="List WooCommerce products", annotations=READ)
async def products_list(
    language: str | None = None,
    search: str | None = None,
    sku: str | None = None,
    status: str = "any",
    category_id: int | None = None,
    page: Annotated[int, Field(ge=1)] = 1,
    per_page: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    try:
        params = {"page": page, "per_page": per_page, "status": status}
        if language: params["lang"] = language
        if search: params["search"] = search
        if sku: params["sku"] = sku
        if category_id: params["category"] = category_id
        rows = await client.woo("GET", "products", params=params)
        return {"ok": True, "count": len(rows), "products": [pick(x, SUMMARY_KEYS) for x in rows]}
    except Exception as e:
        return fail("products_list", e)

@mcp.tool(title="Get WooCommerce product", annotations=READ)
async def product_get(product_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
    try:
        data = await client.woo("GET", f"products/{product_id}")
        return {"ok": True, "product": pick(data)}
    except Exception as e:
        return fail("product_get", e, product_id)

@mcp.tool(title="Find missing WPML product translations", annotations=READ)
async def wpml_missing_product_translations(
    source_lang: str,
    target_lang: str,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    try:
        out, page = [], 1
        while len(out) < limit:
            rows = await client.woo("GET", "products", params={
                "lang": source_lang, "per_page": min(100, limit), "page": page
            })
            if not rows: break
            for p in rows:
                tr = p.get("translations") or {}
                if not (isinstance(tr, dict) and tr.get(target_lang)):
                    out.append(pick(p, SUMMARY_KEYS))
                    if len(out) >= limit: break
            if len(rows) < min(100, limit): break
            page += 1
        return {"ok": True, "source_lang": source_lang, "target_lang": target_lang, "count": len(out), "products": out}
    except Exception as e:
        return fail("wpml_missing_product_translations", e)

@mcp.tool(title="List WooCommerce product categories", annotations=READ)
async def product_categories_list(
    language: str | None = None,
    search: str | None = None,
    per_page: Annotated[int, Field(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    try:
        params = {"per_page": per_page}
        if language: params["lang"] = language
        if search: params["search"] = search
        rows = await client.woo("GET", "products/categories", params=params)
        return {"ok": True, "count": len(rows), "categories": rows}
    except Exception as e:
        return fail("product_categories_list", e)

@mcp.tool(title="List product variations", annotations=READ)
async def product_variations_list(
    product_id: Annotated[int, Field(gt=0)],
    language: str | None = None,
    per_page: Annotated[int, Field(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    try:
        params = {"per_page": per_page}
        if language: params["lang"] = language
        rows = await client.woo("GET", f"products/{product_id}/variations", params=params)
        return {"ok": True, "count": len(rows), "variations": rows}
    except Exception as e:
        return fail("product_variations_list", e, product_id)

@mcp.tool(title="Preview or update product content", annotations=WRITE)
async def product_update_content(
    product_id: Annotated[int, Field(gt=0)],
    name: str | None = None,
    description: str | None = None,
    short_description: str | None = None,
    slug: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "product_update_content"
    try:
        payload = policy.validate_content_patch({
            k:v for k,v in {
                "name":name,"description":description,"short_description":short_description,"slug":slug
            }.items() if v is not None
        })
        if not payload: raise ValueError("Provide at least one content field.")
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload=payload)
            return {"ok": True, "dry_run": True, "product_id": product_id, "payload": payload}
        policy.require_write()
        data = await client.woo("PUT", f"products/{product_id}", json=payload)
        audit.record(action, target=product_id, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok": True, "dry_run": False, "product": pick(data)}
    except Exception as e:
        return fail(action, e, product_id, dry_run)

@mcp.tool(title="Preview or update product price", annotations=HIGH)
async def product_update_price(
    product_id: Annotated[int, Field(gt=0)],
    regular_price: str | None = None,
    sale_price: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "product_update_price"
    try:
        payload = {k:v for k,v in {"regular_price":regular_price,"sale_price":sale_price}.items() if v is not None}
        if not payload: raise ValueError("Provide regular_price and/or sale_price.")
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload=payload)
            return {"ok": True, "dry_run": True, "product_id":product_id, "payload":payload}
        policy.require_price_write()
        data = await client.woo("PUT", f"products/{product_id}", json=payload)
        audit.record(action, target=product_id, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok": True, "dry_run": False, "product":pick(data)}
    except Exception as e:
        return fail(action, e, product_id, dry_run)

@mcp.tool(title="Preview or update product stock", annotations=HIGH)
async def product_update_stock(
    product_id: Annotated[int, Field(gt=0)],
    manage_stock: bool | None = None,
    stock_quantity: int | None = None,
    stock_status: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "product_update_stock"
    try:
        payload = {k:v for k,v in {
            "manage_stock":manage_stock,"stock_quantity":stock_quantity,"stock_status":stock_status
        }.items() if v is not None}
        if not payload: raise ValueError("Provide at least one stock field.")
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload=payload)
            return {"ok":True,"dry_run":True,"product_id":product_id,"payload":payload}
        policy.require_stock_write()
        data = await client.woo("PUT", f"products/{product_id}", json=payload)
        audit.record(action, target=product_id, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok":True,"dry_run":False,"product":pick(data)}
    except Exception as e:
        return fail(action, e, product_id, dry_run)

@mcp.tool(title="Preview or create WPML product translation", annotations=WRITE)
async def wpml_create_product_translation(
    source_product_id: Annotated[int, Field(gt=0)],
    target_lang: str,
    name: str,
    description: str = "",
    short_description: str = "",
    slug: str | None = None,
    translated_category_ids: list[int] | None = None,
    status: str = "draft",
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "wpml_create_product_translation"
    try:
        source = await client.woo("GET", f"products/{source_product_id}")
        existing = source.get("translations") or {}
        if isinstance(existing, dict) and existing.get(target_lang):
            raise ValueError(f"Translation already exists: {existing[target_lang]}")
        payload: dict[str, Any] = {
            "name":name,"description":description,"short_description":short_description,
            "type":source.get("type","simple"),"status":status,
            "lang":target_lang,"translation_of":source_product_id,
        }
        if slug: payload["slug"] = slug
        if settings.translation_copy_price:
            for k in ("regular_price","sale_price"):
                if source.get(k) not in (None,""): payload[k] = source[k]
        if settings.translation_copy_images and source.get("images"):
            payload["images"] = [{"id":i["id"]} for i in source["images"] if i.get("id")]
        if settings.translation_copy_physical_fields:
            for k in ("virtual","downloadable","tax_status","tax_class","weight","dimensions","shipping_class"):
                if source.get(k) not in (None,""): payload[k] = source[k]
        if translated_category_ids is not None:
            payload["categories"] = [{"id":i} for i in translated_category_ids]
        if source.get("type") == "variable":
            raise ValueError("Variable product translation requires explicit attribute/variation mapping; use manual content translation first.")
        if dry_run:
            audit.record(action, target=source_product_id, dry_run=True, payload=payload)
            return {"ok":True,"dry_run":True,"payload":payload}
        policy.require_write()
        data = await client.woo("POST", "products", json=payload)
        audit.record(action, target=source_product_id, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok":True,"dry_run":False,"product":pick(data)}
    except Exception as e:
        return fail(action, e, source_product_id, dry_run)

@mcp.tool(title="Preview or update existing WPML product translation", annotations=WRITE)
async def wpml_update_product_translation(
    source_product_id: Annotated[int, Field(gt=0)],
    target_lang: str,
    name: str | None = None,
    description: str | None = None,
    short_description: str | None = None,
    slug: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "wpml_update_product_translation"
    try:
        source = await client.woo("GET", f"products/{source_product_id}")
        tr = source.get("translations") or {}
        tid = tr.get(target_lang) if isinstance(tr, dict) else None
        if not tid: raise ValueError(f"No {target_lang} translation found.")
        payload = policy.validate_content_patch({
            k:v for k,v in {
                "name":name,"description":description,"short_description":short_description,"slug":slug
            }.items() if v is not None
        })
        if not payload: raise ValueError("Provide at least one content field.")
        if dry_run:
            audit.record(action, target=tid, dry_run=True, payload=payload)
            return {"ok":True,"dry_run":True,"translation_product_id":tid,"payload":payload}
        policy.require_write()
        data = await client.woo("PUT", f"products/{tid}", json=payload)
        audit.record(action, target=tid, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok":True,"dry_run":False,"product":pick(data)}
    except Exception as e:
        return fail(action, e, source_product_id, dry_run)

@mcp.tool(title="Preview or publish product", annotations=HIGH)
async def product_publish(product_id: Annotated[int, Field(gt=0)], dry_run: bool = True) -> dict[str, Any]:
    action = "product_publish"
    payload = {"status":"publish"}
    try:
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload=payload)
            return {"ok":True,"dry_run":True,"product_id":product_id,"payload":payload}
        policy.require_publish()
        data = await client.woo("PUT", f"products/{product_id}", json=payload)
        audit.record(action, target=product_id, dry_run=False, payload=payload, result={"id":data.get("id")})
        return {"ok":True,"dry_run":False,"product":pick(data)}
    except Exception as e:
        return fail(action, e, product_id, dry_run)

if __name__ == "__main__":
    log.info("Starting MCP on %s:%s%s", settings.mcp_host, settings.mcp_port, settings.mcp_path)
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        max_request_body_size=2 * 1024 * 1024,
    )
