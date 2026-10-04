from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import mimetypes
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import Annotated


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
HIGH = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

SUMMARY_KEYS = (
    "id", "name", "slug", "status", "type", "sku", "price", "regular_price",
    "sale_price", "stock_status", "stock_quantity", "manage_stock", "lang",
    "translations", "permalink", "date_created", "date_modified", "categories",
    "images", "attributes", "default_attributes", "meta_data",
)

MAX_IMAGE_BYTES = 15 * 1024 * 1024
SUPPORTED_IMAGE_MIMES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


def _pick(data: dict[str, Any]) -> dict[str, Any]:
    return {k: data.get(k) for k in SUMMARY_KEYS if k in data}


def _audit(audit: Any, action: str, *, target: Any = None, dry_run: bool | None = None,
           payload: Any = None, result: Any = None, error: str | None = None) -> None:
    try:
        audit.record(action, target=target, dry_run=dry_run, payload=payload, result=result, error=error)
    except Exception:
        pass


def _safe_filename(filename: str | None, mime_type: str) -> str:
    ext = SUPPORTED_IMAGE_MIMES[mime_type]
    base = (filename or f"product-image.{ext}").split("/")[-1].split("\\")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "product-image"
    return f"{stem}.{ext}"


def _decode_base64_image(data: str, filename: str | None = None) -> tuple[bytes, str, str, int, int]:
    value = str(data or "").strip()
    declared_mime = None
    if value.startswith("data:") and "," in value:
        header, value = value.split(",", 1)
        declared_mime = header[5:].split(";", 1)[0].strip().lower()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 image: {exc}") from exc
    return _validate_image_bytes(raw, filename=filename, declared_mime=declared_mime)


def _validate_image_bytes(raw: bytes, filename: str | None = None,
                          declared_mime: str | None = None) -> tuple[bytes, str, str, int, int]:
    if not raw:
        raise ValueError("Image is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            fmt = str(im.format or "").upper()
            width, height = im.size
            im.verify()
    except Exception as exc:
        raise ValueError(f"Invalid image data: {exc}") from exc
    mime_by_format = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    mime_type = mime_by_format.get(fmt)
    if mime_type not in SUPPORTED_IMAGE_MIMES:
        raise ValueError("Only PNG, JPEG and WebP images are supported.")
    if declared_mime and declared_mime not in SUPPORTED_IMAGE_MIMES:
        raise ValueError(f"Unsupported declared MIME type: {declared_mime}")
    return raw, mime_type, _safe_filename(filename, mime_type), int(width), int(height)


def _validate_external_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Image URL must be http or https.")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local/private image URLs are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Local/private image URLs are not allowed.")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
        # Hostname rather than literal IP. Redirect targets are still checked by httpx URL policy below.


async def _languages(client: Any) -> dict[str, Any]:
    data = await client.wp("GET", "cxg-mcp/v1/languages")
    return data if isinstance(data, dict) else {}


async def _validate_language(client: Any, language: str) -> dict[str, Any]:
    code = str(language or "").strip()
    data = await _languages(client)
    rows = list(data.get("languages") or [])
    match = next((row for row in rows if str(row.get("code") or "") == code), None)
    if not match:
        valid = [str(row.get("code")) for row in rows if row.get("code")]
        raise ValueError(f"Unsupported WPML language '{code}'. Valid languages: {valid}")
    return match


