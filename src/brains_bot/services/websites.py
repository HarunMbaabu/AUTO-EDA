"""Read a partner's or supplier's website for products and prices, into the same catalog
spreadsheet as a brochure (see services/brochures.py): nothing goes live before a person checks
the sheet and sends it back.

Shops built on Shopify or WooCommerce publish their products as data, which is read first; other
sites are read page by page (the home page and the shop-looking pages it links to), including
the product data many sites embed for search engines. The bot asks robots.txt first, reads only
public web addresses, and keeps to a few pages per request. Sites that draw their products with
JavaScript only show what is in the page itself.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from brains_bot.services.analysis import AnalysisError
from brains_bot.services.brochures import MAX_TEXT_CHARS, Reading

log = logging.getLogger(__name__)

USER_AGENT = "BrainsBot/1.0 (catalog reader; reads a few pages on request)"
TIMEOUT = 15.0
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
RETRY_AFTER = 3.0  # seconds, once, after 429 Too Many Requests (shops limit bursts)
MAX_PAGES = 8
PAGE_CHARS = 4_000
MAX_SHOP_PRODUCTS = 100  # the sheet takes MAX_PRODUCTS (80); full lists can pass MAX_BYTES
# Links worth following from the home page, best first.
SHOP_WORDS = ("product", "shop", "catalog", "catalogue", "price", "collection", "category",
              "store", "range", "item")  # fmt: skip
_SKIP_LINK = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|pdf|zip|mp4|css|js)(?:$|\?)|/(?:cart|checkout|account|login|"
    r"wp-admin|my-account)(?:/|$)|^(?:mailto|tel|whatsapp|javascript):",
    re.I,
)
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
_BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section",
               "article", "td", "th", "dd", "dt", "figcaption"}  # fmt: skip


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    body: str


def _public(host: str | None) -> bool:
    """Only public internet addresses: never the bot's own server or a private network."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def normalize(url: str) -> str:
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise AnalysisError("that doesn't look like a website address")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       parts.query, ""))  # fmt: skip


def _get(client: httpx.Client, url: str) -> Page | None:
    """GET a public URL, following redirects one at a time so each hop is checked; None when it
    cannot be read."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not _public(parts.hostname):
            return None
        client.cookies.clear()  # no session: shops limit product lists per session cookie
        try:
            with client.stream("GET", url) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                return Page(
                    str(response.url),
                    response.status_code,
                    response.headers.get("content-type", ""),
                    bytes(body[:MAX_BYTES]).decode(response.encoding or "utf-8", "replace"),
                )
        except httpx.HTTPError:
            return None
    return None


def _json(page: Page | None) -> Any:
    if page is None or page.status != 200:
        return None
    try:
        return json.loads(page.body)
    except ValueError:
        return None


class _PageText(HTMLParser):
    """Visible text, links and embedded product data (JSON-LD) of one HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.data: list[str] = []
        self._skip = 0
        self._ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._ld = True
        elif tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld:
            self._ld = False
        elif tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._ld:
            self.data.append(data)
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).split("\n"))
        return "\n".join(line for line in lines if line)


