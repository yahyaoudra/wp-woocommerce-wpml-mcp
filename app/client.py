from __future__ import annotations

from typing import Any
import httpx

from .config import Settings
from .errors import RemoteAPIError


class WordPressClient:
    def __init__(self, s: Settings):
        self.s = s
        self.timeout = httpx.Timeout(s.http_timeout_seconds)

    async def _decode(self, response: httpx.Response) -> Any:
        try:
            data = response.json()
        except Exception:
            data = response.text[:4000]
        if response.is_error:
            if isinstance(data, dict):
                message = data.get("message") or data.get("code") or response.reason_phrase
            else:
                message = str(data)
            raise RemoteAPIError(
                f"WordPress API error {response.status_code}: {message}",
                status_code=response.status_code,
                details=data,
            )
        return data

    async def woo(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                  json: Any = None) -> Any:
        url = f"{self.s.base_url}/wp-json/wc/v3/{path.lstrip('/')}"
        auth = httpx.BasicAuth(self.s.wc_consumer_key, self.s.wc_consumer_secret)
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.s.verify_tls, follow_redirects=True) as client:
            r = await client.request(method, url, params=params, json=json, auth=auth)
        return await self._decode(r)

    async def wp(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json: Any = None) -> Any:
        if not self.s.wp_username or not self.s.wp_app_password:
            raise RemoteAPIError("WP_USERNAME and WP_APP_PASSWORD are required for this WordPress endpoint.")
        url = f"{self.s.base_url}/wp-json/{path.lstrip('/')}"
        auth = httpx.BasicAuth(self.s.wp_username, self.s.wp_app_password)
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.s.verify_tls, follow_redirects=True) as client:
            r = await client.request(method, url, params=params, json=json, auth=auth)
        return await self._decode(r)

    async def wp_media(self, filename: str, mime_type: str, content: bytes) -> Any:
        if not self.s.wp_username or not self.s.wp_app_password:
            raise RemoteAPIError("WP_USERNAME and WP_APP_PASSWORD are required for media upload.")
        url = f"{self.s.base_url}/wp-json/wp/v2/media"
        auth = httpx.BasicAuth(self.s.wp_username, self.s.wp_app_password)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mime_type,
        }
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.s.verify_tls, follow_redirects=True) as client:
            r = await client.post(url, content=content, headers=headers, auth=auth)
        return await self._decode(r)

    async def root(self) -> Any:
        url = f"{self.s.base_url}/wp-json/"
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.s.verify_tls, follow_redirects=True) as client:
            r = await client.get(url)
        return await self._decode(r)
