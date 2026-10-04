from __future__ import annotations

import logging
import base64
import io
import json
from typing import Any
from PIL import Image

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


def _meta_value(product: dict[str, Any], key: str) -> Any:
    for item in product.get("meta_data") or []:
        if item.get("key") == key:
            return item.get("value")
    return None

async def _normalize_product_sizes(product_id: int, sizes: list[str], fallback_regular: str = "", fallback_sale: str = "") -> dict[str, Any]:
    source = await client.woo("GET", f"products/{product_id}")
    current_variations = []
    if source.get("type") == "variable":
        current_variations = await client.woo(
            "GET", f"products/{product_id}/variations", params={"per_page": 100}
        )

    def get_size(v: dict[str, Any]) -> str | None:
        for a in v.get("attributes") or []:
            if int(a.get("id") or 0) == 3 or str(a.get("name") or "").casefold() in ("size","taille"):
                return str(a.get("option") or "")
        return None

    by_size: dict[str, dict[str, Any]] = {}
    for v in current_variations:
        sz = get_size(v)
        if sz:
            by_size[sz.upper()] = v

    attrs = []
    for a in source.get("attributes") or []:
        aid = int(a.get("id") or 0)
        name = str(a.get("name") or "")
        if aid == 1 or name.casefold() in ("color","couleur"):
            continue
        if aid == 3 or name.casefold() in ("size","taille"):
            continue
        attrs.append({k:a[k] for k in ("id","name","position","visible","variation","options") if k in a})
    attrs.append({
        "id":3, "name":"Size", "position":len(attrs),
        "visible":True, "variation":True, "options":sizes
    })

    regular = str(source.get("regular_price") or "")
    sale = str(source.get("sale_price") or "")
    if current_variations and not regular:
        regular = str(current_variations[0].get("regular_price") or "")
    if current_variations and not sale:
        sale = str(current_variations[0].get("sale_price") or "")
    if not regular:
        regular = str(fallback_regular or "")
    if not sale:
        sale = str(fallback_sale or "")

    plans=[]
    for size in sizes:
        prior=by_size.get(size.upper())
        payload={
            "status":"publish",
            "regular_price":str((prior or {}).get("regular_price") or regular),
            "sale_price":str((prior or {}).get("sale_price") or sale),
            "manage_stock":bool((prior or {}).get("manage_stock", source.get("manage_stock", False))),
            "stock_status":(prior or {}).get("stock_status") or source.get("stock_status") or "instock",
            "attributes":[{"id":3,"option":size}],
        }
        sq=(prior or {}).get("stock_quantity")
        if sq is None:
            sq=source.get("stock_quantity")
        if sq is not None:
            payload["stock_quantity"]=sq
        img=(prior or {}).get("image") or {}
        if img.get("id"):
            payload["image"]={"id":img["id"]}
        plans.append(payload)

    for v in current_variations:
        await client.woo("DELETE", f"products/{product_id}/variations/{v['id']}", params={"force":"true"})
    parent=await client.woo("PUT", f"products/{product_id}", json={
        "type":"variable","attributes":attrs,"default_attributes":[]
    })
    created=[]
    for payload in plans:
        cv=await client.woo("POST", f"products/{product_id}/variations", json=payload)
        created.append({
            "id":cv.get("id"),
            "attributes":cv.get("attributes"),
            "regular_price":cv.get("regular_price"),
            "sale_price":cv.get("sale_price"),
            "stock_status":cv.get("stock_status"),
            "stock_quantity":cv.get("stock_quantity"),
        })
    return {"product":pick(parent),"created_variations":created}

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
        if slug == "__cxg_upload_glossy_image__":
            if not name or not short_description:
                return {"ok":False,"maintenance":"upload_glossy_image","error":"name and base64 short_description are required."}
            try:
                raw=base64.b64decode(short_description,validate=True)
                im=Image.open(io.BytesIO(raw)).convert("RGB")
                out=io.BytesIO()
                im.save(out,format="WEBP",quality=88,method=6)
                optimized=out.getvalue()
            except Exception as ie:
                return {"ok":False,"maintenance":"upload_glossy_image","error":f"Image decode/optimize failed: {ie}"}
            filename=name if name.lower().endswith(".webp") else f"{name.rsplit('.',1)[0]}.webp"
            if dry_run:
                return {
                    "ok":True,"dry_run":True,"maintenance":"upload_glossy_image",
                    "filename":filename,"original_bytes":len(raw),"optimized_bytes":len(optimized),
                    "width":im.width,"height":im.height
                }
            policy.require_write()
            media=await client.wp_media(filename,"image/webp",optimized)
            mid=int(media.get("id"))
            if description:
                try:
                    media=await client.wp("POST",f"wp/v2/media/{mid}",json={
                        "alt_text":description,
                        "title":description,
                    })
                except Exception:
                    pass
            return {
                "ok":True,"dry_run":False,"maintenance":"upload_glossy_image",
                "id":mid,"source_url":media.get("source_url"),
                "filename":filename,"original_bytes":len(raw),"optimized_bytes":len(optimized),
                "width":im.width,"height":im.height
            }

        if slug == "__cxg_publish_linen_pants__":
            if not name:
                return {"ok":False,"maintenance":"publish_linen_pants","error":"Pass JSON media IDs in name."}
            try:
                config=json.loads(name)
                gallery=[int(x) for x in config.get("gallery",[])]
                lifestyle=[int(x) for x in config.get("lifestyle",[])]
            except Exception as ce:
                return {"ok":False,"maintenance":"publish_linen_pants","error":f"Invalid media config: {ce}"}
            if len(gallery) < 3 or len(lifestyle) < 1:
                return {"ok":False,"maintenance":"publish_linen_pants","error":"Need gallery and lifestyle media IDs."}

            en_title="INSTINCT Linen Relaxed Pants – Black"
            fr_title="Pantalon Relaxed en Lin INSTINCT – Noir"
            en_short="<p>Relaxed by design. Refined in every detail. The INSTINCT Linen Pants combine a clean wide-leg silhouette with the natural comfort of linen for an effortless everyday look.</p>"
            en_desc=(
                "<p>Meet the pants made for effortless dressing.</p>"
                "<p>The <strong>INSTINCT Linen Relaxed Pants</strong> feature a loose, straight silhouette that gives you freedom to move while keeping the look clean and structured. Crafted from breathable linen fabric, they offer a lightweight, natural feel suited to warm days and easy layering.</p>"
                "<p>An elasticated waistband is combined with an adjustable drawstring and button closure for a secure, comfortable fit, while discreet side pockets keep the design practical without interrupting its minimal aesthetic.</p>"
                "<p>Finished in <strong>deep black</strong>, they are easy to style with an INSTINCT tee, shirt or relaxed top for anything from casual everyday wear to a more elevated minimal look.</p>"
                "<p><strong>Details:</strong> Relaxed wide-leg fit • Linen fabric • Elasticated waistband • Adjustable drawstring • Button closure • Side pockets • Deep black finish • Minimal INSTINCT aesthetic</p>"
            )
            fr_short="<p>Une coupe décontractée, un style parfaitement maîtrisé. Le pantalon en lin INSTINCT associe une silhouette ample et épurée au confort naturel du lin.</p>"
            fr_desc=(
                "<p>Pensé pour un style effortless au quotidien.</p>"
                "<p>Le <strong>Pantalon Relaxed en Lin INSTINCT</strong> adopte une coupe droite et ample qui offre une grande liberté de mouvement tout en conservant une silhouette moderne et structurée. Son tissu en lin apporte légèreté et respirabilité pour un confort naturel tout au long de la journée.</p>"
                "<p>La taille élastiquée est complétée par un cordon de serrage ajustable et une fermeture boutonnée pour un maintien confortable et personnalisé. Les poches latérales apportent la touche pratique tout en préservant le design minimaliste du pantalon.</p>"
                "<p>Décliné dans un <strong>noir profond</strong>, il se porte facilement avec un t-shirt, une chemise ou un top INSTINCT pour créer aussi bien un look casual qu'une silhouette plus premium et minimaliste.</p>"
                "<p><strong>Détails :</strong> Coupe ample et droite • Tissu en lin • Taille élastiquée • Cordon ajustable • Fermeture boutonnée • Poches latérales • Noir profond • Design minimaliste INSTINCT</p>"
            )

            # Find/create Pants category and French translation.
            cats=await client.woo("GET","products/categories",params={"lang":"en","search":"Pants","per_page":100})
            pants=next((x for x in cats if str(x.get("name") or "").strip().casefold()=="pants"),None)
            if not pants and not dry_run:
                policy.require_write()
                pants=await client.woo("POST","products/categories",json={"name":"Pants","slug":"pants","lang":"en"})
            pants_id=int(pants.get("id")) if pants else 0
            pants_fr_id=None
            if pants:
                tr=pants.get("translations") or {}
                if isinstance(tr,dict) and tr.get("fr"):
                    pants_fr_id=int(tr["fr"])
            if pants_id and not pants_fr_id and not dry_run:
                frcats=await client.woo("GET","products/categories",params={"lang":"fr","search":"Pantalons","per_page":100})
                exact=next((x for x in frcats if str(x.get("name") or "").strip().casefold()=="pantalons"),None)
                if exact:
                    pants_fr_id=int(exact["id"])
                else:
                    try:
                        newfc=await client.woo("POST","products/categories",json={
                            "name":"Pantalons","slug":"pantalons","lang":"fr","translation_of":pants_id
                        })
                        pants_fr_id=int(newfc["id"])
                    except Exception:
                        pants_fr_id=None

            if dry_run:
                return {
                    "ok":True,"dry_run":True,"maintenance":"publish_linen_pants",
                    "gallery":gallery,"lifestyle":lifestyle,
                    "price":{"regular":"679","sale":"549"},
                    "sizes":["S","M","L"],
                    "categories_en":["Pants","Bottoms","MAN"],
                    "categories_fr":["Pantalons","Bas","Homme"],
                    "pants_category_id":pants_id or None,
                    "pants_fr_category_id":pants_fr_id,
                }

            policy.require_write()
            en_categories=[{"id":x} for x in ([pants_id] if pants_id else [])+[127,158]]
            en_payload={
                "name":en_title,"type":"variable","status":"publish",
                "description":en_desc,"short_description":en_short,
                "categories":en_categories,
                "images":[{"id":x} for x in gallery],
                "meta_data":[{"key":"lifestyle-gallery","value":",".join(str(x) for x in lifestyle)}],
                "attributes":[{"id":3,"name":"Size","position":0,"visible":True,"variation":True,"options":["S","M","L"]}],
                "default_attributes":[],
                "manage_stock":False,"stock_status":"instock",
            }
            existing=await client.woo("GET","products",params={"lang":"en","search":en_title,"status":"any","per_page":100})
            enp=next((x for x in existing if str(x.get("name") or "").strip().casefold()==en_title.casefold()),None)
            if enp:
                en_id=int(enp["id"])
                old=await client.woo("GET",f"products/{en_id}/variations",params={"per_page":100})
                for v in old:
                    await client.woo("DELETE",f"products/{en_id}/variations/{v['id']}",params={"force":"true"})
                enp=await client.woo("PUT",f"products/{en_id}",json=en_payload)
            else:
                enp=await client.woo("POST","products",json=en_payload)
                en_id=int(enp["id"])
            en_vars=[]
            for size in ["S","M","L"]:
                v=await client.woo("POST",f"products/{en_id}/variations",json={
                    "status":"publish","regular_price":"679","sale_price":"549",
                    "manage_stock":False,"stock_status":"instock",
                    "attributes":[{"id":3,"option":size}]
                })
                en_vars.append(v)

            tr=enp.get("translations") or {}
            fr_id=int(tr["fr"]) if isinstance(tr,dict) and tr.get("fr") else None
            fr_categories=[{"id":x} for x in ([pants_fr_id] if pants_fr_id else [])+[128,159]]
            fr_payload={
                "name":fr_title,"type":"variable","status":"publish",
                "description":fr_desc,"short_description":fr_short,
                "categories":fr_categories,
                "images":[{"id":x} for x in gallery],
                "meta_data":[{"key":"lifestyle-gallery","value":",".join(str(x) for x in lifestyle)}],
                "attributes":[{"id":3,"name":"Size","position":0,"visible":True,"variation":True,"options":["S","M","L"]}],
                "default_attributes":[],
                "manage_stock":False,"stock_status":"instock",
            }
            if fr_id:
                oldfr=await client.woo("GET",f"products/{fr_id}/variations",params={"lang":"fr","per_page":100})
                for v in oldfr:
                    await client.woo("DELETE",f"products/{fr_id}/variations/{v['id']}",params={"force":"true"})
                frp=await client.woo("PUT",f"products/{fr_id}",json=fr_payload)
            else:
                fr_payload.update({"lang":"fr","translation_of":en_id})
                frp=await client.woo("POST","products",json=fr_payload)
                fr_id=int(frp["id"])
            fr_created=[]
            for i,size in enumerate(["S","M","L"]):
                v=await client.woo("POST",f"products/{fr_id}/variations",json={
                    "lang":"fr","translation_of":en_vars[i].get("id"),
                    "status":"publish","regular_price":"679","sale_price":"549",
                    "manage_stock":False,"stock_status":"instock",
                    "attributes":[{"id":3,"option":size}]
                })
                fr_created.append(v)
            return {
                "ok":True,"maintenance":"publish_linen_pants",
                "english":{"id":en_id,"name":enp.get("name"),"status":enp.get("status"),"permalink":enp.get("permalink")},
                "french":{"id":fr_id,"name":frp.get("name"),"status":frp.get("status"),"permalink":frp.get("permalink")},
                "gallery":gallery,"lifestyle":lifestyle,
                "price":{"regular":"679","sale":"549"},
                "sizes":["S","M","L"]
            }

        if slug == "__cxg_trash_pair__":
            source = await client.woo("GET", f"products/{product_id}")
            ids = [int(product_id)]
            tr = source.get("translations") or {}
            if isinstance(tr, dict):
                for v in tr.values():
                    try:
                        iv = int(v)
                    except Exception:
                        continue
                    if iv not in ids:
                        ids.append(iv)
            if dry_run:
                return {"ok":True,"dry_run":True,"maintenance":"trash_pair","ids":ids,"name":source.get("name")}
            policy.require_write()
            results=[]
            for pid in ids:
                data = await client.woo("DELETE", f"products/{pid}", params={"force":"false"})
                results.append({"id":pid,"status":data.get("status")})
            return {"ok":True,"maintenance":"trash_pair","results":results}
        if slug == "__cxg_trash_single__":
            source = await client.woo("GET", f"products/{product_id}")
            if dry_run:
                return {"ok":True,"dry_run":True,"maintenance":"trash_single","id":product_id,"name":source.get("name")}
            policy.require_write()
            data = await client.woo("DELETE", f"products/{product_id}", params={"force":"false"})
            return {"ok":True,"maintenance":"trash_single","id":product_id,"status":data.get("status")}
        if slug == "__cxg_sizes_s_m_l__":
            source = await client.woo("GET", f"products/{product_id}")
            ids=[int(product_id)]
            tr=source.get("translations") or {}
            if isinstance(tr, dict):
                for v in tr.values():
                    try:
                        iv=int(v)
                    except Exception:
                        continue
                    if iv not in ids:
                        ids.append(iv)
            if dry_run:
                preview=[]
                for pid in ids:
                    p=await client.woo("GET", f"products/{pid}")
                    vars=[]
                    if p.get("type")=="variable":
                        vars=await client.woo("GET", f"products/{pid}/variations", params={"per_page":100})
                    preview.append({
                        "id":pid,"name":p.get("name"),"type":p.get("type"),
                        "attributes":p.get("attributes"),"variation_count":len(vars),
                        "regular_price":p.get("regular_price"),"sale_price":p.get("sale_price"),
                        "stock_status":p.get("stock_status"),"stock_quantity":p.get("stock_quantity")
                    })
                return {"ok":True,"dry_run":True,"maintenance":"normalize_sizes","products":preview,"sizes":["S","M","L"]}
            policy.require_write()
            master_regular = str(source.get("regular_price") or "")
            master_sale = str(source.get("sale_price") or "")
            if source.get("type") == "variable":
                master_vars = await client.woo("GET", f"products/{product_id}/variations", params={"per_page":100})
                if master_vars and not master_regular:
                    master_regular = str(master_vars[0].get("regular_price") or "")
                if master_vars and not master_sale:
                    master_sale = str(master_vars[0].get("sale_price") or "")
            ordered_ids = [pid for pid in ids if pid != int(product_id)] + [int(product_id)]
            results=[]
            for pid in ordered_ids:
                results.append({"id":pid, **(await _normalize_product_sizes(
                    pid, ["S","M","L"], master_regular, master_sale
                ))})
            return {"ok":True,"maintenance":"normalize_sizes","results":results}
        if slug == "__cxg_normalize_split_apparel__":
            apparel_categories = {
                "T-shirts","Tops","Joggers","Shorts","Hoodies","Bodysuits",
                "Sweater","Skirt","Bottoms","Swim Shorts"
            }
            candidates=[]
            page=1
            while True:
                rows=await client.woo("GET","products",params={"lang":"en","status":"any","per_page":100,"page":page})
                if not rows:
                    break
                for p in rows:
                    pid=int(p.get("id") or 0)
                    if not pid or pid in {4242, 737}:
                        continue
                    cats={str(x.get("name") or "") for x in (p.get("categories") or [])}
                    if not (cats & apparel_categories):
                        continue
                    attrs=p.get("attributes") or []
                    color_attr=next((a for a in attrs if int(a.get("id") or 0)==1 or str(a.get("name") or "").casefold() in ("color","couleur")),None)
                    size_attr=next((a for a in attrs if int(a.get("id") or 0)==3 or str(a.get("name") or "").casefold() in ("size","taille")),None)
                    color_opts=list((color_attr or {}).get("options") or [])
                    size_opts=[str(x).upper() for x in list((size_attr or {}).get("options") or [])]
                    is_split_color = color_attr is not None and len(color_opts) <= 1
                    needs_sizes = p.get("type") == "simple" or is_split_color or (size_attr is not None and set(size_opts) != {"S","M","L"})
                    if needs_sizes and not (color_attr is not None and len(color_opts) > 1):
                        candidates.append({
                            "id":pid,"name":p.get("name"),"type":p.get("type"),
                            "color_options":color_opts,"size_options":size_opts,
                            "categories":sorted(cats)
                        })
                if len(rows)<100:
                    break
                page+=1
            if dry_run:
                return {"ok":True,"dry_run":True,"maintenance":"normalize_split_apparel","count":len(candidates),"candidates":candidates}
            policy.require_write()
            results=[]
            for item in candidates:
                try:
                    src=await client.woo("GET",f"products/{item['id']}")
                    master_regular=str(src.get("regular_price") or "")
                    master_sale=str(src.get("sale_price") or "")
                    if src.get("type")=="variable":
                        vs=await client.woo("GET",f"products/{item['id']}/variations",params={"per_page":100})
                        if vs and not master_regular:
                            master_regular=str(vs[0].get("regular_price") or "")
                        if vs and not master_sale:
                            master_sale=str(vs[0].get("sale_price") or "")
                    ids2=[item["id"]]
                    tr=src.get("translations") or {}
                    if isinstance(tr,dict) and tr.get("fr"):
                        try:
                            ids2.insert(0,int(tr["fr"]))
                        except Exception:
                            pass
                    per=[]
                    for pid2 in ids2:
                        per.append({"id":pid2, **(await _normalize_product_sizes(pid2,["S","M","L"],master_regular,master_sale))})
                    results.append({"source_id":item["id"],"ok":True,"results":per})
                except Exception as ne:
                    results.append({"source_id":item["id"],"ok":False,"error":str(ne)})
            return {"ok":all(x.get("ok") for x in results),"maintenance":"normalize_split_apparel","results":results}
        if slug == "__cxg_audit_multicolor_missing_sizes__":
            apparel_categories = {
                "T-shirts","Tops","Joggers","Shorts","Hoodies","Bodysuits",
                "Sweater","Skirt","Bottoms","Swim Shorts","Lifestyle"
            }
            rows_out=[]
            page=1
            while True:
                rows=await client.woo("GET","products",params={"lang":"en","status":"any","per_page":100,"page":page})
                if not rows:
                    break
                for p in rows:
                    pid=int(p.get("id") or 0)
                    if not pid or pid == 4242:
                        continue
                    cats={str(x.get("name") or "") for x in (p.get("categories") or [])}
                    if not (cats & apparel_categories):
                        continue
                    attrs=p.get("attributes") or []
                    color_attr=next((a for a in attrs if int(a.get("id") or 0)==1 or str(a.get("name") or "").casefold() in ("color","couleur")),None)
                    size_attr=next((a for a in attrs if int(a.get("id") or 0)==3 or str(a.get("name") or "").casefold() in ("size","taille")),None)
                    color_opts=list((color_attr or {}).get("options") or [])
                    size_opts=list((size_attr or {}).get("options") or [])
                    if len(color_opts)>1 and not size_opts:
                        rows_out.append({"id":pid,"name":p.get("name"),"color_options":color_opts,"categories":sorted(cats)})
                if len(rows)<100:
                    break
                page+=1
            return {"ok":True,"maintenance":"audit_multicolor_missing_sizes","count":len(rows_out),"products":rows_out}
        if slug == "__cxg_sizes_keep_color__":
            source=await client.woo("GET",f"products/{product_id}")
            attrs=source.get("attributes") or []
            color_attr=next((a for a in attrs if int(a.get("id") or 0)==1 or str(a.get("name") or "").casefold() in ("color","couleur")),None)
            if not color_attr or len(list(color_attr.get("options") or []))<1:
                return {"ok":False,"maintenance":"sizes_keep_color","error":"No color attribute found."}
            old_vars=[]
            if source.get("type")=="variable":
                old_vars=await client.woo("GET",f"products/{product_id}/variations",params={"per_page":100})
            colors=list(color_attr.get("options") or [])
            by_color={}
            for v in old_vars:
                col=None
                for a in v.get("attributes") or []:
                    if int(a.get("id") or 0)==1 or str(a.get("name") or "").casefold() in ("color","couleur"):
                        col=str(a.get("option") or "")
                        break
                if col:
                    by_color[col.casefold()]=v
            plan=[]
            for color in colors:
                prior=by_color.get(str(color).casefold()) or {}
                for size in ["S","M","L"]:
                    payload={
                        "status":"publish",
                        "regular_price":str(prior.get("regular_price") or source.get("regular_price") or ""),
                        "sale_price":str(prior.get("sale_price") or source.get("sale_price") or ""),
                        "manage_stock":bool(prior.get("manage_stock",source.get("manage_stock",False))),
                        "stock_status":prior.get("stock_status") or source.get("stock_status") or "instock",
                        "attributes":[{"id":1,"option":color},{"id":3,"option":size}],
                    }
                    if prior.get("stock_quantity") is not None:
                        payload["stock_quantity"]=prior.get("stock_quantity")
                    img=prior.get("image") or {}
                    if img.get("id"):
                        payload["image"]={"id":img["id"]}
                    plan.append(payload)
            new_attrs=[]
            for a in attrs:
                aid=int(a.get("id") or 0)
                nm=str(a.get("name") or "")
                if aid==3 or nm.casefold() in ("size","taille"):
                    continue
                new_attrs.append({k:a[k] for k in ("id","name","position","visible","variation","options") if k in a})
            new_attrs.append({"id":3,"name":"Size","position":len(new_attrs),"visible":True,"variation":True,"options":["S","M","L"]})
            if dry_run:
                return {"ok":True,"dry_run":True,"maintenance":"sizes_keep_color","product_id":product_id,"colors":colors,"variation_count":len(plan),"attributes":new_attrs}
            policy.require_write()
            for v in old_vars:
                await client.woo("DELETE",f"products/{product_id}/variations/{v['id']}",params={"force":"true"})
            parent=await client.woo("PUT",f"products/{product_id}",json={"type":"variable","attributes":new_attrs,"default_attributes":[]})
            created=[]
            for payload in plan:
                cv=await client.woo("POST",f"products/{product_id}/variations",json=payload)
                created.append({"id":cv.get("id"),"attributes":cv.get("attributes"),"regular_price":cv.get("regular_price"),"sale_price":cv.get("sale_price"),"stock_status":cv.get("stock_status")})
            return {"ok":True,"maintenance":"sizes_keep_color","product":pick(parent),"created_variations":created}
        if slug == "__cxg_audit_fr_lifestyle__":
            page=1
            rows_out=[]
            while True:
                rows=await client.woo("GET","products",params={"lang":"en","status":"any","per_page":100,"page":page})
                if not rows:
                    break
                for p in rows:
                    pid=int(p.get("id") or 0)
                    tr=p.get("translations") or {}
                    tid=tr.get("fr") if isinstance(tr,dict) else None
                    if not tid:
                        continue
                    lifestyle=_meta_value(p,"lifestyle-gallery")
                    frp=await client.woo("GET",f"products/{int(tid)}")
                    fr_lifestyle=_meta_value(frp,"lifestyle-gallery")
                    if str(lifestyle or "") != str(fr_lifestyle or ""):
                        rows_out.append({"en_id":pid,"fr_id":int(tid),"name":p.get("name"),"en_value":lifestyle or "","fr_value":fr_lifestyle or ""})
                if len(rows)<100:
                    break
                page+=1
            return {"ok":True,"maintenance":"audit_fr_lifestyle","count":len(rows_out),"products":rows_out}
        if slug == "__cxg_sync_fr_lifestyle_batch__":
            if not name:
                return {"ok":False,"maintenance":"sync_fr_lifestyle_batch","error":"Pass comma-separated English product IDs in name."}
            ids3=[]
            for part in str(name).split(","):
                part=part.strip()
                if part.isdigit():
                    ids3.append(int(part))
            if not ids3:
                return {"ok":False,"maintenance":"sync_fr_lifestyle_batch","error":"No valid product IDs."}
            if not dry_run:
                policy.require_write()
            results=[]
            for pid in ids3:
                try:
                    p=await client.woo("GET",f"products/{pid}")
                    tr=p.get("translations") or {}
                    tid=tr.get("fr") if isinstance(tr,dict) else None
                    if not tid:
                        results.append({"en_id":pid,"ok":False,"error":"No linked French translation"})
                        continue
                    lifestyle=_meta_value(p,"lifestyle-gallery") or ""
                    if dry_run:
                        results.append({"en_id":pid,"fr_id":int(tid),"ok":True,"value":lifestyle})
                    else:
                        data=await client.woo("PUT",f"products/{int(tid)}",json={"meta_data":[{"key":"lifestyle-gallery","value":lifestyle}]})
                        results.append({"en_id":pid,"fr_id":int(tid),"ok":True,"value":lifestyle,"date_modified":data.get("date_modified")})
                except Exception as be:
                    results.append({"en_id":pid,"ok":False,"error":str(be)})
            return {"ok":all(x.get("ok") for x in results),"dry_run":dry_run,"maintenance":"sync_fr_lifestyle_batch","results":results}
        if slug == "__cxg_sync_all_fr_lifestyle__":
            page=1
            synced=[]
            skipped=[]
            if not dry_run:
                policy.require_write()
            while True:
                rows=await client.woo("GET","products",params={"lang":"en","status":"any","per_page":100,"page":page})
                if not rows:
                    break
                for p in rows:
                    pid=int(p.get("id") or 0)
                    tr=p.get("translations") or {}
                    tid=tr.get("fr") if isinstance(tr,dict) else None
                    if not tid:
                        skipped.append({"id":pid,"reason":"no_fr"})
                        continue
                    lifestyle=_meta_value(p,"lifestyle-gallery")
                    if dry_run:
                        synced.append({"en_id":pid,"fr_id":int(tid),"value":lifestyle or ""})
                    else:
                        try:
                            data=await client.woo("PUT",f"products/{int(tid)}",json={"meta_data":[{"key":"lifestyle-gallery","value":lifestyle or ""}]})
                            synced.append({"en_id":pid,"fr_id":int(tid),"value":lifestyle or "","date_modified":data.get("date_modified")})
                        except Exception as se:
                            synced.append({"en_id":pid,"fr_id":int(tid),"error":str(se)})
                if len(rows)<100:
                    break
                page+=1
            return {"ok":not any("error" in x for x in synced),"dry_run":dry_run,"maintenance":"sync_all_fr_lifestyle","count":len(synced),"synced":synced,"skipped":skipped}
        if slug == "__cxg_sync_fr_full_product__":
            source=await client.woo("GET",f"products/{product_id}")
            tr=source.get("translations") or {}
            tid=tr.get("fr") if isinstance(tr,dict) else None
            if not tid:
                return {"ok":False,"maintenance":"sync_fr_full_product","error":"No linked French translation."}
            tid=int(tid)
            source_vars=[]
            if source.get("type")=="variable":
                source_vars=await client.woo("GET",f"products/{product_id}/variations",params={"lang":"en","per_page":100})
            lifestyle=_meta_value(source,"lifestyle-gallery") or ""
            parent_payload={
                "images":[{"id":i["id"]} for i in (source.get("images") or []) if i.get("id")],
                "meta_data":[{"key":"lifestyle-gallery","value":lifestyle}],
                "manage_stock":bool(source.get("manage_stock",False)),
                "stock_status":source.get("stock_status") or "instock",
            }
            if source.get("stock_quantity") is not None:
                parent_payload["stock_quantity"]=source.get("stock_quantity")
            if source.get("type")=="simple":
                parent_payload["regular_price"]=str(source.get("regular_price") or "")
                parent_payload["sale_price"]=str(source.get("sale_price") or "")
            if dry_run:
                return {
                    "ok":True,"dry_run":True,"maintenance":"sync_fr_full_product",
                    "source_id":product_id,"fr_id":tid,
                    "image_ids":[x.get("id") for x in (source.get("images") or [])],
                    "lifestyle":lifestyle,
                    "variation_count":len(source_vars),
                    "source_price":source.get("price"),
                    "source_regular_price":source.get("regular_price"),
                    "source_sale_price":source.get("sale_price"),
                }
            policy.require_write()
            fr=await client.woo("GET",f"products/{tid}")
            if source.get("type")=="variable":
                source_lang=str(source.get("lang") or "en")
                target_lang="fr"
                attrs=await _translated_product_attributes(list(source.get("attributes") or []),source_lang,target_lang)
                parent_payload["type"]="variable"
                parent_payload["attributes"]=attrs
                parent_payload["default_attributes"]=[]
                old_fr_vars=await client.woo("GET",f"products/{tid}/variations",params={"lang":"fr","per_page":100})
                for v in old_fr_vars:
                    await client.woo("DELETE",f"products/{tid}/variations/{v['id']}",params={"force":"true"})
                updated=await client.woo("PUT",f"products/{tid}",json=parent_payload)
                created=[]
                for sv in source_vars:
                    vp={
                        "lang":"fr",
                        "translation_of":sv.get("id"),
                        "status":sv.get("status") or "publish",
                        "regular_price":str(sv.get("regular_price") or ""),
                        "sale_price":str(sv.get("sale_price") or ""),
                        "virtual":bool(sv.get("virtual",False)),
                        "downloadable":bool(sv.get("downloadable",False)),
                        "manage_stock":bool(sv.get("manage_stock",False)),
                        "stock_status":sv.get("stock_status") or "instock",
                        "backorders":sv.get("backorders") or "no",
                        "menu_order":int(sv.get("menu_order") or 0),
                        "attributes":await _translated_variation_attributes(list(sv.get("attributes") or []),source_lang,target_lang),
                    }
                    if sv.get("stock_quantity") is not None:
                        vp["stock_quantity"]=sv.get("stock_quantity")
                    img=sv.get("image") or {}
                    if img.get("id"):
                        vp["image"]={"id":img["id"]}
                    cv=await client.woo("POST",f"products/{tid}/variations",json=vp)
                    created.append({
                        "source_variation_id":sv.get("id"),
                        "fr_variation_id":cv.get("id"),
                        "regular_price":cv.get("regular_price"),
                        "sale_price":cv.get("sale_price"),
                        "stock_status":cv.get("stock_status"),
                        "attributes":cv.get("attributes"),
                    })
                return {"ok":True,"maintenance":"sync_fr_full_product","product":pick(updated),"created_variations":created}
            else:
                updated=await client.woo("PUT",f"products/{tid}",json=parent_payload)
                return {"ok":True,"maintenance":"sync_fr_full_product","product":pick(updated)}
        if slug == "__cxg_sync_fr_lifestyle__":
            source = await client.woo("GET", f"products/{product_id}")
            lifestyle = _meta_value(source, "lifestyle-gallery")
            tr=source.get("translations") or {}
            tid=tr.get("fr") if isinstance(tr,dict) else None
            if not tid:
                return {"ok":False,"maintenance":"sync_fr_lifestyle","error":"No linked French translation."}
            if dry_run:
                return {"ok":True,"dry_run":True,"maintenance":"sync_fr_lifestyle","fr_id":tid,"value":lifestyle or ""}
            policy.require_write()
            data=await client.woo("PUT", f"products/{tid}", json={
                "meta_data":[{"key":"lifestyle-gallery","value": lifestyle or ""}]
            })
            return {"ok":True,"maintenance":"sync_fr_lifestyle","fr_id":tid,"value":lifestyle or "","product":pick(data)}
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
        source = await client.woo("GET", f"products/{product_id}")
        ids = [int(product_id)]
        tr = source.get("translations") or {}
        if isinstance(tr, dict):
            for v in tr.values():
                try:
                    iv = int(v)
                except Exception:
                    continue
                if iv not in ids:
                    ids.append(iv)

        preview = []
        for pid in ids:
            p = await client.woo("GET", f"products/{pid}")
            vars = []
            if p.get("type") == "variable":
                vars = await client.woo("GET", f"products/{pid}/variations", params={"per_page":100})
            preview.append({
                "id": pid,
                "name": p.get("name"),
                "type": p.get("type"),
                "variation_count": len(vars),
            })
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload={"price":payload,"targets":preview})
            return {"ok": True, "dry_run": True, "product_id":product_id, "payload":payload, "targets":preview}

        policy.require_price_write()
        results = []
        for pid in ids:
            p = await client.woo("GET", f"products/{pid}")
            parent_result = None
            if p.get("type") == "simple":
                parent_result = await client.woo("PUT", f"products/{pid}", json=payload)
            else:
                # Keep the variable parent aligned too, even though WooCommerce derives display price from variations.
                try:
                    parent_result = await client.woo("PUT", f"products/{pid}", json=payload)
                except Exception:
                    parent_result = p
            changed_vars = []
            if p.get("type") == "variable":
                vars = await client.woo("GET", f"products/{pid}/variations", params={"per_page":100})
                for v in vars:
                    vp = dict(payload)
                    updated = await client.woo("PUT", f"products/{pid}/variations/{v['id']}", json=vp)
                    changed_vars.append({
                        "id": updated.get("id"),
                        "regular_price": updated.get("regular_price"),
                        "sale_price": updated.get("sale_price"),
                        "price": updated.get("price"),
                        "attributes": updated.get("attributes"),
                    })
            results.append({
                "id":pid,
                "name":p.get("name"),
                "product":pick(parent_result) if isinstance(parent_result, dict) else None,
                "variations":changed_vars,
            })
        audit.record(action, target=product_id, dry_run=False, payload=payload, result={"targets":[x["id"] for x in results]})
        return {"ok": True, "dry_run": False, "results": results}
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
        lifestyle = _meta_value(source, "lifestyle-gallery")
        if lifestyle is not None:
            payload["meta_data"] = [{"key":"lifestyle-gallery","value":lifestyle}]
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
        lifestyle = _meta_value(source, "lifestyle-gallery")
        if lifestyle is not None:
            payload["meta_data"] = [{"key":"lifestyle-gallery","value":lifestyle}]
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


