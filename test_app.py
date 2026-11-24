import io
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import app


SHORT_URL = "https://maps.app.goo.gl/PublicTest1"
PLACE_URL = (
    "https://www.google.co.jp/maps/place/Test/@35.65,139.74,17z/"
    "data=!4m5!3m4!1s0xa:0xb!8m2!3d35.66!4d139.75!16s%2Fg%2Fexample"
)


def redirect(location):
    headers = Message()
    headers["Location"] = location
    return HTTPError(SHORT_URL, 302, "Found", headers, io.BytesIO())


class DestinationCoordinatesTests(unittest.TestCase):
    def test_place_pin_wins_over_viewport_with_escaped_slashes_in_data(self):
        self.assertEqual(app.extract_latlon_from_url(PLACE_URL), (35.66, 139.75))

    def test_explicit_pin_queries(self):
        for query in ("/maps/search/?api=1&query=35.66%2C139.75", "/maps?q=35.66,139.75"):
            with self.subTest(query=query):
                self.assertEqual(app.extract_latlon_from_url("https://www.google.com" + query), (35.66, 139.75))

    def test_ambiguous_or_invalid_coordinates_are_not_returned(self):
        for url in (
            "https://www.google.com/maps/@35.66,139.75,17z",
            "https://www.google.com/maps?center=35.66,139.75&ll=35.67,139.76",
            "https://www.google.com/maps/search/?api=1&query=35,139&query_place_id=example",
            "https://www.google.com/maps?query=35,139&q=36,140",
            "https://www.google.com/maps?q=91,139",
            "https://www.google.com/maps/dir/?api=1&destination=35,139",
            PLACE_URL + "!8m2!3d35.67!4d139.76",
            PLACE_URL.replace("!4d139.75", "!4d139.75e1"),
        ):
            with self.subTest(url=url):
                self.assertIsNone(app.extract_latlon_from_url(url))

    def test_text_search_only_without_an_explicit_place_id(self):
        url = "https://www.google.com/maps/search/?api=1&query=Test%20Place"
        self.assertEqual(app.extract_text_query(url), "Test Place")
        self.assertIsNone(app.extract_text_query(url + "&query_place_id=example"))
        self.assertIsNone(app.extract_text_query("https://www.google.com/maps?q=91,139"))


@patch.object(app, "HTTP_FAST_RESOLVE", True)
@patch.object(app, "HTTP_FAST_RESOLVE_TIMEOUT_SECONDS", 2)
@patch.object(app, "build_opener")
class HttpExpansionTests(unittest.TestCase):
    def test_google_short_link_resolves_without_fetching_destination(self, build_opener):
        opener = build_opener.return_value
        opener.open.side_effect = redirect(PLACE_URL)
        timings = {}
        result = app.try_http_resolve(SHORT_URL, timings)
        self.assertEqual((result["lat"], result["lon"]), (35.66, 139.75))
        self.assertEqual(result["method"], "direct_google_http_redirect")
        self.assertEqual(result["visited_urls"], [SHORT_URL, PLACE_URL])
        self.assertIn("http_expand", timings)
        opener.open.assert_called_once()

    def test_unknown_hosts_credentials_and_loops_are_not_followed(self, build_opener):
        for target in ("https://maps.app.goo.gl.example/maps/a", "https://www.google.com@example.invalid/maps/a",
                       "https://www.google.com:8443/maps/a", "http://www.google.com/maps/a", SHORT_URL):
            with self.subTest(target=target):
                opener = build_opener.return_value = Mock()
                opener.open.side_effect = redirect(target)
                self.assertIsNone(app.try_http_resolve(SHORT_URL, {}))
                opener.open.assert_called_once()

    def test_network_and_coordinate_failures_fall_back(self, build_opener):
        opener = build_opener.return_value
        opener.open.side_effect = URLError(TimeoutError())
        self.assertIsNone(app.try_http_resolve(SHORT_URL, {}))
        opener.open.side_effect = [
            redirect("https://www.google.com/maps/@35,139,17z"),
            Mock(__enter__=Mock(), __exit__=Mock()),
        ]
        self.assertIsNone(app.try_http_resolve(SHORT_URL, {}))

    def test_long_links_do_not_use_http_expansion(self, build_opener):
        self.assertIsNone(app.try_http_resolve(PLACE_URL, {}))
        build_opener.assert_not_called()

    def test_route_calls_browser_when_fast_path_is_unavailable(self, build_opener):
        build_opener.return_value.open.side_effect = URLError(TimeoutError())
        expected = app.base_resolve_result(SHORT_URL, PLACE_URL, [PLACE_URL])
        expected["method"] = "unresolved"
        with patch.object(app.browser_manager, "resolve", return_value=expected) as browser, \
                patch.object(app, "set_cached_response"), patch.object(app, "cleanup_db"):
            self.assertEqual(app.resolve(app.ResolveRequest(url=SHORT_URL)), expected)
        browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
