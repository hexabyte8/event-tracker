import http.client
import ipaddress
import json
import logging
import socket
import time
from collections import deque
from datetime import date, datetime
from email.message import Message
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, UnicodeDammit

from .config import location_city, web_url
from .store import save_events

LOGGER = logging.getLogger(__name__)
USER_AGENT = "EventTracker/1.0"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EVENT_TYPES = {
    "Event", "BusinessEvent", "ChildrensEvent", "ComedyEvent", "CourseInstance",
    "DanceEvent", "DeliveryEvent", "EducationEvent", "ExhibitionEvent",
    "Festival", "FoodEvent", "Hackathon", "LiteraryEvent", "MusicEvent",
    "PublicationEvent", "SaleEvent", "ScreeningEvent", "SocialEvent",
    "SportsEvent", "TheaterEvent", "VisualArtsEvent",
}


def origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class FetchError(ValueError):
    pass


class Fetcher:
    def __init__(self, delay=1):
        self.delay = delay
        self.last_request = 0

    def fetch(self, url, allowed=None):
        initial_origin = origin(url)
        for _ in range(4):
            url = web_url(url)
            if not url or origin(url) != initial_origin:
                raise FetchError("Invalid URL or cross-origin redirect.")
            if allowed is not None and not allowed(USER_AGENT, url):
                raise FetchError("Disallowed by robots.txt.")
            parts = urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            if port not in (80, 443):
                raise FetchError("Only public web ports are allowed.")
            addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            ips = [item[4][0] for item in addresses]
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise FetchError("Non-public destination blocked.")
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            connection_class = (
                http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            )
            connection = connection_class(parts.hostname, port, timeout=10)
            # Pin the validated IP while retaining the original Host and TLS hostname.
            connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
                (ips[0], port), timeout, source_address
            )
            try:
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                connection.request("GET", path, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, application/ld+json, application/json, text/plain",
                    "Accept-Encoding": "identity",
                })
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = web_url(response.getheader("Location", ""), url)
                    continue
                if response.status == 404:
                    return None, url
                if response.status != 200:
                    raise FetchError(f"HTTP {response.status}")
                content_header = Message()
                content_header["Content-Type"] = response.getheader("Content-Type", "")
                content_type = content_header.get_content_type()
                if content_type not in ("text/html", "application/xhtml+xml", "application/ld+json", "application/json", "text/plain"):
                    raise FetchError("Unsupported content type.")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FetchError("Response exceeds size limit.")
                charset = content_header.get_content_charset()
                decoded = UnicodeDammit(
                    body,
                    known_definite_encodings=[charset] if charset else [],
                    is_html=content_type in ("text/html", "application/xhtml+xml"),
                ).unicode_markup
                if decoded is None:
                    raise FetchError("Unable to decode response.")
                return decoded, url
            finally:
                connection.close()
        raise FetchError("Too many redirects.")


def text(value):
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)[:10000]


def date_text(value):
    if not isinstance(value, str):
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return ""


def nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from nodes(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from nodes(item)


def location_text(value):
    if isinstance(value, list):
        return "; ".join(filter(None, (location_text(item) for item in value)))
    if not isinstance(value, dict):
        return text(value)
    address = value.get("address", "")
    if isinstance(address, dict):
        address = ", ".join(
            text(address.get(key))
            for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
            if address.get(key)
        )
    return ", ".join(filter(None, (text(value.get("name")), text(address))))


def parse_page(html, source_url, city):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    documents = [
        script.string or script.get_text()
        for script in soup.find_all("script", type="application/ld+json")
    ]
    if html.lstrip().startswith(("{", "[")):
        documents.append(html)
    for document in documents:
        try:
            data = json.loads(document)
        except (ValueError, RecursionError):
            continue
        for item in nodes(data):
            types = item.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if not any(isinstance(kind, str) and kind.rsplit("/", 1)[-1] in EVENT_TYPES for kind in types):
                continue
            title = text(item.get("name"))
            if not title:
                continue
            image = item.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("contentUrl") or image.get("url", "")
            events.append({
                "city": city,
                "title": title,
                "start_date": date_text(item.get("startDate")),
                "end_date": date_text(item.get("endDate")),
                "description": text(item.get("description")),
                "location": location_text(item.get("location")),
                "image_url": web_url(image, source_url),
                "event_url": (
                    web_url(item.get("url"), source_url)
                    or web_url(item.get("@id"), source_url)
                    or source_url
                ),
                "source_url": source_url,
            })
    links = [web_url(link.get("href"), source_url) for link in soup.find_all("a", href=True)]
    return events, [link for link in links if link and origin(link) == origin(source_url)]


def crawl(config, db_path, fetcher=None):
    fetcher = fetcher or Fetcher(config["request_delay_seconds"])
    count, errors = 0, 0
    for city in config["cities"]:
        for seed in city["sources"]:
            seed = web_url(seed)
            parts = urlsplit(seed)
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            robots = RobotFileParser(robots_url)
            try:
                rules, _ = fetcher.fetch(robots_url)
                robots.parse(rules.splitlines() if rules is not None else [])
            except (OSError, ValueError, http.client.HTTPException) as error:
                LOGGER.warning("Skipping %s: robots.txt unavailable: %s", seed, error)
                errors += 1
                continue
            robot_delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 0
            request_rate = robots.request_rate(USER_AGENT)
            if request_rate and request_rate.requests:
                robot_delay = max(robot_delay, request_rate.seconds / request_rate.requests)
            fetcher.delay = max(config["request_delay_seconds"], robot_delay)
            queue = deque([(seed, 0)])
            visited = set()
            while queue and len(visited) < config["max_pages_per_source"]:
                url, depth = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                if not robots.can_fetch(USER_AGENT, url):
                    LOGGER.info("Disallowed by robots.txt: %s", url)
                    continue
                try:
                    html, final_url = fetcher.fetch(url, allowed=robots.can_fetch)
                    if html is None:
                        continue
                    if not robots.can_fetch(USER_AGENT, final_url):
                        continue
                    events, links = parse_page(html, final_url, city["name"])
                    if config.get("filter_by_location"):
                        local_events = []
                        for event in events:
                            event["city"] = location_city(event["location"], config["cities"])
                            if event["city"]:
                                local_events.append(event)
                        events = local_events
                    save_events(db_path, events)
                    count += len(events)
                    if depth < config["max_depth"]:
                        remaining = config["max_pages_per_source"] - len(visited)
                        queued = {entry[0] for entry in queue}
                        for link in links:
                            if len(queue) >= remaining:
                                break
                            if link not in visited and link not in queued:
                                queue.append((link, depth + 1))
                                queued.add(link)
                except (OSError, ValueError, http.client.HTTPException, RecursionError) as error:
                    LOGGER.warning("Could not crawl %s: %s", url, error)
                    errors += 1
    LOGGER.info("Crawl complete: %s events processed, %s errors", count, errors)
    return count, errors
