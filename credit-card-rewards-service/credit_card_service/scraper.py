from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib import robotparser

from . import db
from .extract import PARSER_VERSION, extract
from .util import utc_now

USER_AGENT = "CreditCardRewardsService/0.1 (+operator-maintained; contact configured by operator)"
MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 3


class ScrapeError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Scraper:
    def __init__(self, allow_private_hosts: bool = False, timeout: float = 10.0, *, clock=time.monotonic, sleeper=time.sleep) -> None:
        self.allow_private_hosts = allow_private_hosts
        self.timeout = timeout
        self._last_by_host: dict[str, float] = {}
        self._opener = build_opener(_NoRedirect())
        self._clock = clock
        self._sleep = sleeper

    def _check_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ScrapeError("only plain configured HTTP/HTTPS URLs are allowed")
        if self.allow_private_hosts:
            return
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ScrapeError(f"DNS resolution failed: {exc}") from exc
        if not addresses:
            raise ScrapeError("DNS resolution returned no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise ScrapeError("refusing loopback/private/link-local/reserved target; use --allow-private-hosts only for trusted local testing")

    def _pace(self, url: str, request_delay: float) -> None:
        """Keep starts of requests to each host at least delay seconds apart."""
        host = urlparse(url).netloc.lower()
        previous = self._last_by_host.get(host)
        if previous is not None:
            remaining = request_delay - (self._clock() - previous)
            if remaining > 0:
                self._sleep(remaining)
        self._last_by_host[host] = self._clock()

    def _request(self, url: str, max_bytes: int = MAX_RESPONSE_BYTES, request_delay: float = 0):
        self._check_url(url)
        self._pace(url, request_delay)
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ScrapeError(f"transient network failure: {exc}") from exc
        try:
            status = response.code if isinstance(response, HTTPError) else response.status
            headers = {name.lower(): value for name, value in response.headers.items()}
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ScrapeError("response exceeds configured size cap")
            return status, headers, data
        except (OSError, TimeoutError) as exc:
            raise ScrapeError(f"transient network failure: {exc}") from exc
        finally:
            response.close()

    def _fetch(self, url: str, max_bytes: int = MAX_RESPONSE_BYTES, request_delay: float = 0) -> tuple[str, dict[str, str], bytes]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            status, headers, body = self._request(current, max_bytes, request_delay)
            if status in (301, 302, 303, 307, 308):
                target = headers.get("location")
                if not target:
                    raise ScrapeError("redirect without Location")
                current = urljoin(current, target)
                self._check_url(current)
                continue
            if status < 200 or status >= 300:
                raise ScrapeError(f"HTTP status {status}")
            return current, headers, body
        raise ScrapeError("too many redirects")

    def _robots_allowed(self, page_url: str, request_delay: float) -> bool:
        parsed = urlparse(page_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            _, headers, body = self._fetch(robots_url, 64_000, request_delay)
        except ScrapeError as exc:
            # A missing robots file is represented as HTTP 404 and intentionally allowed below only if explicit.
            if "HTTP status 404" in str(exc):
                return True
            raise ScrapeError(f"robots.txt unavailable: {exc}") from exc
        content_type = (headers.get("content-type") or "").lower()
        if "text" not in content_type and "robots" not in content_type:
            raise ScrapeError("robots.txt has unsupported content type")
        rp = robotparser.RobotFileParser()
        rp.parse(body.decode("utf-8", errors="replace").splitlines())
        return rp.can_fetch(USER_AGENT, page_url)

    def scrape_source(self, source: dict, db_path: str) -> dict:
        run_id = db.start_run(db_path, source["source_id"])
        try:
            delay = float(source.get("request_delay_seconds", 0))
            if not self._robots_allowed(source["page_url"], delay):
                raise ScrapeError("robots.txt disallows this configured path")
            last_error = None
            for attempt in range(3):
                try:
                    final_url, headers, body = self._fetch(source["page_url"], request_delay=delay)
                    break
                except ScrapeError as exc:
                    last_error = exc
                    if not any(word in str(exc) for word in ("HTTP status 429", "HTTP status 500", "HTTP status 502", "HTTP status 503", "HTTP status 504", "DNS", "timed out", "transient network")) or attempt == 2:
                        raise
                    self._sleep(0.2 * (2 ** attempt))
            else:
                raise last_error or ScrapeError("fetch failed")
            content_type = (headers.get("content-type") or "").lower()
            if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
                raise ScrapeError("unsupported content type; expected HTML")
            html = body.decode("utf-8", errors="replace")
            facts = extract(source, html)
            record = {"card_id": source["card_id"], "issuer": source["issuer"], "name": source["name"], "source_url": final_url,
                **facts, "provenance": {"source_id": source["source_id"], "fetched_at": utc_now(), "content_sha256": hashlib.sha256(body).hexdigest(), "parser_version": PARSER_VERSION},
                "status": "success", "error": None}
            db.finish_success(db_path, run_id, record)
            return {"source_id": source["source_id"], "run_id": run_id, "status": "success", "card_id": record["card_id"]}
        except Exception as exc:
            db.finish_failure(db_path, run_id, str(exc))
            return {"source_id": source["source_id"], "run_id": run_id, "status": "failed", "error": str(exc)}


def scrape_sources(sources: list[dict], db_path: str, allow_private_hosts: bool = False) -> list[dict]:
    scraper = Scraper(allow_private_hosts=allow_private_hosts)
    return [scraper.scrape_source(source, db_path) for source in sources]