async def _validate_categories(client: Any, category_ids: list[int] | None,
                               language: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_id in category_ids or []:
        cid = int(raw_id)
        if cid <= 0:
            raise ValueError(f"Invalid category ID: {raw_id}")
        row = await client.woo("GET", f"products/categories/{cid}")
        if language and row.get("lang") and str(row.get("lang")) != language:
            raise ValueError(
                f"Category {cid} is language '{row.get('lang')}', expected '{language}'."
            )
        out.append(row)
    return out


async def _validate_media_ids(client: Any, image_ids: list[int] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_id in image_ids or []:
        mid = int(raw_id)
        if mid <= 0:
            raise ValueError(f"Invalid media ID: {raw_id}")
        row = await client.wp("GET", f"wp/v2/media/{mid}")
        media_type = str(row.get("media_type") or "")
        mime_type = str(row.get("mime_type") or "")
        if media_type and media_type != "image":
            raise ValueError(f"Media {mid} is not an image.")
        if mime_type and mime_type not in SUPPORTED_IMAGE_MIMES:
            raise ValueError(f"Media {mid} has unsupported MIME type {mime_type}.")
        rows.append(row)
    return rows


async def _validate_attribute(client: Any, attribute_id: int,
                              options: list[str] | None = None,
                              language: str | None = None) -> dict[str, Any]:
    aid = int(attribute_id)
    if aid <= 0:
        raise ValueError(f"Invalid attribute ID: {attribute_id}")
    attr = await client.woo("GET", f"products/attributes/{aid}")
    if options:
        params: dict[str, Any] = {"per_page": 100}
        if language:
            params["lang"] = language
        terms = await client.woo("GET", f"products/attributes/{aid}/terms", params=params)
        names = {str(t.get("name") or "").casefold() for t in terms}
        slugs = {str(t.get("slug") or "").casefold() for t in terms}
        missing = [
            str(opt) for opt in options
            if str(opt).casefold() not in names and str(opt).casefold() not in slugs
        ]
        if missing:
            raise ValueError(f"Unknown options for attribute {aid}: {missing}")
    return attr


async def _upload_bytes(client: Any, raw: bytes, filename: str | None,
                        alt_text: str | None = None, title: str | None = None,
                        declared_mime: str | None = None) -> dict[str, Any]:
    raw, mime_type, safe_name, width, height = _validate_image_bytes(
        raw, filename=filename, declared_mime=declared_mime
    )
    media = await client.wp_media(safe_name, mime_type, raw)
    mid = int(media["id"])
    patch: dict[str, Any] = {}
    if alt_text is not None:
        patch["alt_text"] = str(alt_text)
    if title is not None:
        patch["title"] = str(title)
    if patch:
        media = await client.wp("POST", f"wp/v2/media/{mid}", json=patch)
    return {
        "id": mid,
        "source_url": media.get("source_url"),
        "mime_type": mime_type,
        "filename": safe_name,
        "width": width,
        "height": height,
        "bytes": len(raw),
    }


async def _upload_from_url(client: Any, settings: Any, url: str,
                           filename: str | None = None,
                           alt_text: str | None = None,
                           title: str | None = None) -> dict[str, Any]:
    if not bool(getattr(settings, "allow_external_image_urls", False)):
        raise ValueError("External image URL uploads are disabled by ALLOW_EXTERNAL_IMAGE_URLS=false.")
    _validate_external_url(url)
    timeout = httpx.Timeout(float(getattr(settings, "http_timeout_seconds", 30.0)))
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=bool(getattr(settings, "verify_tls", True)),
        follow_redirects=True,
    ) as http:
        response = await http.get(url, headers={"User-Agent": "WearInstinct-MCP/1.0"})
        response.raise_for_status()
        final_url = str(response.url)
        _validate_external_url(final_url)
        raw = response.content
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not filename:
        filename = urlparse(final_url).path.split("/")[-1] or None
    return await _upload_bytes(
        client, raw, filename, alt_text=alt_text, title=title,
        declared_mime=content_type or None,
    )


async def _find_duplicate_product(client: Any, name: str, language: str,
                                  slug: str | None = None) -> dict[str, Any] | None:
    rows = await client.woo(
        "GET", "products",
        params={"lang": language, "status": "any", "search": name, "per_page": 100},
    )
    for row in rows:
        if str(row.get("name") or "").strip().casefold() == name.strip().casefold():
            return row
        if slug and str(row.get("slug") or "").strip().casefold() == slug.strip().casefold():
            return row
    return None


def register_product_tools(mcp: Any, client: Any, policy: Any, audit: Any, settings: Any) -> None:
    @mcp.tool(
        title="Upload a product image from base64",
        description=(
            "Upload one PNG, JPEG, or WebP image to the WordPress Media Library from base64. "
            "Use this for conversation-generated or locally supplied product images. Writes default to dry_run=true."
        ),
        annotations=WRITE,
    )
    async def media_upload_base64(
        data: str,
        filename: str | None = None,
        alt_text: str | None = None,
        title: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        action = "media_upload_base64"
        try:
            raw, mime_type, safe_name, width, height = _decode_base64_image(data, filename)
            preview = {
                "filename": safe_name, "mime_type": mime_type, "bytes": len(raw),
                "width": width, "height": height, "alt_text": alt_text, "title": title,
            }
            if dry_run:
                _audit(audit, action, dry_run=True, payload=preview)
                return {"ok": True, "dry_run": True, **preview}
            policy.require_write()
            uploaded = await _upload_bytes(
                client, raw, safe_name, alt_text=alt_text, title=title, declared_mime=mime_type
            )
            _audit(audit, action, target=uploaded["id"], dry_run=False, payload=preview, result=uploaded)
            return {"ok": True, "dry_run": False, "media": uploaded}
        except Exception as exc:
            _audit(audit, action, dry_run=dry_run, error=str(exc))
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Upload a product image from URL",
        description=(
            "Upload one external PNG, JPEG, or WebP image to WordPress. "
            "Requires ALLOW_EXTERNAL_IMAGE_URLS=true. Writes default to dry_run=true."
        ),
        annotations=WRITE,
    )
    async def media_upload_from_url(
        url: str,
        filename: str | None = None,
        alt_text: str | None = None,
        title: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        action = "media_upload_from_url"
        try:
            _validate_external_url(url)
            if dry_run:
                return {
                    "ok": True, "dry_run": True, "url": url, "filename": filename,
                    "alt_text": alt_text, "title": title,
                    "external_urls_enabled": bool(getattr(settings, "allow_external_image_urls", False)),
                }
            policy.require_write()
            uploaded = await _upload_from_url(
                client, settings, url, filename=filename, alt_text=alt_text, title=title
            )
            _audit(audit, action, target=uploaded["id"], dry_run=False, payload={"url": url}, result=uploaded)
            return {"ok": True, "dry_run": False, "media": uploaded}
        except Exception as exc:
            _audit(audit, action, dry_run=dry_run, error=str(exc))
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Create a new WooCommerce product",
        description=(
            "Create a brand-new simple or variable WooCommerce source product. "
            "Validates WPML language, category IDs, image IDs, and global attributes before writing. "
            "Use product_create_complete when images, sizes and variations should be created together."
        ),
        annotations=WRITE,
    )
    async def product_create(
        name: str,
        language: str = "en",
        type: str = "simple",
        slug: str | None = None,
        description: str = "",
        short_description: str = "",
        status: str = "draft",
        sku: str | None = None,
        regular_price: str | None = None,
        sale_price: str | None = None,
        category_ids: list[int] | None = None,
        image_ids: list[int] | None = None,
        featured_image_id: int | None = None,
        attributes: list[dict[str, Any]] | None = None,
        meta_data: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        action = "product_create"
        try:
            product_type = str(type or "simple").strip().lower()
            if product_type not in {"simple", "variable"}:
                raise ValueError("type must be 'simple' or 'variable'.")
            if status not in {"draft", "publish", "private"}:
                raise ValueError("status must be draft, publish, or private.")
            await _validate_language(client, language)
            cats = await _validate_categories(client, category_ids, language)
            ids = [int(x) for x in (image_ids or [])]
            if featured_image_id is not None:
                fid = int(featured_image_id)
                ids = [fid] + [x for x in ids if x != fid]
            await _validate_media_ids(client, ids)
            normalized_attrs: list[dict[str, Any]] = []
            for row in attributes or []:
                aid = int(row.get("id") or 0)
                options = [str(x) for x in list(row.get("options") or [])]
                if aid:
                    attr = await _validate_attribute(client, aid, options, language)
                    normalized_attrs.append({
                        "id": aid,
                        "name": row.get("name") or attr.get("name"),
                        "position": int(row.get("position") or len(normalized_attrs)),
                        "visible": bool(row.get("visible", True)),
                        "variation": bool(row.get("variation", False)),
                        "options": options,
                    })
                else:
                    if not row.get("name"):
                        raise ValueError("Local attributes require a name.")
                    normalized_attrs.append({
                        "name": str(row["name"]),
                        "position": int(row.get("position") or len(normalized_attrs)),
                        "visible": bool(row.get("visible", True)),
                        "variation": bool(row.get("variation", False)),
                        "options": options,
                    })
            meta_payload = []
            if meta_data:
                meta_map = {str(x.get("key")): x.get("value") for x in meta_data if x.get("key")}
                meta_payload = policy.validate_meta_patch(meta_map)
            duplicate = await _find_duplicate_product(client, name, language, slug)
            if duplicate:
                raise ValueError(f"A product with this name/slug already exists: ID {duplicate.get('id')}")
            payload: dict[str, Any] = {
                "name": name,
                "type": product_type,
                "status": status,
                "description": description,
                "short_description": short_description,
                "lang": language,
                "categories": [{"id": int(x["id"])} for x in cats],
            }
            if slug:
                payload["slug"] = slug
            if sku:
                payload["sku"] = sku
            if ids:
                payload["images"] = [{"id": x} for x in ids]
            if normalized_attrs:
                payload["attributes"] = normalized_attrs
                payload["default_attributes"] = []
            if meta_payload:
                payload["meta_data"] = meta_payload
            if product_type == "simple":
                if regular_price is not None:
                    payload["regular_price"] = str(regular_price)
                if sale_price is not None:
                    payload["sale_price"] = str(sale_price)
            if dry_run:
                _audit(audit, action, dry_run=True, payload=payload)
                return {"ok": True, "dry_run": True, "payload": payload}
            policy.require_write()
            if regular_price is not None or sale_price is not None:
                policy.require_price_write()
            if status == "publish":
                policy.require_publish()
            created = await client.woo("POST", "products", json=payload)
            _audit(audit, action, target=created.get("id"), dry_run=False, payload=payload, result={"id": created.get("id")})
            return {"ok": True, "dry_run": False, "product": _pick(created)}
        except Exception as exc:
            _audit(audit, action, dry_run=dry_run, error=str(exc))
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Update product images",
        description="Replace a product's featured/gallery image order using validated WordPress media IDs.",
        annotations=WRITE,
    )
    async def product_update_images(
        product_id: Annotated[int, Field(gt=0)],
        image_ids: list[int],
        featured_image_id: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        action = "product_update_images"
        try:
            await client.woo("GET", f"products/{product_id}")
            ids = [int(x) for x in image_ids]
            if featured_image_id is not None:
                fid = int(featured_image_id)
                ids = [fid] + [x for x in ids if x != fid]
            await _validate_media_ids(client, ids)
            payload = {"images": [{"id": x} for x in ids]}
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "payload": payload}
            policy.require_write()
            updated = await client.woo("PUT", f"products/{product_id}", json=payload)
            return {"ok": True, "dry_run": False, "product": _pick(updated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Update product categories",
        description="Replace a WooCommerce product's categories after validating each category ID.",
        annotations=WRITE,
    )
    async def product_update_categories(
        product_id: Annotated[int, Field(gt=0)],
        category_ids: list[int],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            product = await client.woo("GET", f"products/{product_id}")
            language = str(product.get("lang") or "") or None
            cats = await _validate_categories(client, category_ids, language)
            payload = {"categories": [{"id": int(x["id"])} for x in cats]}
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "categories": [{"id": x.get("id"), "name": x.get("name")} for x in cats]}
            policy.require_write()
            updated = await client.woo("PUT", f"products/{product_id}", json=payload)
            return {"ok": True, "dry_run": False, "product": _pick(updated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Update variable product attributes",
        description="Set WooCommerce product attributes after validating global attribute IDs and options.",
        annotations=WRITE,
    )
    async def product_update_attributes(
        product_id: Annotated[int, Field(gt=0)],
        attributes: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            product = await client.woo("GET", f"products/{product_id}")
            language = str(product.get("lang") or "") or None
            rows: list[dict[str, Any]] = []
            for row in attributes:
                aid = int(row.get("id") or 0)
                options = [str(x) for x in list(row.get("options") or [])]
                if aid:
                    attr = await _validate_attribute(client, aid, options, language)
                    rows.append({
                        "id": aid, "name": row.get("name") or attr.get("name"),
                        "position": int(row.get("position") or len(rows)),
                        "visible": bool(row.get("visible", True)),
                        "variation": bool(row.get("variation", False)),
                        "options": options,
                    })
                else:
                    if not row.get("name"):
                        raise ValueError("Local attributes require a name.")
                    rows.append({
                        "name": str(row["name"]),
                        "position": int(row.get("position") or len(rows)),
                        "visible": bool(row.get("visible", True)),
                        "variation": bool(row.get("variation", False)),
                        "options": options,
                    })
            payload = {"type": "variable", "attributes": rows, "default_attributes": []}
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "payload": payload}
            policy.require_write()
            updated = await client.woo("PUT", f"products/{product_id}", json=payload)
            return {"ok": True, "dry_run": False, "product": _pick(updated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Create a product variation",
        description="Create one WooCommerce variation after validating its parent and variation attributes.",
        annotations=WRITE,
    )
    async def product_variation_create(
        product_id: Annotated[int, Field(gt=0)],
        attributes: list[dict[str, Any]],
        regular_price: str,
        sale_price: str | None = None,
        sku: str | None = None,
        stock_status: str = "instock",
        manage_stock: bool = False,
        stock_quantity: int | None = None,
        image_id: int | None = None,
        status: str = "publish",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            parent = await client.woo("GET", f"products/{product_id}")
            if parent.get("type") != "variable":
                raise ValueError("Parent product must be variable.")
            existing = await client.woo("GET", f"products/{product_id}/variations", params={"per_page": 100})
            normalized = []
            for row in attributes:
                aid = int(row.get("id") or 0)
                option = str(row.get("option") or "")
                if aid:
                    await _validate_attribute(client, aid, [option], str(parent.get("lang") or "") or None)
                    normalized.append({"id": aid, "option": option})
                elif row.get("name"):
                    normalized.append({"name": str(row["name"]), "option": option})
                else:
                    raise ValueError("Variation attributes require id or name.")
            wanted = sorted((int(x.get("id") or 0), str(x.get("name") or "").casefold(), str(x.get("option") or "").casefold()) for x in normalized)
            for var in existing:
                have = sorted((int(x.get("id") or 0), str(x.get("name") or "").casefold(), str(x.get("option") or "").casefold()) for x in (var.get("attributes") or []))
                if have == wanted:
                    raise ValueError(f"Duplicate variation already exists: ID {var.get('id')}")
            if image_id is not None:
                await _validate_media_ids(client, [int(image_id)])
            payload: dict[str, Any] = {
                "status": status, "regular_price": str(regular_price),
                "stock_status": stock_status, "manage_stock": bool(manage_stock),
                "attributes": normalized,
            }
            if sale_price is not None:
                payload["sale_price"] = str(sale_price)
            if sku:
                payload["sku"] = sku
            if stock_quantity is not None:
                payload["stock_quantity"] = int(stock_quantity)
            if image_id is not None:
                payload["image"] = {"id": int(image_id)}
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "payload": payload}
            policy.require_write()
            policy.require_price_write()
            created = await client.woo("POST", f"products/{product_id}/variations", json=payload)
            return {"ok": True, "dry_run": False, "variation": created}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Bulk create product variations",
        description="Create a small batch of non-duplicate WooCommerce product variations, such as S/M/L.",
        annotations=WRITE,
    )
    async def product_variations_bulk_create(
        product_id: Annotated[int, Field(gt=0)],
        variations: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            policy.validate_bulk_size(len(variations))
            parent = await client.woo("GET", f"products/{product_id}")
            if parent.get("type") != "variable":
                raise ValueError("Parent product must be variable.")
            language = str(parent.get("lang") or "") or None
            existing = await client.woo("GET", f"products/{product_id}/variations", params={"per_page": 100})
            signatures = {
                tuple(sorted((int(x.get("id") or 0), str(x.get("name") or "").casefold(), str(x.get("option") or "").casefold()) for x in (v.get("attributes") or [])))
                for v in existing
            }
            plans: list[dict[str, Any]] = []
            for item in variations:
                attrs: list[dict[str, Any]] = []
                for row in list(item.get("attributes") or []):
                    aid = int(row.get("id") or 0)
                    option = str(row.get("option") or "")
                    if aid:
                        await _validate_attribute(client, aid, [option], language)
                        attrs.append({"id": aid, "option": option})
                    elif row.get("name"):
                        attrs.append({"name": str(row["name"]), "option": option})
                    else:
                        raise ValueError("Variation attributes require id or name.")
                sig = tuple(sorted((int(x.get("id") or 0), str(x.get("name") or "").casefold(), str(x.get("option") or "").casefold()) for x in attrs))
                if sig in signatures:
                    raise ValueError(f"Duplicate variation attributes: {attrs}")
                signatures.add(sig)
                if not item.get("regular_price"):
                    raise ValueError("Each variation requires regular_price.")
                payload: dict[str, Any] = {
                    "status": item.get("status") or "publish",
                    "regular_price": str(item["regular_price"]),
                    "stock_status": item.get("stock_status") or "instock",
                    "manage_stock": bool(item.get("manage_stock", False)),
                    "attributes": attrs,
                }
                if item.get("sale_price") is not None:
                    payload["sale_price"] = str(item.get("sale_price"))
                if item.get("sku"):
                    payload["sku"] = str(item.get("sku"))
                if item.get("stock_quantity") is not None:
                    payload["stock_quantity"] = int(item.get("stock_quantity"))
                if item.get("image_id") is not None:
                    await _validate_media_ids(client, [int(item.get("image_id"))])
                    payload["image"] = {"id": int(item.get("image_id"))}
                plans.append(payload)
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "variations": plans}
            policy.require_write()
            policy.require_price_write()
            created: list[dict[str, Any]] = []
            for payload in plans:
                row = await client.woo("POST", f"products/{product_id}/variations", json=payload)
                created.append(row)
            return {"ok": True, "dry_run": False, "created_count": len(created), "variations": created}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Update product metadata",
        description=(
            "Update only allowlisted WooCommerce product metadata keys while preserving all other metadata. "
            "The allowlist is controlled by ALLOWED_META_KEYS."
        ),
        annotations=WRITE,
    )
    async def product_update_meta(
        product_id: Annotated[int, Field(gt=0)],
        meta_data: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        try:
            await client.woo("GET", f"products/{product_id}")
            meta_map = {str(x.get("key")): x.get("value") for x in meta_data if x.get("key")}
            payload = {"meta_data": policy.validate_meta_patch(meta_map)}
            if dry_run:
                return {"ok": True, "dry_run": True, "product_id": product_id, "payload": payload}
            policy.require_write()
            updated = await client.woo("PUT", f"products/{product_id}", json=payload)
            return {"ok": True, "dry_run": False, "product": _pick(updated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    @mcp.tool(
        title="Create a complete WooCommerce product",
        description=(
            "Preferred action for creating a new WooCommerce product with content, categories, product images, "
            "Size variations and pricing in one workflow. Supports image_ids, base64 conversation images, and "
            "external image URLs when enabled. Validates all IDs before writing and defaults to dry_run=true."
        ),
        annotations=HIGH,
    )
    async def product_create_complete(
        name: str,
        language: str = "en",
        description: str = "",
        short_description: str = "",
        slug: str | None = None,
        status: str = "draft",
        category_ids: list[int] | None = None,
        image_ids: list[int] | None = None,
        images: list[dict[str, Any]] | None = None,
        featured_image_index: int = 0,
        sizes: list[str] | None = None,
        size_attribute_id: int = 3,
        regular_price: str = "",
        sale_price: str | None = None,
        sku: str | None = None,
        meta_data: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        action = "product_create_complete"
        uploaded: list[dict[str, Any]] = []
        created_variations: list[dict[str, Any]] = []
        product_id: int | None = None
        completed_steps: list[str] = []
        try:
            if status not in {"draft", "publish", "private"}:
                raise ValueError("status must be draft, publish, or private.")
            if not regular_price:
                raise ValueError("regular_price is required.")
            await _validate_language(client, language)
            completed_steps.append("validated_language")
            cats = await _validate_categories(client, category_ids, language)
            completed_steps.append("validated_categories")

            normalized_sizes = [str(x).strip().upper() for x in (sizes or []) if str(x).strip()]
            if normalized_sizes:
                if len(set(normalized_sizes)) != len(normalized_sizes):
                    raise ValueError("Duplicate sizes are not allowed.")
                await _validate_attribute(client, int(size_attribute_id), normalized_sizes, language)
                completed_steps.append("validated_size_attribute")

            existing_ids = [int(x) for x in (image_ids or [])]
            await _validate_media_ids(client, existing_ids)
            completed_steps.append("validated_existing_images")
            duplicate = await _find_duplicate_product(client, name, language, slug)
            if duplicate:
                raise ValueError(f"A product with this name/slug already exists: ID {duplicate.get('id')}")

            image_previews: list[dict[str, Any]] = []
            for idx, spec in enumerate(images or []):
                filename = spec.get("filename") or f"{slug or re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-').lower()}-{idx + 1}.png"
                if spec.get("base64"):
                    raw, mime_type, safe_name, width, height = _decode_base64_image(str(spec["base64"]), filename)
                    image_previews.append({
                        "source": "base64", "filename": safe_name, "mime_type": mime_type,
                        "bytes": len(raw), "width": width, "height": height,
                        "alt_text": spec.get("alt_text"),
                    })
                elif spec.get("url"):
                    _validate_external_url(str(spec["url"]))
                    image_previews.append({
                        "source": "url", "url": str(spec["url"]), "filename": filename,
                        "alt_text": spec.get("alt_text"),
                    })
                else:
                    raise ValueError("Each image must contain either 'base64' or 'url'.")

            product_type = "variable" if normalized_sizes else "simple"
            parent_preview: dict[str, Any] = {
                "name": name,
                "type": product_type,
                "status": status,
                "description": description,
                "short_description": short_description,
                "lang": language,
                "categories": [{"id": int(x["id"])} for x in cats],
            }
            if slug:
                parent_preview["slug"] = slug
            if sku:
                parent_preview["sku"] = sku
            if normalized_sizes:
                parent_preview["attributes"] = [{
                    "id": int(size_attribute_id),
                    "position": 0,
                    "visible": True,
                    "variation": True,
                    "options": normalized_sizes,
                }]
                parent_preview["default_attributes"] = []
            else:
                parent_preview["regular_price"] = str(regular_price)
                if sale_price is not None:
                    parent_preview["sale_price"] = str(sale_price)
            if meta_data:
                meta_map = {str(x.get("key")): x.get("value") for x in meta_data if x.get("key")}
                parent_preview["meta_data"] = policy.validate_meta_patch(meta_map)

            variation_preview = [
                {
                    "status": "publish",
                    "regular_price": str(regular_price),
                    **({"sale_price": str(sale_price)} if sale_price is not None else {}),
                    "manage_stock": False,
                    "stock_status": "instock",
                    "attributes": [{"id": int(size_attribute_id), "option": size}],
                }
                for size in normalized_sizes
            ]

            if dry_run:
                _audit(audit, action, dry_run=True, payload={
                    "product": parent_preview, "existing_image_ids": existing_ids,
                    "images": image_previews, "variations": variation_preview,
                })
                return {
                    "ok": True, "dry_run": True,
                    "product_payload": parent_preview,
                    "existing_image_ids": existing_ids,
                    "image_uploads": image_previews,
                    "variations": variation_preview,
                    "categories": [{"id": x.get("id"), "name": x.get("name")} for x in cats],
                }

            policy.require_write()
            policy.require_price_write()
            if status == "publish":
                policy.require_publish()

            final_image_ids = list(existing_ids)
            for idx, spec in enumerate(images or []):
                filename = spec.get("filename") or f"{slug or re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-').lower()}-{idx + 1}.png"
                if spec.get("base64"):
                    raw, mime_type, safe_name, _, _ = _decode_base64_image(str(spec["base64"]), filename)
                    media = await _upload_bytes(
                        client, raw, safe_name,
                        alt_text=spec.get("alt_text") or name,
                        title=spec.get("title") or name,
                        declared_mime=mime_type,
                    )
                else:
                    media = await _upload_from_url(
                        client, settings, str(spec["url"]), filename=filename,
                        alt_text=spec.get("alt_text") or name,
                        title=spec.get("title") or name,
                    )
                uploaded.append(media)
                final_image_ids.append(int(media["id"]))
            completed_steps.append("uploaded_images")

            if final_image_ids:
                if featured_image_index < 0 or featured_image_index >= len(final_image_ids):
                    raise ValueError("featured_image_index is outside the final image list.")
                fid = final_image_ids[featured_image_index]
                final_image_ids = [fid] + [x for i, x in enumerate(final_image_ids) if i != featured_image_index]
                parent_preview["images"] = [{"id": x} for x in final_image_ids]

            created = await client.woo("POST", "products", json=parent_preview)
            product_id = int(created["id"])
            completed_steps.append("created_parent_product")

            for payload in variation_preview:
                row = await client.woo("POST", f"products/{product_id}/variations", json=payload)
                created_variations.append({
                    "id": row.get("id"),
                    "attributes": row.get("attributes"),
                    "regular_price": row.get("regular_price"),
                    "sale_price": row.get("sale_price"),
                    "stock_status": row.get("stock_status"),
                })
            if variation_preview:
                completed_steps.append("created_variations")

            final_product = await client.woo("GET", f"products/{product_id}")
            result = {
                "product_id": product_id,
                "media_ids": final_image_ids,
                "uploaded_media": uploaded,
                "variation_ids": [x.get("id") for x in created_variations],
            }
            _audit(audit, action, target=product_id, dry_run=False, payload={
                "name": name, "language": language, "category_ids": category_ids,
                "sizes": normalized_sizes, "regular_price": regular_price, "sale_price": sale_price,
            }, result=result)
            return {
                "ok": True, "dry_run": False, "completed_steps": completed_steps,
                "product": _pick(final_product),
                "uploaded_media": uploaded,
                "created_variations": created_variations,
            }
        except Exception as exc:
            _audit(audit, action, target=product_id, dry_run=dry_run, error=str(exc))
            return {
                "ok": False,
                "dry_run": dry_run,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "completed_steps": completed_steps,
                "partial": {
                    "product_id": product_id,
                    "uploaded_media": uploaded,
                    "created_variations": created_variations,
                },
            }
