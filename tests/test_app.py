import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from event_tracker.config import load_config, location_city, web_url
from event_tracker.crawler import FetchError, Fetcher, MAX_RESPONSE_BYTES, crawl, parse_page
from event_tracker.store import find_events, save_events
from event_tracker.web import create_app


def page(**overrides):
    event = {
        "@type": "MusicEvent",
        "name": "City concert",
        "startDate": "2099-05-01T19:00:00+01:00",
        "description": "<p>Live music</p>",
        "location": {
            "@type": "Place", "name": "Town Hall",
            "address": {"streetAddress": "1 High Street", "addressLocality": "London"},
        },
        "image": {"url": "/concert.jpg"},
        "url": "/concert",
    }
    event.update(overrides)
    return '<script type="application/ld+json">' + json.dumps({"@graph": [event]}) + "</script>"


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "events.sqlite3")
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps({
            "cities": [
                {"name": "London", "sources": ["https://example.org/events"]},
                {"name": "Cambridge", "sources": ["https://example.net/events"]},
            ],
            "max_pages_per_source": 2,
            "max_depth": 1,
            "request_delay_seconds": 1,
        }))
        self.config = load_config(self.config_path)

    def test_jsonld_graph_extracts_event_details_and_relative_urls(self):
        events, links = parse_page(page() + '<a href="/detail">More</a><a href="https://other.org/">Other</a>', "https://example.org/events", "London")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["location"], "Town Hall, 1 High Street, London")
        self.assertEqual(events[0]["description"], "Live music")
        self.assertEqual(events[0]["image_url"], "https://example.org/concert.jpg")
        self.assertEqual(events[0]["event_url"], "https://example.org/concert")
        self.assertEqual(events[0]["source_url"], "https://example.org/events")
        self.assertEqual(links, ["https://example.org/detail"])

    def test_malformed_and_non_event_json_are_ignored(self):
        events, _ = parse_page('<script type="application/ld+json">{bad}</script>' + page(**{"@type": "Place"}), "https://example.org/", "London")
        self.assertEqual(events, [])

    def test_missing_fields_and_unsafe_urls(self):
        events, _ = parse_page(page(image=None, url="javascript:alert(1)", startDate="bad", location=None), "https://example.org/", "London")
        self.assertEqual(events[0]["image_url"], "")
        self.assertEqual(events[0]["event_url"], "https://example.org/")
        self.assertEqual(events[0]["start_date"], "")
        self.assertEqual(events[0]["location"], "")
        for url in ("", "javascript:alert(1)", "file:///etc/passwd", "******example.org/", "https://example.org\\evil"):
            self.assertEqual(web_url(url), "")

    def test_raw_json_event_list(self):
        events, _ = parse_page(json.dumps([{"@type": "Event", "name": "Market"}]), "https://example.org/", "London")
        self.assertEqual(events[0]["title"], "Market")

    def test_canonical_event_id_deduplicates_listing_and_detail_pages(self):
        html = page(url=None, **{"@id": "https://example.org/concert"})
        for source in ("https://example.org/listing", "https://example.org/detail"):
            events, _ = parse_page(html, source, "London")
            save_events(self.db, events)
        saved = find_events(self.db, ["London"])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["event_url"], "https://example.org/concert")
        self.assertEqual(saved[0]["source_url"], "https://example.org/detail")

    def test_basic_and_week_dates_are_normalized_before_filtering(self):
        for value in ("20000101", "2000-W01-1", "20000101T190000Z"):
            events, _ = parse_page(page(startDate=value), "https://example.org/", "London")
            self.assertTrue(events[0]["start_date"].startswith("2000-01-"))
            save_events(self.db, events)
        self.assertEqual(find_events(self.db, ["London"]), [])
        self.assertEqual(len(find_events(self.db, ["London"], include_past=True)), 3)

    def test_crawl_respects_robots_bounds_and_multiple_cities(self):
        requested = []

        class FixtureFetcher:
            delay = 1

            def fetch(self, url, allowed=None):
                requested.append(url)
                if url.endswith("/robots.txt"):
                    return "User-agent: *\nDisallow: /private\nCrawl-delay: 2", url
                if allowed:
                    self_check = allowed("EventTracker/1.0", url)
                    if not self_check:
                        raise FetchError("disallowed")
                return page() + '<a href="/private">Private</a><a href="/detail">Details</a><a href="/extra">Extra</a>', url

        fetcher = FixtureFetcher()
        count, errors = crawl(self.config, self.db, fetcher)
        self.assertEqual(errors, 0)
        self.assertEqual(count, 2)
        self.assertFalse(any(url.endswith("/private") for url in requested))
        self.assertEqual(len(requested), 4)
        self.assertEqual(fetcher.delay, 2)
        self.assertEqual(len(find_events(self.db, ["London", "Cambridge"])), 2)

    def test_crawl_follows_detail_link_and_updates_existing_events(self):
        class FixtureFetcher:
            delay = 1

            def fetch(self, url, allowed=None):
                if url.endswith("/robots.txt"):
                    return None, url
                if url.endswith("/events"):
                    return '<a href="/detail">Detail</a><a href="/extra">Extra</a>', url
                return page(), url

        for _ in range(2):
            count, errors = crawl(self.config, self.db, FixtureFetcher())
            self.assertEqual((count, errors), (2, 0))
        self.assertEqual(len(find_events(self.db, ["London", "Cambridge"])), 2)

    def test_robots_failure_skips_source_without_erasing_events(self):
        events, _ = parse_page(page(), "https://example.org/", "London")
        save_events(self.db, events)
        fetcher = Mock()
        fetcher.fetch.side_effect = FetchError("HTTP 403")
        self.assertEqual(crawl(self.config, self.db, fetcher), (0, 2))
        self.assertEqual(fetcher.fetch.call_count, 2)
        self.assertEqual(len(find_events(self.db, ["London"])), 1)

    def test_upsert_search_city_and_past_filters(self):
        events, _ = parse_page(page(), "https://example.org/", "London")
        save_events(self.db, events)
        events[0]["description"] = "Updated music"
        save_events(self.db, events)
        old, _ = parse_page(page(name="Old concert", startDate="2000-01-01"), "https://example.net/", "Cambridge")
        save_events(self.db, old)
        self.assertEqual(len(find_events(self.db, ["London", "Cambridge"])), 1)
        self.assertEqual(len(find_events(self.db, ["London", "Cambridge"], include_past=True)), 2)
        self.assertEqual(find_events(self.db, ["London"], query="UPDATED")[0]["description"], "Updated music")
        self.assertEqual(find_events(self.db, ["London"], city="Unknown"), [])
        self.assertEqual(find_events(self.db, ["London"], query="' OR 1=1 --"), [])
        self.assertEqual(find_events(self.db, ["Cambridge"]), [])

    def test_ongoing_events_remain_visible(self):
        events, _ = parse_page(page(startDate="2000-01-01", endDate="2099-01-01"), "https://example.org/", "London")
        save_events(self.db, events)
        self.assertEqual(len(find_events(self.db, ["London"])), 1)

    def local_config(self):
        return {
            **self.config,
            "filter_by_location": True,
            "cities": [
                {"name": "Nashville", "regions": ["TN", "Tennessee"],
                 "sources": ["https://example.org/events"]},
                {"name": "Lebanon", "regions": ["TN", "Tennessee"], "sources": []},
                {"name": "Mount Juliet", "regions": ["TN", "Tennessee"],
                 "localities": ["Mount Juliet", "Mt Juliet", "Mt. Juliet"], "sources": []},
            ],
        }

    def test_location_matching_requires_exact_locality_and_region(self):
        cities = self.local_config()["cities"]
        for location, expected in (
            ("Hall, 1 Main Street, Nashville, TN, 37201, US", "Nashville"),
            ("Hall, Lebanon, Tennessee, US", "Lebanon"),
            ("Hall, mt. juliet, tn, US", "Mount Juliet"),
            ("Hall, North Nashville, TN, US", ""),
            ("Hall, Nashville, GA, US", ""),
            ("Nashville Music Tour, Atlanta, GA, US", ""),
            ("Hall, Charleston, SC, US", ""),
            ("Nashville", ""),
            ("", ""),
            ("Hall, Nashville, TN; Hall, Lebanon, TN", ""),
        ):
            with self.subTest(location=location):
                self.assertEqual(location_city(location, cities), expected)

    def test_crawl_filters_recommendations_and_assigns_venue_city(self):
        config = self.local_config()
        config["max_depth"] = 0
        html = "".join(page(name=name, location={
            "address": {"addressLocality": locality, "addressRegion": region}
        }) for name, locality, region in (
            ("Local", "Nashville", "TN"),
            ("Other configured city", "Lebanon", "TN"),
            ("Alias", "Mt. Juliet", "Tennessee"),
            ("Distant", "Atlanta", "GA"),
            ("Wrong state", "Lebanon", "PA"),
        )) + page(name="Online", location=None)
        fetcher = Mock(delay=1)
        fetcher.fetch.side_effect = lambda url, allowed=None: (
            ("User-agent: *\nDisallow:", url) if url.endswith("/robots.txt") else (html, url)
        )
        self.assertEqual(crawl(config, self.db, fetcher), (3, 0))
        events = find_events(self.db, [city["name"] for city in config["cities"]])
        self.assertEqual({event["title"]: event["city"] for event in events}, {
            "Local": "Nashville", "Other configured city": "Lebanon", "Alias": "Mount Juliet",
        })

    def test_web_filters_and_reclassifies_existing_records_without_duplicates(self):
        config = self.local_config()
        self.config_path.write_text(json.dumps(config))
        for city in ("Nashville", "Lebanon"):
            events, _ = parse_page(page(name="Local concert", location={
                "name": "Hall", "address": {"addressLocality": "Lebanon", "addressRegion": "TN"},
            }), "https://example.org/events", city)
            save_events(self.db, events)
        for title, location in (("Distant concert", "Hall, Atlanta, GA"), ("Online concert", "")):
            events, _ = parse_page(page(name=title, location=location), "https://example.org/", "Lebanon")
            save_events(self.db, events)
        client = create_app(self.config_path, self.db).test_client()
        body = client.get("/?city=Lebanon&q=concert").get_data(as_text=True)
        self.assertIn("1 event shown", body)
        self.assertIn("Local concert", body)
        self.assertNotIn("Distant concert", body)
        self.assertNotIn("Online concert", body)
        self.assertIn("No events found", client.get("/?city=Nashville").get_data(as_text=True))
        self.assertEqual(len(find_events(self.db, ["Nashville", "Lebanon"])), 4)

    def test_location_filter_is_applied_before_result_limit(self):
        config = self.local_config()
        events, _ = parse_page(page(location="Hall, Atlanta, GA"), "https://example.org/", "Nashville")
        distant = [{**events[0], "title": f"A distant {i}"} for i in range(501)]
        local = {**events[0], "title": "Z local", "location": "Hall, Nashville, TN"}
        save_events(self.db, distant + [local])
        found = find_events(self.db, ["Nashville"], location_cities=config["cities"])
        self.assertEqual([event["title"] for event in found], ["Z local"])

    def test_location_filter_configuration_is_validated(self):
        config = self.local_config()
        for changes in (
            {"filter_by_location": "true"},
            {"cities": [{"name": "Nashville", "sources": []}]},
            {"cities": [{"name": "Nashville", "sources": [], "regions": "TN"}]},
            {"cities": [{"name": "Nashville", "sources": [], "regions": ["TN"], "localities": []}]},
        ):
            self.config_path.write_text(json.dumps({**config, **changes}))
            with self.assertRaises(ValueError):
                load_config(self.config_path)

    def test_web_renders_pictures_locations_links_and_escapes_content(self):
        events, _ = parse_page(page(), "https://example.org/events", "London")
        events[0]["title"] = '<script>alert("x")</script>'
        save_events(self.db, events)
        client = create_app(self.config_path, self.db).test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("concert.jpg", body)
        self.assertIn("Town Hall", body)
        self.assertIn("Original source", body)
        self.assertIn("https://example.org/concert", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn('<script>alert("x")</script>', body)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(client.get("/healthz").json, {"status": "ok"})
        self.assertIn("No events found", client.get("/?city=Cambridge").get_data(as_text=True))
        self.assertEqual(client.post("/").status_code, 405)

    def test_config_changes_are_visible_without_restart(self):
        client = create_app(self.config_path, self.db).test_client()
        self.config["cities"] = [{"name": "Oxford", "sources": []}]
        self.config_path.write_text(json.dumps(self.config))
        self.assertIn("Oxford", client.get("/").get_data(as_text=True))
        self.assertNotIn("Cambridge", client.get("/").get_data(as_text=True))

    def test_invalid_configuration_fails_early(self):
        for cities in ([], [{"name": "", "sources": []}], [{"name": "A", "sources": ["file:///bad"]}], [{"name": "A", "sources": []}] * 2):
            self.config_path.write_text(json.dumps({"cities": cities}))
            with self.assertRaises(ValueError):
                load_config(self.config_path)
        self.config["max_pages_per_source"] = 101
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            load_config(self.config_path)


class NetworkSafetyTests(unittest.TestCase):
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_private_loopback_linklocal_and_mixed_dns_are_blocked(self, resolve):
        for addresses in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["93.184.216.34", "192.168.1.1"]):
            resolve.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
            with self.assertRaises(FetchError):
                Fetcher(0).fetch("https://example.org/")

    def response(self, status=200, body=b"<html></html>", location=None, content_type="text/html"):
        response = Mock()
        response.status = status
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": content_type,
            "Location": location,
        }.get(name, default)
        response.read.return_value = body
        return response

    @patch("event_tracker.crawler.http.client.HTTPSConnection")
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_validated_ip_is_pinned_and_hostname_is_preserved(self, resolve, connection_class):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        connection = connection_class.return_value
        connection.getresponse.return_value = self.response()
        body, url = Fetcher(0).fetch("https://example.org/event")
        self.assertEqual(body, "<html></html>")
        connection_class.assert_called_once_with("example.org", 443, timeout=10)
        with patch("event_tracker.crawler.socket.create_connection") as connect:
            connection._create_connection(("example.org", 443), 10)
            connect.assert_called_once_with(("93.184.216.34", 443), 10, None)
        connection.close.assert_called_once()

    @patch("event_tracker.crawler.http.client.HTTPSConnection")
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_cross_origin_redirect_is_blocked(self, resolve, connection_class):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        connection_class.return_value.getresponse.return_value = self.response(302, location="http://169.254.169.254/")
        with self.assertRaises(FetchError):
            Fetcher(0).fetch("https://example.org/")
        self.assertEqual(connection_class.call_count, 1)

    @patch("event_tracker.crawler.http.client.HTTPSConnection")
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_robots_checked_before_redirect_target_is_requested(self, resolve, connection_class):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        connection_class.return_value.getresponse.return_value = self.response(302, location="/private")
        with self.assertRaises(FetchError):
            Fetcher(0).fetch("https://example.org/", allowed=lambda agent, url: not url.endswith("/private"))
        self.assertEqual(connection_class.call_count, 1)

    @patch("event_tracker.crawler.http.client.HTTPSConnection")
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_response_size_limit(self, resolve, connection_class):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        connection_class.return_value.getresponse.return_value = self.response(body=b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(FetchError):
            Fetcher(0).fetch("https://example.org/")

    @patch("event_tracker.crawler.http.client.HTTPSConnection")
    @patch("event_tracker.crawler.socket.getaddrinfo")
    def test_http_charset_and_html_metadata_preserve_event_text(self, resolve, connection_class):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        html = page(name="Café", location="Théâtre").replace("\\u00e9", "é").replace("\\u00e2", "â")
        for content_type, prefix in (
            ("text/html; charset=iso-8859-1", ""),
            ("text/html", '<meta charset="iso-8859-1">'),
        ):
            connection_class.return_value.getresponse.return_value = self.response(
                body=(prefix + html).encode("iso-8859-1"), content_type=content_type
            )
            body, url = Fetcher(0).fetch("https://example.org/")
            events, _ = parse_page(body, url, "London")
            self.assertEqual(events[0]["title"], "Café")
            self.assertEqual(events[0]["location"], "Théâtre")


if __name__ == "__main__":
    unittest.main()