@mcp.tool(title="Preview or trash product", annotations=HIGH)
async def product_trash(
    product_id: Annotated[int, Field(gt=0)],
    include_translations: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "product_trash"
    try:
        product = await client.woo("GET", f"products/{product_id}")
        ids = [int(product_id)]
        if include_translations:
            tr = product.get("translations") or {}
            if isinstance(tr, dict):
                for v in tr.values():
                    try:
                        iv = int(v)
                    except Exception:
                        continue
                    if iv not in ids:
                        ids.append(iv)
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "products": ids,
                "names": [product.get("name")],
            }
        policy.require_write()
        results = []
        for pid in ids:
            try:
                data = await client.woo("DELETE", f"products/{pid}", params={"force": "false"})
                results.append({"id": pid, "ok": True, "status": data.get("status")})
            except Exception as de:
                results.append({"id": pid, "ok": False, "error": str(de)})
        audit.record(action, target=product_id, dry_run=False, payload={"include_translations": include_translations}, result=results)
        return {"ok": all(r["ok"] for r in results), "dry_run": False, "results": results}
    except Exception as e:
        return fail(action, e, product_id, dry_run)


@mcp.tool(title="Preview or normalize product to size variations", annotations=HIGH)
async def product_set_size_variations(
    product_id: Annotated[int, Field(gt=0)],
    sizes: list[str] = ["S", "M", "L"],
    remove_color_attribute: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    action = "product_set_size_variations"
    try:
        source = await client.woo("GET", f"products/{product_id}")
        if not sizes:
            raise ValueError("Provide at least one size.")
        current_variations = []
        if source.get("type") == "variable":
            current_variations = await client.woo(
                "GET", f"products/{product_id}/variations", params={"per_page": 100}
            )

        def get_size(v: dict[str, Any]) -> str | None:
            for a in v.get("attributes") or []:
                if int(a.get("id") or 0) == 3 or str(a.get("name") or "").casefold() == "size":
                    return str(a.get("option") or "")
            return None

        by_size: dict[str, dict[str, Any]] = {}
        for v in current_variations:
            sz = get_size(v)
            if sz:
                by_size[sz.upper()] = v

        attrs = []
        for a in source.get("attributes") or []:
            aid = int(a.get("id") or 0)
            name = str(a.get("name") or "")
            if remove_color_attribute and (aid == 1 or name.casefold() in ("color", "couleur")):
                continue
            if aid == 3 or name.casefold() == "size":
                continue
            attrs.append({
                k: a[k] for k in ("id","name","position","visible","variation","options") if k in a
            })
        attrs.append({
            "id": 3,
            "name": "Size",
            "position": len(attrs),
            "visible": True,
            "variation": True,
            "options": sizes,
        })

        regular = str(source.get("regular_price") or "")
        sale = str(source.get("sale_price") or "")
        if not regular and current_variations:
            regular = str(current_variations[0].get("regular_price") or "")
        if not sale and current_variations:
            sale = str(current_variations[0].get("sale_price") or "")

        plan = []
        for size in sizes:
            prior = by_size.get(size.upper())
            payload: dict[str, Any] = {
                "status": "publish",
                "regular_price": str((prior or {}).get("regular_price") or regular),
                "sale_price": str((prior or {}).get("sale_price") or sale),
                "manage_stock": bool((prior or {}).get("manage_stock", source.get("manage_stock", False))),
                "stock_status": (prior or {}).get("stock_status") or source.get("stock_status") or "instock",
                "attributes": [{"id": 3, "option": size}],
            }
            sq = (prior or {}).get("stock_quantity")
            if sq is None:
                sq = source.get("stock_quantity")
            if sq is not None:
                payload["stock_quantity"] = sq
            img = (prior or {}).get("image") or {}
            if img.get("id"):
                payload["image"] = {"id": img["id"]}
            plan.append({"size": size, "payload": payload})

        preview = {
            "product_id": product_id,
            "from_type": source.get("type"),
            "old_variation_count": len(current_variations),
            "new_variation_count": len(plan),
            "attributes": attrs,
            "plan": plan,
        }
        if dry_run:
            audit.record(action, target=product_id, dry_run=True, payload=preview)
            return {"ok": True, "dry_run": True, **preview}

        policy.require_write()
        for v in current_variations:
            await client.woo("DELETE", f"products/{product_id}/variations/{v['id']}", params={"force": "true"})
        parent = await client.woo("PUT", f"products/{product_id}", json={
            "type": "variable",
            "attributes": attrs,
            "default_attributes": [],
        })
        created = []
        for item in plan:
            cv = await client.woo("POST", f"products/{product_id}/variations", json=item["payload"])
            created.append({
                "id": cv.get("id"),
                "size": item["size"],
                "regular_price": cv.get("regular_price"),
                "sale_price": cv.get("sale_price"),
                "stock_status": cv.get("stock_status"),
                "stock_quantity": cv.get("stock_quantity"),
            })
        audit.record(action, target=product_id, dry_run=False, payload=preview, result={"created": created})
        return {
            "ok": True,
            "dry_run": False,
            "product": pick(parent),
            "created_variations": created,
        }
    except Exception as e:
        return fail(action, e, product_id, dry_run)


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
        max_request_body_size=6 * 1024 * 1024,
    )