def _ld_products(blocks: list[str]) -> list[str]:
    """Product lines from a page's JSON-LD (schema.org Product with offers)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Product" in kinds and node.get("name"):
            offers = node.get("offers") or {}
            offer = offers[0] if isinstance(offers, list) and offers else offers
            offer = offer if isinstance(offer, dict) else {}
            price = offer.get("price") or offer.get("lowPrice")
            currency = offer.get("priceCurrency") or ""
            brand = node.get("brand")
            brand = brand.get("name") if isinstance(brand, dict) else brand
            found.append(
                " | ".join(
                    str(x)
                    for x in (
                        node["name"],
                        brand,
                        (node.get("description") or "")[:150],
                        f"{currency} {price}".strip() if price else None,
                    )
                    if x
                )
            )
        for key in ("@graph", "itemListElement", "item", "hasVariant"):
            if key in node:
                walk(node[key])

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


Fetch = Callable[[str], Page | None]


def _shopify(fetch: Fetch, origin: str) -> list[str]:
    data = _json(fetch(f"{origin}/products.json?limit={MAX_SHOP_PRODUCTS}"))
    products = data.get("products") if isinstance(data, dict) else None
    lines = []
    for p in products or []:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        variants = [v for v in p.get("variants") or [] if isinstance(v, dict)]
        prices = sorted({v["price"] for v in variants if v.get("price")}, key=float)
        price = (f"from {prices[0]}" if len(prices) > 1 else prices[0]) if prices else None
        lines.append(" | ".join(str(x) for x in (p["title"], p.get("vendor"),
                                                 p.get("product_type"), price) if x))  # fmt: skip
    return lines


def _woocommerce(fetch: Fetch, origin: str) -> list[str]:
    data = _json(fetch(f"{origin}/wp-json/wc/store/v1/products?per_page=100"))
    lines = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        prices = p.get("prices") or {}
        price = None
        if prices.get("price"):
            try:
                amount = int(prices["price"]) / 10 ** int(prices.get("currency_minor_unit") or 0)
                price = f"{prices.get('currency_code') or ''} {amount:,.0f}".strip()
            except (TypeError, ValueError):
                price = None
        categories = ", ".join(c.get("name", "") for c in p.get("categories") or [] if
                               isinstance(c, dict))  # fmt: skip
        parser = _PageText()
        parser.feed(p.get("short_description") or "")
        lines.append(" | ".join(x for x in (p["name"], categories, parser.text()[:150], price)
                                if x))  # fmt: skip
    return lines


def _score(url: str) -> int:
    path = urlsplit(url).path.lower()
    return sum(word in path for word in SHOP_WORDS)


def read_site(url: str, client: httpx.Client | None = None) -> Reading:
    """The products and prices a website shows, as text for the product reader."""
    url = normalize(url)
    own = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    )
    try:
        home = _get(client, url)
        if home is None or home.status >= 400:
            raise AnalysisError("that website could not be opened")
        parts = urlsplit(home.url)
        origin = f"{parts.scheme}://{parts.netloc}"
        robots = RobotFileParser()
        rules = _get(client, f"{origin}/robots.txt")
        robots.parse(rules.body.splitlines() if rules is not None and rules.status == 200 else [])
        if not robots.can_fetch(USER_AGENT, home.url):
            raise AnalysisError("that website asks bots not to read it")

        def fetch(link: str) -> Page | None:
            if not robots.can_fetch(USER_AGENT, link):
                return None
            page = _get(client, link)
            if page is not None and page.status == 429:
                time.sleep(RETRY_AFTER)
                page = _get(client, link)
            return page

        for method, read in (("shopify", _shopify), ("woocommerce", _woocommerce)):
            lines = read(fetch, origin)
            if lines:
                log.info("website read", extra={"event": "website_read", "command": method})
                return Reading(f"Products on {origin}:\n" + "\n".join(lines), 1, method)

        texts, seen, queue = [], {home.url}, [home]
        while queue and len(texts) < MAX_PAGES:
            page = queue.pop(0)
            if page is None or page.status >= 400 or "html" not in page.content_type.lower():
                continue
            parser = _PageText()
            parser.feed(page.body)
            products = _ld_products(parser.data)
            body = "\n".join([*products, parser.text()])[:PAGE_CHARS]
            texts.append(f"Page {page.url}:\n{body}")
            if page is not home:
                continue
            links = []
            for href in parser.links:
                link = urljoin(page.url, href.strip()).split("#")[0]
                if (urlsplit(link).netloc == parts.netloc and link not in seen
                        and not _SKIP_LINK.search(href.strip()) and _score(link)):  # fmt: skip
                    seen.add(link)
                    links.append(link)
            links.sort(key=_score, reverse=True)
            queue += [fetch(link) for link in links[: MAX_PAGES - 1]]
        log.info("website read", extra={"event": "website_read", "command": str(len(texts))})
        return Reading("\n\n".join(texts)[:MAX_TEXT_CHARS], len(texts), "web")
    finally:
        if own:
            client.close()
