import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import identifiers
import storage
import vod_catalog
import xtream_proxy


FIXTURE = {
    "movies": [
        {"imdb_id": "tt0000001", "name": "Zulu", "year": "2024", "genres": ["Drama"]},
        {"imdb_id": "tt0000002", "name": "Alpha", "year": "2025", "genres": ["Drama"]},
    ],
    "series": [
        {"imdb_id": "tt0000003", "name": "Show", "year": "2025", "genres": ["Drama"],
         "episodes": [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}]},
    ],
}


class IdentifierTests(unittest.TestCase):
    def test_vod_ids_are_stable_across_order_and_process_hash_seed(self):
        forward = [identifiers.movie_id(m["imdb_id"]) for m in FIXTURE["movies"]]
        reverse = [identifiers.movie_id(m["imdb_id"]) for m in reversed(FIXTURE["movies"])]
        self.assertEqual(forward, list(reversed(reverse)))
        self.assertNotEqual(identifiers.movie_id("tt0000001"), identifiers.movie_id("tt0000002"))
        self.assertEqual(identifiers.category_id("Drama"), identifiers.category_id("Drama"))

    def test_episode_ids_are_distinct_and_stable(self):
        first = identifiers.episode_id("tt0000003", 1, 1)
        self.assertEqual(first, identifiers.episode_id("tt0000003", 1, 1))
        self.assertNotEqual(first, identifiers.episode_id("tt0000003", 1, 2))
        self.assertGreater(first, identifiers.EPISODE_BASE)


class StorageTests(unittest.TestCase):
    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            storage.write_json(path, {"ok": True})
            self.assertEqual(storage.read_json(path, {}), {"ok": True})
            self.assertEqual(os.listdir(directory), ["state.json"])


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = mock.patch.multiple(
            vod_catalog, OUT=self.temp.name,
            CAND=os.path.join(self.temp.name, "candidates.jsonl"),
            PROVIDER_CACHE=os.path.join(self.temp.name, "providers.json"),
            FILTER_LOCK=os.path.join(self.temp.name, ".lock"),
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_filter_is_idempotent_and_caches_negative_answers(self):
        storage.write_json(vod_catalog.CAND, "")
        with open(vod_catalog.CAND, "w", encoding="utf-8") as stream:
            for item in FIXTURE["movies"]:
                stream.write(json.dumps(item) + "\n")
        results = iter([(True, True), (False, True)])
        with mock.patch.object(vod_catalog, "provider_result", side_effect=lambda item: next(results)) as lookup:
            vod_catalog.stage_filter()
            vod_catalog.stage_filter()
        self.assertEqual(lookup.call_count, 2)
        cache = storage.read_json(vod_catalog.PROVIDER_CACHE, {})
        self.assertTrue(cache["tt0000001"]["supported"])
        self.assertFalse(cache["tt0000002"]["supported"])

    def test_transport_failure_does_not_replace_cached_answer(self):
        with open(vod_catalog.CAND, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(FIXTURE["movies"][0]) + "\n")
        with mock.patch.object(vod_catalog, "provider_result", return_value=(True, True)):
            vod_catalog.stage_filter()
        with mock.patch.object(vod_catalog, "provider_result", return_value=(False, False)) as lookup:
            vod_catalog.stage_filter()
        lookup.assert_not_called()
        cache = storage.read_json(vod_catalog.PROVIDER_CACHE, {})
        self.assertTrue(cache["tt0000001"]["supported"])

    def test_episode_tuple_and_retry(self):
        with mock.patch.object(vod_catalog.vod, "episodes", return_value=[(1, 1, "ok")]):
            self.assertEqual(vod_catalog.episodes_of("tt1"), [{"season": 1, "episode": 1}])
        item = {"imdb_id": "tt1", "type": "movie"}
        with mock.patch.object(vod_catalog.vod, "resolve",
                               side_effect=[None, {"kind": "direct", "url": "https://example.test/v.mp4"}]), \
                mock.patch.object(vod_catalog.time, "sleep"):
            self.assertIsNotNone(vod_catalog.verify(item))


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_pub, cls.old_pass = xtream_proxy.PUB, xtream_proxy.PASS
        xtream_proxy.PUB, xtream_proxy.PASS = cls.temp.name, "test-secret"
        storage.write_json(os.path.join(cls.temp.name, "vod.json"), FIXTURE)
        storage.write_json(os.path.join(cls.temp.name, "channels.json"),
                           [{"name": "One", "group": "General", "url": "https://example.test/1",
                             "tvg_id": "one", "logo": ""}])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), xtream_proxy.H)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        xtream_proxy.PUB, xtream_proxy.PASS = cls.old_pub, cls.old_pass
        cls.temp.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_health_does_not_require_credentials(self):
        status, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["movies"], 2)

    def test_all_sensitive_routes_require_auth(self):
        for path in ("/player_api.php", "/get.php?type=m3u", "/playlist.m3u",
                     "/xmltv.php", "/movie/easyplay/wrong/1000001.mp4",
                     "/series/easyplay/wrong/100000001.mp4"):
            status, _ = self.request(path)
            self.assertEqual(status, 401, path)

    def test_xtream_catalogue_uses_stable_ids(self):
        auth = "username=easyplay&password=test-secret"
        status, body = self.request("/player_api.php?%s&action=get_vod_streams" % auth)
        movies = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([m["stream_id"] for m in movies],
                         [identifiers.movie_id(m["imdb_id"]) for m in FIXTURE["movies"]])
        status, body = self.request("/player_api.php?%s&action=get_series" % auth)
        series = json.loads(body)[0]
        self.assertEqual(series["series_id"], "tt0000003")
        status, body = self.request("/player_api.php?%s&action=get_series_info&series_id=tt0000003" % auth)
        info = json.loads(body)
        self.assertEqual(info["episodes"]["1"][0]["id"],
                         str(identifiers.episode_id("tt0000003", 1, 1)))

    def test_stable_vod_route_and_category(self):
        ident = identifiers.movie_id("tt0000001")
        auth = "username=easyplay&password=test-secret"
        status, body = self.request("/player_api.php?%s&action=get_vod_info&vod_id=%d" % (auth, ident))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["info"]["name"], "Zulu")

    def test_head_has_no_body(self):
        status, body = self.request("/healthz", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
