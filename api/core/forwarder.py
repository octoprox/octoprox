"""HTTP request forwarder through managed proxies."""

import time
from typing import TYPE_CHECKING

import httpx
import structlog

from api.core.config import settings
from api.models.proxy import Proxy, ProxyProtocol

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()


class ForwardingError(Exception):
    """Error during request forwarding."""
    
    def __init__(self, message: str, status_code: int = 502, proxy_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.proxy_id = proxy_id


class ProxyForwarder:
    """Forwards HTTP requests through managed proxies."""
    
    def __init__(self, proxy_manager: "ProxyManager") -> None:
        self._proxy_manager = proxy_manager
        self._timeout = httpx.Timeout(
            connect=settings.connection_timeout,
            read=60.0,
            write=60.0,
            pool=10.0,
        )
    
    def _get_proxy_url(self, proxy: Proxy) -> str:
        """Get the proxy URL for httpx."""
        if proxy.username and proxy.password:
            return f"{proxy.protocol.value}://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
        return f"{proxy.protocol.value}://{proxy.host}:{proxy.port}"
    
    def _get_proxy_mounts(self, proxy: Proxy) -> dict[str, httpx.AsyncHTTPTransport]:
        """Create proxy mounts for httpx client."""
        proxy_url = self._get_proxy_url(proxy)
        
        if proxy.protocol in (ProxyProtocol.HTTP, ProxyProtocol.HTTPS):
            transport = httpx.AsyncHTTPTransport(proxy=proxy_url)
            return {
                "http://": transport,
                "https://": transport,
            }
        else:
            # SOCKS proxies require httpx-socks extension
            # For now, only support HTTP/HTTPS proxies
            raise ForwardingError(
                f"Unsupported proxy protocol: {proxy.protocol}",
                status_code=501,
                proxy_id=proxy.id,
            )
    
    async def forward(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        session_id: str | None = None,
    ) -> httpx.Response:
        """Forward a request through a managed proxy.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: Target URL to request
            headers: Optional request headers
            content: Optional request body
            session_id: Optional session ID for sticky routing
            
        Returns:
            The response from the target server
            
        Raises:
            ForwardingError: If no proxy is available or request fails
        """
        # Select a proxy
        proxy = self._proxy_manager.select_proxy(session_id)
        if not proxy:
            raise ForwardingError("No healthy proxies available", status_code=503)
        
        logger.debug(
            "Forwarding request",
            method=method,
            url=url,
            proxy_id=proxy.id,
            proxy_host=proxy.host,
        )
        
        start_time = time.monotonic()
        success = False
        
        try:
            mounts = self._get_proxy_mounts(proxy)
            
            async with httpx.AsyncClient(
                mounts=mounts,
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=content,
                )
                success = True
                return response
                
        except httpx.TimeoutException as e:
            logger.warning(
                "Proxy request timeout",
                proxy_id=proxy.id,
                url=url,
                error=str(e),
            )
            raise ForwardingError(
                f"Request timeout through proxy {proxy.host}:{proxy.port}",
                status_code=504,
                proxy_id=proxy.id,
            ) from e
            
        except httpx.ConnectError as e:
            logger.warning(
                "Proxy connection error",
                proxy_id=proxy.id,
                url=url,
                error=str(e),
            )
            raise ForwardingError(
                f"Connection error through proxy {proxy.host}:{proxy.port}",
                status_code=502,
                proxy_id=proxy.id,
            ) from e
            
        except httpx.HTTPError as e:
            logger.warning(
                "Proxy HTTP error",
                proxy_id=proxy.id,
                url=url,
                error=str(e),
            )
            raise ForwardingError(
                f"HTTP error through proxy {proxy.host}:{proxy.port}: {e}",
                status_code=502,
                proxy_id=proxy.id,
            ) from e
            
        finally:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._proxy_manager.update_proxy_stats(proxy.id, success, latency_ms)

    async def forward_with_retry(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        session_id: str | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """Forward a request with automatic retry on failure.

        Args:
            method: HTTP method
            url: Target URL
            headers: Optional request headers
            content: Optional request body
            session_id: Optional session ID for sticky routing
            max_retries: Maximum retry attempts (defaults to settings.max_retries)

        Returns:
            The response from the target server

        Raises:
            ForwardingError: If all retries fail
        """
        retries = max_retries if max_retries is not None else settings.max_retries
        last_error: ForwardingError | None = None

        for attempt in range(retries + 1):
            try:
                return await self.forward(
                    method=method,
                    url=url,
                    headers=headers,
                    content=content,
                    session_id=session_id,
                )
            except ForwardingError as e:
                last_error = e
                logger.warning(
                    "Forward attempt failed",
                    attempt=attempt + 1,
                    max_retries=retries,
                    error=str(e),
                )

                # Don't retry on certain errors
                if e.status_code in (501, 503):
                    raise

        # All retries exhausted
        raise last_error or ForwardingError("All retry attempts failed", status_code=502)

