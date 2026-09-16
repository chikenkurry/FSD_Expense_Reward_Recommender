from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import ipaddress
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib import robotparser

from . import db
from .extract import PARSER_VERSION, extract
from .util import utc_now

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 (CardCatalogue/1.0)"
MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 3

SMART_EXCLUDE_TERMS = (
    "login", "terms", "faq", "apply", "privacy", "contact", ".pdf",
    "comparator", "compare", "checklist", "rates-fees", "fees", "announcements",
    "news", "promotion", "promotions", "deals", "offers", "services", "card-services",
    "credit-limit", "waive", "payment", "smart-pay", "bill", "alerts",
    "merchant", "activation", "overseas", "customer-service", "supplementary",
    "contest", "privileges", "campaigns", "card-rewards", "statement", "forms",
)

SMART_EXCLUDE_SLUGS = {
    "default", "index", "cards", "credit-cards", "credit-card",
    "debit-cards", "debit-card", "all-cards",
}


class ScrapeError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _LinkExtractor(HTMLParser):
    """Zero-dependency HTML anchor tag extractor for bank directory and hub pages."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._current_href = urljoin(self.base_url, href)
                self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None and data.strip():
            self._current_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            title = " ".join(part for part in self._current_text if part).strip()
            self.links.append({"url": self._current_href, "title": title})
            self._current_href = None
            self._current_text = []


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
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf"})
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, TimeoutError, OSError) as exc:
            err_str = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in err_str or "self-signed certificate" in err_str:
                try:
                    insecure_opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl._create_unverified_context()))
                    try:
                        response = insecure_opener.open(request, timeout=self.timeout)
                    except HTTPError as http_exc:
                        response = http_exc
                except Exception as inner_exc:
                    raise ScrapeError(f"transient network failure: {inner_exc}") from inner_exc
            else:
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
            # Missing robots file, redirects, or WAF challenges allow crawling by default (RFC 9309)
            err_str = str(exc)
            if any(k in err_str for k in ("HTTP status 404", "HTTP status 403", "HTTP status 30", "Redirect", "too many redirects", "rejected")):
                return True
            raise ScrapeError(f"robots.txt unavailable: {exc}") from exc
        content_type = (headers.get("content-type") or "").lower()
        if "text" not in content_type and "robots" not in content_type:
            # If robots.txt redirected to an HTML portal or error page, allow crawling
            return True
        rp = robotparser.RobotFileParser()
        rp.parse(body.decode("utf-8", errors="replace").splitlines())
        return rp.can_fetch(USER_AGENT, page_url)

    def discover_links(
        self,
        page_url: str,
        link_pattern: str | None = None,
        request_delay: float = 0,
    ) -> list[dict[str, str]]:
        """Crawl a bank directory or aggregator page and discover card links."""
        # Auto-normalize known bank quirks (e.g. DBS JS sub-hub to main cards hub)
        if "dbs.com.sg" in page_url and page_url.rstrip("/").endswith("/credit-cards/default.page"):
            page_url = page_url.replace("/credit-cards/default.page", "/default.page")

        final_url, headers, body = self._fetch(page_url, request_delay=request_delay)
        content_type = (headers.get("content-type") or "").lower()
        if not any(k in content_type for k in ("text/html", "application/xhtml+xml")):
            raise ScrapeError("unsupported content type for discovery; expected HTML")

        parser = _LinkExtractor(final_url)
        parser.feed(body.decode("utf-8", errors="replace"))

        results: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        compiled = re.compile(link_pattern, re.IGNORECASE) if (link_pattern and link_pattern.strip()) else None

        for item in parser.links:
            clean_url = item["url"].split("#")[0].split("?")[0].rstrip("/")
            if not clean_url or clean_url in seen_urls or clean_url == final_url.rstrip("/"):
                continue

            slug = [p for p in urlparse(clean_url).path.strip("/").split("/") if p]
            if not slug:
                continue
            slug_name = slug[-1].replace(".page", "").replace(".html", "").lower()

            if compiled:
                if not compiled.search(clean_url):
                    continue
            else:
                # Default smart filter: must have card in path and not be utility/login page
                path_lower = urlparse(clean_url).path.lower()
                if not any(k in path_lower for k in ("card", "credit-card")):
                    continue
                if any(term in path_lower for term in SMART_EXCLUDE_TERMS):
                    continue
                if slug_name in SMART_EXCLUDE_SLUGS:
                    continue

            seen_urls.add(clean_url)
            raw_title = (item["title"] or "").strip()
            if raw_title.lower() in ("find out more", "learn more", "apply now", "click here", "read more", "see all", "more info", "overview", "see all cards"):
                raw_title = ""
            # Clean up long promotional title strings
            elif len(raw_title) > 40:
                raw_title = raw_title.split(",")[0].split(" - ")[0].split(".")[0].strip()
            title = raw_title or slug_name.replace("-", " ").title()
            results.append({"url": clean_url, "title": title, "slug": slug_name})

        return results

    def scrape_source(self, source: dict, db_path: str) -> dict:
        # Handle directory / hub crawling
        if source.get("is_directory"):
            run_id = db.start_run(db_path, source["source_id"])
            try:
                delay = float(source.get("request_delay_seconds", 0))
                if not self._robots_allowed(source["page_url"], delay):
                    raise ScrapeError("robots.txt disallows this configured path")
                discovered = self.discover_links(source["page_url"], source.get("link_pattern"), request_delay=delay)
                scraped: list[str] = []
                for item in discovered:
                    child_id = f"{source['source_id']}-{item['slug']}"
                    child_source = {
                        "source_id": child_id,
                        "issuer": source["issuer"],
                        "name": item["title"] or f"{source['issuer']} {item['slug'].replace('-', ' ').title()}",
                        "card_id": f"{source.get('card_id', source['source_id'])}-{item['slug']}",
                        "page_url": item["url"],
                        "enabled": True,
                        "request_delay_seconds": delay,
                        "extraction_hints": source.get("extraction_hints", {}),
                    }
                    res = self.scrape_source(child_source, db_path)
                    if res.get("status") == "success":
                        scraped.append(res.get("card_id", child_id))
                return {
                    "source_id": source["source_id"],
                    "run_id": run_id,
                    "status": "success",
                    "discovered_count": len(discovered),
                    "cards_scraped": scraped,
                }
            except Exception as exc:
                db.finish_failure(db_path, run_id, str(exc))
                return {"source_id": source["source_id"], "run_id": run_id, "status": "failed", "error": str(exc)}

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
            if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "application/pdf")):
                raise ScrapeError("unsupported content type; expected HTML or PDF")
            if "application/pdf" in content_type or body.startswith(b"%PDF"):
                from .extract import extract_pdf_text
                pdf_text = extract_pdf_text(body)
                facts = extract(source, pdf_text)
            else:
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
