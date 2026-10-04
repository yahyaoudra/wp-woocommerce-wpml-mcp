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
    "date_created","date_modified","categories","images",
)
DETAIL_KEYS = SUMMARY_KEYS + (
    "description","short_description","featured","catalog_visibility","virtual",
    "downloadable","tax_status","tax_class","weight","dimensions","shipping_class",
    "attributes","default_attributes","parent_id","menu_order","meta_data",
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


async def _translated_attribute_option(attribute_id: int, option: str, source_lang: str, target_lang: str) -> str:
    """Map a global WooCommerce attribute term to its translated display name."""
    try:
        source_terms = await client.woo(
            "GET", f"products/attributes/{attribute_id}/terms",
            params={"lang": source_lang, "per_page": 100}
        )
        target_terms = await client.woo(
            "GET", f"products/attributes/{attribute_id}/terms",
            params={"lang": target_lang, "per_page": 100}
        )
        source_term = next(
            (
                t for t in source_terms
                if str(t.get("name", "")).casefold() == option.casefold()
                or str(t.get("slug", "")).casefold() == option.casefold()
            ),
            None,
        )
        if source_term:
            translations = source_term.get("translations") or {}
            target_id = translations.get(target_lang) if isinstance(translations, dict) else None
            if target_id:
                target = next((t for t in target_terms if str(t.get("id")) == str(target_id)), None)
                if target and target.get("name"):
                    return str(target["name"])
        direct = next(
            (
                t for t in target_terms
                if str(t.get("name", "")).casefold() == option.casefold()
                or str(t.get("slug", "")).casefold() == option.casefold()
            ),
            None,
        )
        if direct and direct.get("name"):
            return str(direct["name"])
    except Exception:
        pass
    return option


async def _translated_product_attributes(attributes: list[dict[str, Any]], source_lang: str, target_lang: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attr in attributes or []:
        row = {
            k: attr[k] for k in ("id", "name", "position", "visible", "variation")
            if k in attr
        }
        options = list(attr.get("options") or [])
        attribute_id = int(attr.get("id") or 0)
        if attribute_id > 0:
            row["options"] = [
                await _translated_attribute_option(attribute_id, str(opt), source_lang, target_lang)
                for opt in options
            ]
        else:
            row["options"] = options
        out.append(row)
    return out


async def _translated_variation_attributes(attributes: list[dict[str, Any]], source_lang: str, target_lang: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attr in attributes or []:
        row = {k: attr[k] for k in ("id", "name") if k in attr}
        option = str(attr.get("option") or "")
        attribute_id = int(attr.get("id") or 0)
        if attribute_id > 0 and option:
            option = await _translated_attribute_option(attribute_id, option, source_lang, target_lang)
        row["option"] = option
        out.append(row)
    return out


@mcp.tool(title="Preview or create full WPML variable product translation", annotations=WRITE)
async def wpml_create_variable_product_translation(
    source_product_id: Annotated[int, Field(gt=0)],
    target_lang: str,
    name: str,
    description: str = "",
    short_description: str = "",
    slug: str | None = None,
    status: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "wpml_create_variable_product_translation"
    try:
        source = await client.woo("GET", f"products/{source_product_id}")
        if source.get("type") != "variable":
            raise ValueError("Source product is not variable.")
        existing = source.get("translations") or {}
        if isinstance(existing, dict) and existing.get(target_lang):
            raise ValueError(f"Translation already exists: {existing[target_lang]}")
        source_lang = str(source.get("lang") or "en")
        translated_attributes = await _translated_product_attributes(
            list(source.get("attributes") or []), source_lang, target_lang
        )
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "short_description": short_description,
            "type": "variable",
            "status": status or source.get("status") or "draft",
            "lang": target_lang,
            "translation_of": source_product_id,
            "attributes": translated_attributes,
        }
        if slug:
            payload["slug"] = slug
        if source.get("default_attributes"):
            payload["default_attributes"] = await _translated_variation_attributes(
                list(source.get("default_attributes") or []), source_lang, target_lang
            )
        if settings.translation_copy_images and source.get("images"):
            payload["images"] = [{"id": i["id"]} for i in source["images"] if i.get("id")]
        if settings.translation_copy_physical_fields:
            for k in ("virtual", "downloadable", "tax_status", "tax_class", "weight", "dimensions", "shipping_class"):
                if source.get(k) not in (None, ""):
                    payload[k] = source[k]

        source_variations = await client.woo(
            "GET", f"products/{source_product_id}/variations",
            params={"lang": source_lang, "per_page": 100},
        )
        variation_previews: list[dict[str, Any]] = []
        for v in source_variations:
            vp: dict[str, Any] = {
                "lang": target_lang,
                "translation_of": v.get("id"),
                "status": v.get("status") or "publish",
                "regular_price": v.get("regular_price") or "",
                "sale_price": v.get("sale_price") or "",
                "virtual": bool(v.get("virtual", False)),
                "downloadable": bool(v.get("downloadable", False)),
                "manage_stock": bool(v.get("manage_stock", False)),
                "stock_status": v.get("stock_status") or "instock",
                "backorders": v.get("backorders") or "no",
                "menu_order": int(v.get("menu_order") or 0),
                "attributes": await _translated_variation_attributes(
                    list(v.get("attributes") or []), source_lang, target_lang
                ),
            }
            if v.get("stock_quantity") is not None:
                vp["stock_quantity"] = v.get("stock_quantity")
            for k in ("weight", "dimensions", "shipping_class"):
                if v.get(k) not in (None, ""):
                    vp[k] = v[k]
            image = v.get("image") or {}
            if image.get("id"):
                vp["image"] = {"id": image["id"]}
            variation_previews.append({"source_variation_id": v.get("id"), "payload": vp})

        if dry_run:
            audit.record(action, target=source_product_id, dry_run=True, payload={
                "product": payload, "variation_count": len(variation_previews)
            })
            return {
                "ok": True,
                "dry_run": True,
                "source_product_id": source_product_id,
                "product_payload": payload,
                "variations": variation_previews,
            }

        policy.require_write()
        translated = await client.woo("POST", "products", json=payload)
        translated_id = int(translated["id"])
        created_variations: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for item in variation_previews:
            try:
                created = await client.woo(
                    "POST", f"products/{translated_id}/variations", json=item["payload"]
                )
                created_variations.append({
                    "source_variation_id": item["source_variation_id"],
                    "translated_variation_id": created.get("id"),
                    "price": created.get("price"),
                    "stock_status": created.get("stock_status"),
                    "stock_quantity": created.get("stock_quantity"),
                })
            except Exception as ve:
                errors.append({
                    "source_variation_id": item["source_variation_id"],
                    "error": str(ve),
                })
        result = {
            "id": translated_id,
            "variation_count": len(created_variations),
            "variation_errors": errors,
        }
        audit.record(action, target=source_product_id, dry_run=False, payload={
            "product": payload, "variation_count": len(variation_previews)
        }, result=result)
        return {
            "ok": len(errors) == 0,
            "dry_run": False,
            "product": pick(translated),
            "created_variations": created_variations,
            "variation_errors": errors,
        }
    except Exception as e:
        return fail(action, e, source_product_id, dry_run)


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
        # Translation must mirror source inventory state without mutating the source.
        for k in ("manage_stock","stock_quantity","stock_status","backorders"):
            if source.get(k) is not None:
                payload[k] = source[k]
        if translated_category_ids is not None:
            payload["categories"] = [{"id":i} for i in translated_category_ids]
        if source.get("type") == "variable":
            source_lang = str(source.get("lang") or "en")
            payload["attributes"] = await _translated_product_attributes(
                list(source.get("attributes") or []), source_lang, target_lang
            )
            if source.get("default_attributes"):
                payload["default_attributes"] = await _translated_variation_attributes(
                    list(source.get("default_attributes") or []), source_lang, target_lang
                )
            source_variations = await client.woo(
                "GET", f"products/{source_product_id}/variations",
                params={"lang": source_lang, "per_page": 100},
            )
            variation_previews: list[dict[str, Any]] = []
            for v in source_variations:
                vp: dict[str, Any] = {
                    "lang": target_lang,
                    "translation_of": v.get("id"),
                    "status": v.get("status") or "publish",
                    "regular_price": v.get("regular_price") or "",
                    "sale_price": v.get("sale_price") or "",
                    "virtual": bool(v.get("virtual", False)),
                    "downloadable": bool(v.get("downloadable", False)),
                    "manage_stock": bool(v.get("manage_stock", False)),
                    "stock_status": v.get("stock_status") or "instock",
                    "backorders": v.get("backorders") or "no",
                    "menu_order": int(v.get("menu_order") or 0),
                    "attributes": await _translated_variation_attributes(
                        list(v.get("attributes") or []), source_lang, target_lang
                    ),
                }
                if v.get("stock_quantity") is not None:
                    vp["stock_quantity"] = v.get("stock_quantity")
                image = v.get("image") or {}
                if image.get("id"):
                    vp["image"] = {"id": image["id"]}
                variation_previews.append({"source_variation_id": v.get("id"), "payload": vp})
            if dry_run:
                audit.record(action, target=source_product_id, dry_run=True, payload={
                    "product": payload, "variation_count": len(variation_previews)
                })
                return {
                    "ok":True,"dry_run":True,"payload":payload,
                    "variations":variation_previews,
                }
            policy.require_write()
            data = await client.woo("POST", "products", json=payload)
            translated_id = int(data["id"])
            created_variations, variation_errors = [], []
            for item in variation_previews:
                try:
                    created = await client.woo(
                        "POST", f"products/{translated_id}/variations", json=item["payload"]
                    )
                    created_variations.append({
                        "source_variation_id": item["source_variation_id"],
                        "translated_variation_id": created.get("id"),
                        "price": created.get("price"),
                        "stock_status": created.get("stock_status"),
                        "stock_quantity": created.get("stock_quantity"),
                    })
                except Exception as ve:
                    variation_errors.append({
                        "source_variation_id": item["source_variation_id"],
                        "error": str(ve),
                    })
            audit.record(action, target=source_product_id, dry_run=False, payload={
                "product": payload, "variation_count": len(variation_previews)
            }, result={
                "id": translated_id, "variation_count": len(created_variations),
                "variation_errors": variation_errors,
            })
            return {
                "ok": len(variation_errors) == 0,
                "dry_run":False,
                "product":pick(data),
                "created_variations":created_variations,
                "variation_errors":variation_errors,
            }
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
        # Repair a previously-created but unlinked translation by exact title match.
        if not tid and name:
            candidates = await client.woo("GET", "products", params={
                "lang": target_lang, "search": name, "status": "any", "per_page": 100
            })
            exact = next(
                (p for p in candidates if str(p.get("name") or "").strip().casefold() == name.strip().casefold()),
                None,
            )
            if exact:
                tid = exact.get("id")
                if tid:
                    policy.require_write()
                    await client.woo("PUT", f"products/{tid}", json={
                        "lang": target_lang,
                        "translation_of": source_product_id,
                    })
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
