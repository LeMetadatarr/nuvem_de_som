"""nuvem_de_som as an arrkit indexer and download client, against a fake SoundCloud.

arrkit.net.request is replaced by a router that answers as the home page, its script bundle and API v2 do; the stream addresses point at a
file server on 127.0.0.1. Every name is a placeholder and nothing reaches the real site. Run with `python -m pytest tests_arrkit`;
arrkit (the `arr` extra) is needed."""
import contextlib
import http.client
import http.server
import io
import importlib
import json
import re
import socket
import ssl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from email.message import Message
from unittest import mock

import tomllib
from arrkit import net, plugins
from arrkit.clients import ClientError
from arrkit.indexers import IndexerError
from arrkit.testing import FakeServer

import nuvem_de_som
from nuvem_de_som import arr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_ID = "a" * 32
NEWEST = "2999-01-01T00:00:00Z"
OLD = "2001-01-01T00:00:00Z"
CDN = "https://files.example.invalid"  # stream and cover addresses are https like SoundCloud's; Base.setUp maps them to the file server
ART = CDN + "/cover.jpg"


def answer(status=200, body=b"", headers=None):
    m = Message()
    for k, v in (headers or {}).items():
        m[k] = v
    return net.Response(status, m, body if isinstance(body, bytes) else json.dumps(body).encode())


def user(i=7, name="Example Artist"):
    return {"id": i, "username": name, "permalink_url": f"https://soundcloud.com/artist-{i}", "avatar_url": ART,
            "country_code": "PT"}


def track(i, title, downloadable=True, license="cc-by", access="playable", policy="ALLOW", progressive=True, snipped=False, who=None,
          date=NEWEST, hq=False, **extra):
    tcs = [{"url": f"https://api-v2.soundcloud.com/media/{i}/sq/progressive", "quality": "sq", "snipped": snipped,
            "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}}] if progressive else []
    if hq:
        tcs.insert(0, {"url": f"https://api-v2.soundcloud.com/media/{i}/hq/progressive", "quality": "hq", "snipped": snipped,
                       "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}})
    tcs.append({"url": f"https://api-v2.soundcloud.com/media/{i}/sq/hls", "quality": "sq", "snipped": snipped,
                "format": {"protocol": "hls", "mime_type": "audio/mpeg"}})
    return {"id": i, "kind": "track", "title": title, "permalink": f"t{i}", "permalink_url": f"https://soundcloud.com/artist-7/t{i}",
            "user": who or user(), "duration": 200000, "genre": "Chillout", "tag_list": "ambient", "license": license, "downloadable": downloadable,
            "streamable": True, "access": access, "policy": policy, "display_date": date, "artwork_url": ART,
            "media": {"transcodings": tcs}} | extra


def playlist(i, title, tracks, **extra):
    return {"id": i, "kind": "playlist", "title": title, "permalink": f"s{i}", "permalink_url": f"https://soundcloud.com/artist-7/sets/s{i}",
            "user": user(), "tracks": tracks, "track_count": len(tracks), "duration": 400000, "is_album": True, "set_type": "album",
            "display_date": NEWEST, "artwork_url": ART, "genre": "Chillout", "tag_list": "", "license": "cc-by"} | extra


class Site:
    """The router: records every request and answers like SoundCloud."""

    def __init__(self, files_url):
        self.calls, self.files_url, self.tracks, self.sets, self.stubs = [], files_url, [], [], {}
        self.profile_tracks, self.users, self.fail, self.reject_ids, self.api_status = {}, {}, None, 0, 0
        self.stream_url = None

    def url_of(self, url):
        return urllib.parse.urlsplit(url), dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def __call__(self, method, url, headers=None, min_interval=0, **kw):
        self.calls.append((method, url, dict(headers or {}), min_interval))
        u, q = self.url_of(url)
        if self.fail:
            raise net.NetError(self.fail)
        if u.netloc == "soundcloud.com":
            return answer(body=b'<script src="https://a-v2.example.invalid/app.js"></script>', headers={"Content-Type": "text/html"})
        if u.netloc == "a-v2.example.invalid":
            return answer(body=f'{{"client_id":"{CLIENT_ID}"}}'.encode())
        if self.api_status:
            raise net.NetError(f"HTTP {self.api_status} from api-v2.soundcloud.com", self.api_status)
        if q.get("client_id") != CLIENT_ID or self.reject_ids > 0:
            self.reject_ids = max(0, self.reject_ids - 1)
            raise net.NetError("HTTP 401", 401)
        if u.path == "/search/tracks":
            return answer(body={"collection": [t for t in self.tracks if all(w in t["title"].lower() for w in q["q"].lower().split()) or q["q"] == "*"]})
        if u.path == "/search/playlists":
            return answer(body={"collection": [s for s in self.sets if all(w in (s["user"]["username"] + " " + s["title"]).lower() for w in q["q"].lower().split())]})
        if u.path == "/resolve":
            found = next((x for x in self.tracks + self.sets + list(self.users.values()) if x.get("permalink_url") == q["url"]), None)
            return answer(body=found or {"kind": "x"})
        if u.path.startswith("/users/") and u.path.endswith("/tracks"):
            return answer(body={"collection": self.profile_tracks.get(int(u.path.split("/")[2]), [])})
        if u.path == "/tracks":
            ids = [int(x) for x in q["ids"].split(",")]
            return answer(body=[t for t in self.tracks if t["id"] in ids])
        if u.path.startswith("/media/"):
            return answer(body={"url": self.stream_url or f"{self.files_url}/file/{u.path.split('/')[2]}.mp3"})
        raise net.NetError(f"HTTP 404 from {url}", 404)


class Base(unittest.TestCase):
    def setUp(self):
        self.files = FakeServer().serve(self)
        real_open = arr.open_url

        def open_url(req, timeout):
            """Answers an https address under example.invalid from the loopback server (the port after the host, else the file server's)."""
            m = re.match(r"https://[^/:]+\.example\.invalid(?::(\d+))?", req.full_url)
            if m:
                local = f"http://127.0.0.1:{m.group(1)}" if m.group(1) else self.files.url
                req = urllib.request.Request(local + req.full_url[m.end():], headers=dict(req.header_items()))
            return real_open(req, timeout)
        p = mock.patch.object(arr, "open_url", open_url)
        p.start()
        self.addCleanup(p.stop)
        self.files.on("GET", "/file/", lambda req: (200, {"Content-Type": "audio/mpeg"}, b"ID3" + b"\0" * 2045))
        self.files.on("GET", "/cover", lambda req: (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8" + b"\0" * 62))
        self.site = Site(CDN)
        for p in (mock.patch.object(net, "request", self.site), mock.patch.object(nuvem_de_som, "_CLIENT_ID", None),
                  mock.patch.object(nuvem_de_som, "default_session", side_effect=AssertionError("the library's own transport was used"))):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def indexer(self, **settings):
        return arr.SoundCloud({"id": 1, "name": "soundcloud", "settings": {"delay": "0"} | settings})

    def client(self, **settings):
        c = arr.SoundCloudClient({"id": 2, "name": "soundcloud", "settings": {"delay": "0", "download_dir": os.path.join(self.tmp, "dl")} | settings},
                                 self.tmp)
        os.makedirs(os.path.join(self.tmp, "dl"), exist_ok=True)
        c.start = lambda: None
        return c

    def api_calls(self, part):
        return [c for c in self.site.calls if part in c[1] and "api-v2" in c[1]]

    def grab(self, client, release):
        client.add(release)
        client.queue.run_one(client.run)
        return client.status()[-1]

    def release_of(self, name):
        r = self.indexer().search({"term": name})
        self.assertTrue(r, name)
        return r[0]


class Plugin(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            eps = tomllib.load(f)["project"]["entry-points"]
        self.site = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.site, True)
        info = os.path.join(self.site, "nuvem_de_som_fixture-9.9.dist-info")
        os.makedirs(info)
        with open(os.path.join(info, "METADATA"), "w") as f:
            f.write("Metadata-Version: 2.1\nName: nuvem-de-som-fixture\nVersion: 9.9\n")
        with open(os.path.join(info, "entry_points.txt"), "w") as f:
            for group, items in eps.items():
                f.write(f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in items.items()))
        sys.path.insert(0, self.site)
        self.addCleanup(sys.path.remove, self.site)
        importlib.invalidate_caches()

    def row(self, kind, enabled=()):
        return next(r for r in plugins.listing(kind, enabled) if r["name"] == "soundcloud")

    def test_installed_is_listed_disabled_and_enabled_only_by_name(self):
        for kind in ("indexer", "client"):
            self.assertEqual(self.row(kind)["status"], "disabled", self.row(kind))
            self.assertEqual(self.row(kind, ["soundcloud"])["status"], "enabled")
        self.assertIs(plugins.classes("indexer", ["soundcloud"])["soundcloud"], arr.SoundCloud)
        self.assertIs(plugins.classes("client", ["soundcloud"])["soundcloud"], arr.SoundCloudClient)
        self.assertNotIn("soundcloud", plugins.classes("indexer", []))
        self.assertFalse(getattr(arr.SoundCloud, "adult", False) or getattr(arr.SoundCloudClient, "adult", False))

    def test_api_version_is_the_one_arrkit_speaks(self):
        self.assertEqual((arr.API_VERSION, arr.SoundCloud.API_VERSION, arr.SoundCloudClient.API_VERSION), (plugins.API_VERSION,) * 3)

    def test_arrkit_is_an_extra_and_never_a_dependency(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            project = tomllib.load(f)["project"]
        self.assertFalse([d for d in project["dependencies"] if d.lower().startswith("arrkit")])
        self.assertEqual(project["optional-dependencies"]["arr"], ["arrkit>=0.1.0a2"])

    def test_the_library_imports_without_arrkit(self):
        code = "import sys; sys.modules['arrkit'] = None; import nuvem_de_som; print('nuvem_de_som.arr' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "False"), out.stderr)


class Searching(Base):
    def setUp(self):
        super().setUp()
        self.site.tracks = [track(1, "Slow Rain"), track(2, "Slow Snow", downloadable=False, license="all-rights-reserved"),
                            track(3, "Slow Preview", access="preview"), track(4, "Slow Snip", snipped=True), track(5, "Slow Open", downloadable=False, license="cc-by-nc-sa"),
                            track(6, "Slow Hls", progressive=False)]

    def test_a_track_becomes_a_typed_release(self):
        r = next(x for x in self.indexer(search_in="tracks").search({"term": "slow rain"}))
        self.assertEqual((r["title"], r["guid"], r["protocol"], r["kind"]), ("Example Artist - Slow Rain [MP3 128]", "soundcloud:track:1", "soundcloud", "download"))
        self.assertEqual((r["codec"], r["bitrate"], r["quality"], r["license"], r["runtime"]), ("audio/mpeg", 128, "MP3-128", "CC-BY-4.0", 200))
        self.assertEqual((r["downloadable"], r["streamable"], r["sc_kind"], r["soundcloud_id"]), (True, True, "track", "1"))
        self.assertIn("chillout", [g.lower() for g in r["genres"]])
        self.assertEqual((r["url"], r["published"], r["size"]), ("https://soundcloud.com/artist-7/t1", NEWEST[:10], 200 * 128 * 125))

    def test_requests_go_through_arrkit_net_spaced_and_with_arrkits_user_agent(self):
        self.indexer(delay="3", search_in="tracks").search({"term": "slow rain"})
        for method, url, headers, interval in self.site.calls:
            self.assertEqual(interval, 3, url)
            self.assertFalse([k for k in headers if k.lower() == "user-agent"], f"the library's User-Agent was sent to {url}")
        self.assertTrue(self.api_calls("/search/tracks"))
        self.assertIn(f"client_id={CLIENT_ID}", self.api_calls("/search/tracks")[0][1])

    def test_licence_and_flags_decide_download_or_stream(self):
        kinds = {r["soundcloud_id"]: (r["kind"], bool(r["refusal"])) for r in self.indexer(search_in="tracks").search({"term": "slow"})}
        self.assertEqual(kinds[("1")], ("download", False))
        self.assertEqual(kinds["2"], ("stream", True))
        self.assertEqual(kinds["5"], ("download", False))
        self.assertEqual(kinds["6"], ("stream", True))

    def test_go_plus_previews_are_left_out(self):
        ids = {r["soundcloud_id"] for r in self.indexer(search_in="tracks").search({"term": "slow"})}
        self.assertEqual(ids, {"1", "2", "5", "6"})

    def test_artist_and_album_make_the_search_text_and_other_titles_follow(self):
        self.site.sets = [playlist(10, "Quiet Album", [track(11, "One"), track(12, "Two")])]
        found = self.indexer(search_in="sets").search({"artist": "Example Artist", "album": "Quiet Album", "title": "Quiet Album"})
        self.assertEqual([r["guid"] for r in found], ["soundcloud:set:10"])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(self.api_calls("/search/playlists")[0][1]).query)["q"], ["Example Artist Quiet Album"])
        self.site.calls.clear()
        found = self.indexer(search_in="sets").search({"term": "nothing here", "titles": ["Quiet Album"]})
        self.assertEqual(len(found), 1)
        self.assertEqual(len(self.api_calls("/search/playlists")), 2)

    def test_at_most_three_terms_are_tried(self):
        self.indexer().search({"term": "a1", "title": "a2", "titles": ["a3", "a4", "a5"]})
        self.assertEqual(len(self.api_calls("/search/playlists")), 3)

    def test_search_in_picks_the_kinds(self):
        self.indexer(search_in="tracks").search({"term": "slow"})
        self.assertEqual((len(self.api_calls("/search/playlists")), len(self.api_calls("/search/tracks"))), (0, 1))
        self.site.calls.clear()
        self.indexer(search_in="sets").search({"term": "slow"})
        self.assertEqual((len(self.api_calls("/search/playlists")), len(self.api_calls("/search/tracks"))), (1, 0))

    def test_a_set_is_one_release_with_its_tracklist(self):
        self.site.sets = [playlist(10, "Quiet Album", [track(11, "One"), track(12, "Two"), {"id": 13, "kind": "track"}])]
        (r,) = self.indexer(search_in="sets").search({"term": "quiet"})
        self.assertEqual((r["title"], r["sc_kind"], r["kind"], r["album"], r["track_count"], r["quality"]), ("Example Artist - Quiet Album [MP3 128]", "set", "download", True, 3, "MP3-128"))
        self.assertEqual([(t["position"], t["title"], t["seconds"]) for t in r["tracks"]], [(1, "One", 200.0), (2, "Two", 200.0)])

    def test_a_set_with_a_preview_track_is_left_out_and_one_with_a_closed_track_is_a_stream(self):
        self.site.sets = [playlist(10, "Quiet A", [track(11, "One"), track(12, "Two", access="preview")]),
                          playlist(20, "Quiet B", [track(21, "One"), track(22, "Two", downloadable=False, license="all-rights-reserved")])]
        found = self.indexer(search_in="sets").search({"term": "quiet"})
        self.assertEqual([(r["guid"], r["kind"]) for r in found], [("soundcloud:set:20", "stream")])
        self.assertIn("Two", found[0]["refusal"])

    def test_a_401_refreshes_the_client_id_once(self):
        self.site.reject_ids = 1
        self.assertTrue(self.indexer(search_in="tracks").search({"term": "slow rain"}))
        self.assertEqual(len([c for c in self.site.calls if c[1] == "https://soundcloud.com/"]), 2)

    def test_an_error_status_other_than_401_or_403_does_not_refresh_the_client_id(self):
        self.site.api_status = 500
        with self.assertRaises(IndexerError) as cm:
            self.indexer(search_in="tracks").search({"term": "slow"})
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(len([c for c in self.site.calls if c[1] == "https://soundcloud.com/"]), 1)

    def test_a_service_that_does_not_answer_is_an_indexer_error_without_the_client_id(self):
        self.site.fail = "api-v2.soundcloud.com did not answer: timed out"
        with self.assertRaises(IndexerError) as cm:
            self.indexer().search({"term": "x"})
        self.assertNotIn(CLIENT_ID, str(cm.exception))
        self.assertTrue(str(cm.exception).startswith("soundcloud:"))

    def test_an_empty_query_asks_nothing(self):
        self.assertEqual(self.indexer().search({}), [])
        self.assertEqual(self.site.calls, [])

    def test_test_reports_failure(self):
        self.assertEqual(self.indexer().test()[0], True)
        self.site.fail = "down"
        ok, msg = self.indexer().test()
        self.assertFalse(ok)
        self.assertNotIn(CLIENT_ID, msg)


class Feed(Base):
    def setUp(self):
        super().setUp()
        self.profile = "https://soundcloud.com/artist-7"
        self.site.profile_tracks = {7: [track(1, "New One", date="2999-01-02T00:00:00Z"), track(2, "Old One", date=OLD),
                                        track(3, "Newer Preview", access="preview", date=NEWEST), track(4, "Newest", date="2999-02-01T00:00:00Z")]}
        self.site.users[self.profile] = {"kind": "user", "id": 7, "permalink_url": self.profile}

    def test_rss_lists_recent_uploads_of_followed_profiles_newest_first(self):
        found = self.indexer(follow=self.profile).rss()
        self.assertEqual([r["soundcloud_id"] for r in found], ["4", "1"])

    def test_rss_follows_nothing_by_default(self):
        self.assertEqual(self.indexer().rss(), [])
        self.assertEqual(self.site.calls, [])

    def test_a_bad_profile_does_not_hide_a_good_one_but_all_bad_is_an_error(self):
        self.assertEqual(len(self.indexer(follow=f"https://soundcloud.com/nobody, {self.profile}").rss()), 2)
        with self.assertRaises(IndexerError) as cm:
            self.indexer(follow="https://soundcloud.com/nobody").rss()
        self.assertIn("nobody", str(cm.exception))

    def test_a_readable_profile_with_nothing_new_is_not_an_error_beside_an_unreadable_one(self):
        self.site.profile_tracks[7] = [track(2, "Old One", date=OLD)]
        self.assertEqual(self.indexer(follow=f"https://soundcloud.com/nobody, {self.profile}").rss(), [])

    def test_rss_days_bounds_the_window(self):
        self.assertEqual(self.indexer(follow=self.profile, rss_days="100000").rss().__len__(), 3)


class Grabbing(Base):
    def setUp(self):
        super().setUp()
        self.site.tracks = [track(1, "Slow Rain"), track(2, "Slow Snow", downloadable=False, license="all-rights-reserved"),
                            track(3, "Slow Preview", access="preview")]

    def test_a_track_is_downloaded_with_its_name_cover_and_file_list(self):
        c = self.client()
        s = self.grab(c, self.release_of("slow rain"))
        self.assertEqual((s["state"], s["message"]), ("completed", ""))
        names = sorted(os.listdir(s["path"]))
        self.assertEqual(names, ["Example Artist - Slow Rain.mp3", "cover.jpg"])
        self.assertEqual(s["files"], ["Example Artist - Slow Rain.mp3"])
        self.assertEqual(open(os.path.join(s["path"], names[0]), "rb").read()[:3], b"ID3")
        self.assertTrue(os.path.realpath(s["path"]).startswith(os.path.realpath(os.path.join(self.tmp, "dl")) + os.sep))

    def test_the_stream_is_fetched_from_the_address_soundcloud_gave_for_the_progressive_copy(self):
        self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual([c.path for c in self.files.requests("GET", "/file/")], ["/file/1.mp3"])
        self.assertTrue(self.api_calls("/media/1/sq/progressive"))
        self.assertFalse(self.api_calls("/media/1/sq/hls"))

    def test_a_stream_only_release_is_refused_with_its_reason(self):
        r = next(x for x in self.indexer().search({"term": "slow snow"}))
        with self.assertRaises(ClientError) as cm:
            self.client().add(r)
        self.assertIn("uploader has not enabled downloads", str(cm.exception))
        self.assertEqual(self.client().queue.load(), [])

    def test_only_soundcloud_addresses_are_taken(self):
        for url in ("http://soundcloud.com/a/b", "https://evilsoundcloud.com/a/b", "https://soundcloud.com.example.org/a", "file:///etc/passwd", ""):
            with self.assertRaises(ClientError, msg=url):
                self.client().add({"url": url, "kind": "download", "title": "x"})

    def test_a_track_soundcloud_does_not_stream_is_refused_even_when_downloads_are_enabled(self):
        self.site.tracks.append(track(8, "Slow Mute", streamable=False))
        s = self.grab(self.client(), dict(self.release_of("slow rain"), url="https://soundcloud.com/artist-7/t8", guid="soundcloud:track:8"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("does not stream", s["message"])
        self.assertEqual(self.files.requests("GET"), [])

    def test_a_stream_address_that_is_not_https_is_refused_and_nothing_is_copied(self):
        secret = os.path.join(self.tmp, "local-secret.mp3")
        with open(secret, "wb") as f:
            f.write(b"ID3 not for download")
        for bad in (f"file://{secret}", self.files.url + "/file/1.mp3"):
            self.site.stream_url = bad
            s = self.grab(self.client(), self.release_of("slow rain"))
            self.assertEqual(s["state"], "failed", bad)
            self.assertIn("not https", s["message"])
            self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [], bad)
            self.assertEqual(self.files.requests("GET", "/file/"), [], bad)

    def test_a_go_plus_track_is_refused_at_grab_time_even_when_the_release_said_download(self):
        c = self.client()
        s = self.grab(c, dict(self.release_of("slow rain"), url="https://soundcloud.com/artist-7/t3", guid="soundcloud:track:3"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("Go+", s["message"])
        self.assertEqual(self.files.requests("GET"), [])

    def test_downloads_are_judged_on_fresh_data_not_on_the_release(self):
        c = self.client()
        s = self.grab(c, dict(self.release_of("slow rain"), url="https://soundcloud.com/artist-7/t2", guid="soundcloud:track:2"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("has not enabled downloads", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_a_track_with_only_an_hls_stream_is_refused_and_a_playlist_body_is_not_saved(self):
        self.site.tracks.append(track(6, "Slow Hls", progressive=False))
        s = self.grab(self.client(), dict(self.release_of("slow rain"), url="https://soundcloud.com/artist-7/t6"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("progressive", s["message"])
        self.files.routes.insert(0, ("GET", "/file/1", (200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U")))
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("not audio", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_an_http_error_from_the_stream_host_fails_without_the_query(self):
        self.files.routes.insert(0, ("GET", "/file/", (403, {}, b"no")))
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("HTTP 403", s["message"])
        self.assertNotIn(CLIENT_ID, s["message"])

    def test_a_missing_cover_does_not_fail_the_download_or_leave_a_part(self):
        self.files.routes.insert(0, ("GET", "/cover", (500, {}, b"")))
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed")
        self.assertEqual(os.listdir(s["path"]), ["Example Artist - Slow Rain.mp3"])

    def test_a_cc0_track_without_the_download_flag_is_downloaded(self):
        self.site.tracks = [track(1, "Slow Zero", downloadable=False, license="no-rights-reserved")]
        r = self.release_of("slow zero")
        self.assertEqual((r["kind"], r["license"]), ("download", "CC0-1.0"))
        s = self.grab(self.client(), r)
        self.assertEqual((s["state"], s["files"]), ("completed", ["Example Artist - Slow Zero.mp3"]))

    def test_a_cover_answer_that_breaks_the_http_protocol_keeps_the_audio(self):
        real = arr.open_url

        def open_url(req, timeout):
            if req.full_url.endswith("cover.jpg"):
                raise http.client.IncompleteRead(b"")
            return real(req, timeout)
        with mock.patch.object(arr, "open_url", open_url):
            s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(os.listdir(s["path"]), ["Example Artist - Slow Rain.mp3"])

    def test_a_cover_host_sending_a_bad_content_length_keeps_the_audio(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: abc\r\n\r\nxx")
        threading.Thread(target=serve, daemon=True).start()
        self.site.tracks[0]["artwork_url"] = f"https://cover.example.invalid:{srv.getsockname()[1]}/cover.jpg"
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual([n for n in os.listdir(s["path"]) if n.endswith(".mp3")], ["Example Artist - Slow Rain.mp3"])
        self.assertFalse([n for n in os.listdir(s["path"]) if n.endswith(".part")])

    def test_content_length_that_is_not_ascii_digits_is_an_error_and_an_absent_one_is_unknown(self):
        def headers(*values):
            m = Message()
            for v in values:
                m["Content-Length"] = v
            return m
        for bad in ("abc", "\u00b2", "\u0663", "-5", "1.5", "", "+5", "5_0", "5, 5"):
            with self.assertRaises(ClientError, msg=repr(bad)):
                arr.length(headers(bad))
        with self.assertRaises(ClientError):
            arr.length(headers("5", "5"))
        self.assertEqual((arr.length(headers(" 42 ")), arr.length(headers())), (42, 0))

    def test_a_cover_shorter_than_announced_leaves_no_part(self):
        self.files.routes.insert(0, ("GET", "/cover", (200, {"Content-Type": "image/jpeg", "Content-Length": "9999"}, b"xx")))
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(os.listdir(s["path"]), ["Example Artist - Slow Rain.mp3"])

    def test_a_cover_that_stops_sending_leaves_no_part(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        held = []

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                held.append(conn)
                conn.recv(4096)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 9999\r\n\r\nxx")
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in held])
        self.site.tracks[0]["artwork_url"] = f"https://cover.example.invalid:{srv.getsockname()[1]}/cover.jpg"
        s = self.grab(self.client(timeout="1"), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(os.listdir(s["path"]), ["Example Artist - Slow Rain.mp3"])

    def test_the_high_quality_progressive_stream_is_preferred_over_the_standard_one(self):
        self.site.tracks = [track(1, "Slow Rain", hq=True)]
        r = self.release_of("slow rain")
        self.assertEqual((r["quality"], r["bitrate"]), ("MP3-256", 256))
        self.grab(self.client(), r)
        self.assertTrue(self.api_calls("/media/1/hq/progressive"))
        self.assertFalse(self.api_calls("/media/1/sq/progressive"))

    def test_a_cancelled_job_leaves_no_file_and_no_folder(self):
        c = self.client()
        c.add(self.release_of("slow rain"))
        job = c.queue.load()[0]

        class Ctl:
            cancelled = False

            def progress(self, pct):
                self.cancelled = True

            def set(self, **kw):
                pass
        self.assertEqual(c.run(job, Ctl()), "cancelled")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_long_names_stay_within_255_bytes_with_their_part_suffix(self):
        title = "é" * 400
        self.site.tracks = [track(1, "Slow " + title)]
        s = self.grab(self.client(), self.release_of("slow"))
        self.assertEqual(s["state"], "completed")
        for name in os.listdir(s["path"]) + [os.path.basename(s["path"])]:
            self.assertLessEqual(len(name.encode()), 255 - len(".part") if name.endswith(".mp3") else 255, name)
        self.assertTrue(arr.SoundCloudClient.file_name("x" * 900, "m4a").endswith(".m4a"))
        self.assertLessEqual(len((arr.SoundCloudClient.file_name("中" * 900, "mp3") + ".part").encode()), 255)

    def test_names_cannot_leave_the_job_folder(self):
        self.site.tracks = [track(1, "Slow ../../escape", who=user(name="a/b\\c"))]
        s = self.grab(self.client(), self.release_of("slow"))
        self.assertEqual(s["state"], "completed")
        self.assertTrue(all(os.sep not in n for n in os.listdir(s["path"])))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [os.path.basename(s["path"])])

    def test_remove_deletes_the_job_folder_on_request(self):
        c = self.client()
        s = self.grab(c, self.release_of("slow rain"))
        c.remove(s["client_id"], delete_files=True)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])
        self.assertEqual(c.status(), [])

    def test_progress_is_reported(self):
        c = self.client()
        c.add(self.release_of("slow rain"))
        seen = []

        class Ctl:
            cancelled = False

            def progress(self, pct):
                seen.append(pct)

            def set(self, **kw):
                pass
        c.run(c.queue.load()[0], Ctl())
        self.assertTrue(seen and seen[-1] == 100 or seen[-1] >= 99, seen)


class Ctl:
    cancelled = False


class Body:
    """A response as read_capped sees it: headers and read1, nothing else (a read() would block, as on a trickling socket)."""

    def __init__(self, chunks, length=None, clock=None):
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.chunks, self.clock = list(chunks), clock

    def read1(self, n):
        if self.clock:
            self.clock[0] += 1
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, n):
        raise AssertionError("read blocks until n bytes have arrived; only read1 may be used")


class ReadCapped(unittest.TestCase):
    def read(self, body, limit=100, budget=lambda declared: 10, **kw):
        sink = io.BytesIO()
        return arr.read_capped(body, limit, sink, budget, **kw), sink.getvalue()

    def test_it_reads_with_read1_only(self):
        self.assertEqual(self.read(Body([b"ab", b"cd"])), (4, b"abcd"))

    def test_a_deadline_that_passes_between_two_reads_stops_it(self):
        clock = [0]
        body = Body([b"a"] * 50, clock=clock)
        with mock.patch.object(arr.time, "monotonic", lambda: clock[0]):
            with self.assertRaises(arr.TooSlow):
                self.read(body, budget=lambda declared: 5)
        self.assertEqual(len(body.chunks), 44)

    def test_a_cancel_stops_it_between_two_reads(self):
        body, calls = Body([b"a"] * 50), [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 3
        self.assertEqual(self.read(body, cancelled=cancelled)[0], 3)


class Fetching(Base):
    """SoundCloudClient.save and .cover against a real TLS server on the loopback (a self-signed certificate made here) and a plain http one
    that must never be reached."""

    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.mkdtemp()
        cls.cert, cls.key = os.path.join(cls.certs, "c.pem"), os.path.join(cls.certs, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", cls.key, "-out", cls.cert, "-days", "2",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certs, True)

    def setUp(self):
        super().setUp()
        self.hits = []
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                test.hits.append((self.server.kind, self.path))
                if self.path.startswith("/chain/") and self.path != "/chain/0":
                    self.send_response(302)
                    self.send_header("Location", f"https://127.0.0.1:{test.tls}/chain/{int(self.path[7:]) - 1}")
                    self.end_headers()
                    return
                if self.server.kind == "http" or self.path.startswith(("/ok", "/chain/0")):
                    body = b"ID3" + b"\0" * 100
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/to-http"):
                    where = f"http://127.0.0.1:{test.plain}/plain.mp3"
                elif self.path.startswith("/to-file"):
                    where = "file:///etc/hostname"
                elif self.path.startswith("/to-ftp"):
                    where = "ftp://127.0.0.1/x.mp3"
                else:
                    self.send_error(404)
                    return
                self.send_response(302)
                self.send_header("Location", where)
                self.end_headers()

        def serve(kind):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            srv.kind = kind
            if kind == "https":
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self.cert, self.key)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            return srv.server_address[1]
        self.plain, self.tls = serve("http"), serve("https")
        p = mock.patch.object(ssl, "_create_default_https_context", ssl._create_unverified_context)
        p.start()
        self.addCleanup(p.stop)
        self.dir = os.path.join(self.tmp, "t")
        os.makedirs(self.dir)
        self.c = self.client()

    def https(self, path):
        return f"https://127.0.0.1:{self.tls}{path}"

    def save(self, url, name="a.mp3"):
        self.written = []
        self.c.save(url, self.dir, name, Ctl(), lambda pct: None, self.written)

    def test_a_stream_over_tls_is_written(self):
        self.save(self.https("/ok"))
        self.assertEqual(open(os.path.join(self.dir, "a.mp3"), "rb").read()[:3], b"ID3")
        self.assertEqual(os.listdir(self.dir), ["a.mp3"])

    def test_a_stream_redirected_to_a_non_https_address_is_not_followed(self):
        for path in ("/to-http", "/to-file", "/to-ftp"):
            with self.subTest(path):
                with self.assertRaisesRegex(ClientError, "not https|HTTP 302"):  # urllib itself refuses file:// before the handler is asked
                    self.save(self.https(path))
                self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual([h for h in self.hits if h[0] == "http"], [])

    def test_a_cover_redirected_to_a_non_https_address_is_not_followed_and_leaves_no_file(self):
        for path in ("/to-http", "/to-file"):
            written = []
            self.c.cover({"artwork_url": self.https(path + "/art.jpg")}, self.dir, Ctl(), written)
            self.assertEqual((os.listdir(self.dir), written), ([], []), path)
        self.assertEqual([h for h in self.hits if h[0] == "http"], [])

    def test_five_redirects_are_followed_and_the_sixth_is_refused(self):
        self.save(self.https("/chain/5"))
        self.assertEqual(len(self.hits), 6)
        self.assertEqual(os.listdir(self.dir), ["a.mp3"])
        self.hits.clear()
        with self.assertRaisesRegex(ClientError, "HTTP 302"):
            self.save(self.https("/chain/6"), name="b.mp3")
        self.assertEqual(len(self.hits), 6)
        self.assertEqual(os.listdir(self.dir), ["a.mp3"])

    def test_an_address_with_whitespace_or_a_control_character_is_refused_unchanged(self):
        for tail in ("\n", " ", "\t", "\r\n", "\x00", "\x7f", " ", "/a b.mp3"):
            with self.subTest(tail=repr(tail)):
                with self.assertRaisesRegex(ClientError, "not https"):
                    self.save(self.https("/ok") + tail)
        with self.assertRaisesRegex(ClientError, "not https"):
            self.save(" " + self.https("/ok"))
        self.assertEqual((self.hits, os.listdir(self.dir)), ([], []))

    def test_a_release_address_with_a_trailing_newline_is_not_queued(self):
        self.site.tracks = [track(1, "Slow Rain")]
        r = dict(self.release_of("slow rain"), url="https://soundcloud.com/a/b\n")
        with self.assertRaisesRegex(ClientError, "soundcloud.com address"):
            self.c.add(r)

    def test_a_part_file_that_is_a_symlink_is_not_followed(self):
        victim = os.path.join(self.tmp, "victim")
        open(victim, "wb").write(b"KEEP")
        os.symlink(victim, os.path.join(self.dir, "a.mp3.part"))
        with self.assertRaisesRegex(ClientError, "already exists"):
            self.save(self.https("/ok"))
        self.assertEqual(open(victim, "rb").read(), b"KEEP")
        self.assertEqual(self.written, [])
        self.assertTrue(os.path.islink(os.path.join(self.dir, "a.mp3.part")))

    def test_an_existing_file_is_not_overwritten(self):
        final = os.path.join(self.dir, "a.mp3")
        open(final, "wb").write(b"PRECIOUS")
        with self.assertRaisesRegex(ClientError, "already exists"):
            self.save(self.https("/ok"))
        self.assertEqual(open(final, "rb").read(), b"PRECIOUS")
        self.assertEqual((self.written, self.hits), ([], []))

    def test_an_existing_cover_survives_a_cover_that_cannot_be_saved(self):
        final = os.path.join(self.dir, "cover.jpg")
        open(final, "wb").write(b"MINE")
        written = []
        self.c.cover({"artwork_url": self.https("/ok/cover.jpg")}, self.dir, Ctl(), written)
        self.assertEqual((open(final, "rb").read(), written), (b"MINE", []))

    def raw_server(self, answer):
        """A TLS server on the loopback that reads each request and calls `answer(tls)`, which writes raw bytes; the address it returns."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.addCleanup(srv.close)

        def one(conn):
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(4096)
                    answer(tls)
            except OSError:
                pass

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=one, args=(conn,), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        return f"https://127.0.0.1:{srv.getsockname()[1]}/x.mp3"

    def short_server(self, reply):
        """A TLS server that sends `reply` (raw bytes) to each request and closes."""
        return self.raw_server(lambda tls: tls.sendall(reply))

    def drip_server(self, declared="100000", kind="audio/mpeg"):
        """Announces `declared` bytes (none when it is None) and sends one every fifth of a second for twenty seconds."""
        def answer(tls):
            length = b"Content-Length: " + declared.encode() + b"\r\n" if declared else b""
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind.encode() + b"\r\n" + length + b"Connection: close\r\n\r\n")
            for _ in range(100):
                time.sleep(0.2)
                tls.sendall(b"A")
        return self.raw_server(answer)

    def endless_server(self, kind="audio/mpeg"):
        """Sends 1000-byte blocks with no Content-Length, 20 MB at most."""
        def answer(tls):
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind.encode() + b"\r\nConnection: close\r\n\r\n")
            for _ in range(20000):
                tls.sendall(b"A" * 1000)
        return self.raw_server(answer)

    def test_a_content_length_the_library_would_read_differently_is_an_error_and_keeps_nothing(self):
        for label, value in {"plain digits": "90000", "plus sign": "+90000", "underscore": "9_0000", "list": "90000, 90000"}.items():
            with self.subTest(label):
                url = self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: " + value.encode()
                                        + b"\r\nConnection: close\r\n\r\n" + b"A" * 3000)
                name = label.replace(" ", "-") + ".mp3"
                with self.assertRaises(ClientError) as raised:
                    self.save(url, name=name)
                self.assertRegex(str(raised.exception), "incomplete download" if label == "plain digits" else "Content-Length")
                self.assertFalse(os.path.exists(os.path.join(self.dir, name)))

    def test_a_job_whose_content_length_is_not_a_plain_number_fails_and_leaves_nothing(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.stream_url = self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: +90000\r\nConnection: close\r\n\r\n" + b"A" * 3000)
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertNotEqual(s["state"], "completed")
        self.assertIn("Content-Length", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_an_empty_audio_body_is_not_a_download(self):
        head = b"Content-Type: audio/mpeg\r\n"
        cases = {"length 0": b"HTTP/1.1 200 OK\r\n" + head + b"Content-Length: 0\r\n\r\n",
                 "closed with no body": b"HTTP/1.1 200 OK\r\n" + head + b"Connection: close\r\n\r\n",
                 "no content": b"HTTP/1.1 204 No Content\r\n" + head + b"\r\n"}
        for label, reply in cases.items():
            with self.subTest(label):
                name = label.replace(" ", "-") + ".mp3"
                with self.assertRaisesRegex(ClientError, "empty answer"):
                    self.save(self.short_server(reply), name=name)
                self.assertFalse(os.path.exists(os.path.join(self.dir, name)))

    def test_a_job_with_an_empty_audio_body_fails_and_leaves_nothing(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.stream_url = self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 0\r\n\r\n")
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertNotEqual(s["state"], "completed")
        self.assertIn("empty", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_the_limits_are_these_numbers(self):
        self.assertEqual((arr.MAX_REDIRECTS, arr.MAX_AUDIO, arr.MAX_COVER, arr.PAGE_SECONDS, arr.COVER_SECONDS),
                         (5, 256 * 1024 * 1024, 8 * 1024 * 1024, 120, 120))
        self.assertEqual((arr.AUDIO_SECONDS, arr.AUDIO_RATE, arr.AUDIO_SECONDS_MAX), (60, 32 * 1024, 3600))
        self.assertEqual((arr.audio_seconds(0), arr.audio_seconds(32 * 1024 * 100), arr.audio_seconds(10 ** 12)), (3600, 160, 3600))

    def test_an_answer_that_trickles_is_stopped_at_its_time_budget(self):
        with mock.patch.object(arr, "AUDIO_SECONDS", 1), mock.patch.object(arr, "AUDIO_SECONDS_MAX", 1):
            for declared in ("100000", None):
                started = time.monotonic()
                url = self.drip_server(declared)
                with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
                    self.save(url, name=f"{declared}.mp3")
                self.assertLess(time.monotonic() - started, 8)
                self.assertFalse(os.path.exists(os.path.join(self.dir, f"{declared}.mp3")))

    def test_a_cover_that_trickles_is_stopped_at_the_cover_budget(self):
        with mock.patch.object(arr, "COVER_SECONDS", 1):
            started = time.monotonic()
            with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
                self.save_cover(self.drip_server(kind="image/jpeg"))
            self.assertLess(time.monotonic() - started, 8)

    def save_cover(self, url, name="cover.jpg"):
        self.c.save(url, self.dir, name, Ctl(), lambda pct: None, [], audio=False)

    def small_budget(self, seconds=1):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name in ("AUDIO_SECONDS", "AUDIO_SECONDS_MAX", "COVER_SECONDS"):
            stack.enter_context(mock.patch.object(arr, name, seconds))
        stack.enter_context(mock.patch.object(arr, "PAGE_SECONDS", 120))

    def header_drip_server(self, kind="audio/mpeg", gap=0.1):
        """Sends a complete answer one byte at a time, the headers (400 bytes of them) taking forty seconds."""
        data = b"HTTP/1.1 200 OK\r\nContent-Type: " + kind.encode() + b"\r\nContent-Length: 10\r\nX-Pad: " + b"A" * 400 + b"\r\n\r\n0123456789"

        def answer(tls):
            for i in range(len(data)):
                tls.sendall(data[i:i + 1])
                time.sleep(gap)
        return self.raw_server(answer)

    def test_headers_that_trickle_are_stopped_at_the_time_budget(self):
        self.small_budget()
        for label, url in {"audio": self.header_drip_server(), "silent": self.raw_server(lambda tls: time.sleep(20))}.items():
            with self.subTest(label):
                started = time.monotonic()
                with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
                    self.save(url, name=label + ".mp3")
                self.assertLess(time.monotonic() - started, 6)
                self.assertFalse(os.path.exists(os.path.join(self.dir, label + ".mp3")))

    def test_a_cover_whose_headers_trickle_is_stopped_at_the_cover_budget(self):
        self.small_budget()
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
            self.save_cover(self.header_drip_server("image/jpeg"))
        self.assertLess(time.monotonic() - started, 6)

    def test_a_connection_that_never_completes_tls_is_stopped_at_the_time_budget(self):
        self.small_budget()
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        held = []
        self.addCleanup(srv.close)
        self.addCleanup(lambda: [c.close() for c in held])
        threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
            self.save(f"https://127.0.0.1:{srv.getsockname()[1]}/x.mp3")
        self.assertLess(time.monotonic() - started, 6)

    def test_a_cancel_ends_the_wait_for_headers(self):
        ctl = Ctl()
        ctl.cancelled = False
        timer = threading.Timer(0.5, lambda: setattr(ctl, "cancelled", True))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.written = []
        self.c.save(self.header_drip_server(), self.dir, "a.mp3", ctl, lambda pct: None, self.written)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_the_time_budget_is_shared_by_every_redirect(self):
        self.small_budget(2)
        seen, where = [], []

        def answer(tls):
            seen.append(1)
            time.sleep(0.8)
            if len(seen) <= 4:
                tls.sendall(b"HTTP/1.1 302 Found\r\nLocation: " + where[0].encode() + b"\r\nContent-Length: 0\r\n\r\n")
            else:
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 3\r\n\r\nID3")
        where.append(self.raw_server(answer))
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "did not finish within 2 seconds"):
            self.save(where[0])
        self.assertLess(time.monotonic() - started, 4)
        self.assertLess(len(seen), 5)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_once_the_headers_are_in_the_file_has_its_own_longer_budget(self):
        def answer(tls):
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 98304\r\nConnection: close\r\n\r\n")
            time.sleep(1.5)
            tls.sendall(b"A" * 98304)
        with mock.patch.object(arr, "AUDIO_SECONDS", 1), mock.patch.object(arr, "PAGE_SECONDS", 1):
            self.assertEqual(arr.audio_seconds(98304), 4)
            self.save(self.raw_server(answer))
        self.assertEqual(os.path.getsize(os.path.join(self.dir, "a.mp3")), 98304)

    def test_an_undeclared_body_that_goes_silent_is_not_taken_for_a_complete_one(self):
        def answer(tls):
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nConnection: close\r\n\r\n" + b"A" * 10)
            time.sleep(20)
        self.small_budget()
        started = time.monotonic()
        with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
            self.save(self.raw_server(answer))
        self.assertLess(time.monotonic() - started, 6)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_a_body_that_ends_cleanly_after_the_watchdog_fired_is_not_kept(self):
        """A shutdown can look like the end of an undeclared body (a socket without TLS, or TLS that hides the missing close)."""
        class Quiet:
            headers = Message()
            chunks = [b"A", b""]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read1(self, n):
                chunk = self.chunks.pop(0)
                if not chunk:
                    time.sleep(1.5)
                return chunk
        Quiet.headers["Content-Type"] = "audio/mpeg"
        self.small_budget()
        with mock.patch.object(arr, "open_url", lambda req, timeout: Quiet()):
            with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
                self.save("https://127.0.0.1:9/x.mp3")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_the_header_allowance_is_the_page_budget_when_that_is_shorter(self):
        with mock.patch.object(arr, "PAGE_SECONDS", 1):
            started = time.monotonic()
            with self.assertRaisesRegex(ClientError, "did not finish within 1 seconds"):
                self.save(self.raw_server(lambda tls: time.sleep(20)))
            self.assertLess(time.monotonic() - started, 6)

    def test_the_size_cap_is_exact_for_declared_and_undeclared_lengths(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
        with mock.patch.object(arr, "MAX_AUDIO", 4000):
            for n, ok in ((4000, True), (4001, False)):
                for label, framing in (("declared", b"Content-Length: %d\r\n" % n), ("undeclared", b"")):
                    with self.subTest(f"{label} {n}"):
                        name = f"{label}{n}.mp3"
                        url = self.short_server(head + framing + b"Connection: close\r\n\r\n" + b"A" * n)
                        if ok:
                            self.save(url, name=name)
                            self.assertEqual(os.path.getsize(os.path.join(self.dir, name)), n)
                        else:
                            with self.assertRaisesRegex(ClientError, "more than 4000 bytes"):
                                self.save(url, name=name)
                            self.assertFalse(os.path.exists(os.path.join(self.dir, name)))

    def test_the_cover_cap_is_exact_for_declared_and_undeclared_lengths(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
        with mock.patch.object(arr, "MAX_COVER", 100):
            for n, ok in ((100, True), (101, False)):
                for label, framing in (("declared", b"Content-Length: %d\r\n" % n), ("undeclared", b"")):
                    with self.subTest(f"{label} {n}"):
                        name = f"{label}{n}.jpg"
                        url = self.short_server(head + framing + b"Connection: close\r\n\r\n" + b"A" * n)
                        if ok:
                            self.save_cover(url, name)
                            self.assertEqual(os.path.getsize(os.path.join(self.dir, name)), n)
                        else:
                            with self.assertRaisesRegex(ClientError, "more than 100 bytes"):
                                self.save_cover(url, name)

    def test_a_cancel_is_seen_while_the_answer_trickles(self):
        ctl = Ctl()
        ctl.cancelled = False
        timer = threading.Timer(0.6, lambda: setattr(ctl, "cancelled", True))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.written = []
        self.c.save(self.drip_server(), self.dir, "a.mp3", ctl, lambda pct: None, self.written)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_a_job_that_trickles_fails_and_leaves_no_part_and_no_folder(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.stream_url = self.drip_server()
        with mock.patch.object(arr, "AUDIO_SECONDS", 1), mock.patch.object(arr, "AUDIO_SECONDS_MAX", 1):
            s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertNotEqual(s["state"], "completed")
        self.assertIn("did not finish", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_an_endless_body_without_a_length_is_stopped_at_the_size_cap(self):
        with mock.patch.object(arr, "MAX_AUDIO", 4000):
            with self.assertRaisesRegex(ClientError, "more than 4000 bytes"):
                self.save(self.endless_server())
            with self.assertRaisesRegex(ClientError, "more than 4000 bytes"):  # declared too long: refused before a byte is read
                self.save(self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 4001\r\n\r\n"), name="b.mp3")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.mp3")))

    def test_a_job_with_an_endless_body_fails_and_leaves_nothing(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.stream_url = self.endless_server()
        with mock.patch.object(arr, "MAX_AUDIO", 4000):
            s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertNotEqual(s["state"], "completed")
        self.assertIn("more than 4000 bytes", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_a_cover_over_the_cover_cap_is_dropped_and_the_audio_kept(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.tracks[0]["artwork_url"] = self.endless_server("image/jpeg").replace("/x.mp3", "/cover.jpg")
        with mock.patch.object(arr, "MAX_COVER", 4000):
            s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(os.listdir(s["path"]), ["Example Artist - Slow Rain.mp3"])

    def test_a_cover_that_is_not_an_image_is_refused_like_audio_that_is_not_audio(self):
        for kind in ("text/html", "audio/mpeg", "application/octet-stream", ""):
            with self.subTest(kind):
                head = b"HTTP/1.1 200 OK\r\n" + (b"Content-Type: " + kind.encode() + b"\r\n" if kind else b"") + b"Content-Length: 64\r\n\r\n"
                written = []
                self.c.cover({"artwork_url": self.short_server(head + b"A" * 64).replace("/x.mp3", "/cover.jpg")}, self.dir, Ctl(), written)
                self.assertEqual((os.listdir(self.dir), written), ([], []))
        with self.assertRaisesRegex(ClientError, "not an image"):
            self.c.save(self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 4\r\n\r\nabcd"), self.dir, "cover.jpg",
                        Ctl(), lambda pct: None, [], audio=False)

    def test_a_body_shorter_than_its_content_length_or_cut_in_a_chunk_is_not_kept(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
        cases = {"short": head + b"Content-Length: 90000\r\nConnection: close\r\n\r\n" + b"ID3" + b"\0" * 2997,
                 "cut mid-chunk": head + b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n1000\r\n" + b"\0" * 300,
                 "chunks without the last one": head + b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n10\r\n" + b"\0" * 16 + b"\r\n"}
        for label, reply in cases.items():
            with self.subTest(label):
                with self.assertRaises(ClientError) as raised:
                    self.save(self.short_server(reply), name=label.replace(" ", "-") + ".mp3")
                self.assertRegex(str(raised.exception), "incomplete download" if label == "short" else "IncompleteRead")
                self.assertEqual([n for n in os.listdir(self.dir) if not n.endswith(".part")], [])

    def test_a_download_cut_short_fails_the_job_and_leaves_no_file(self):
        self.site.tracks = [track(1, "Slow Rain")]
        self.site.stream_url = self.short_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: 90000\r\nConnection: close\r\n\r\nID3" + b"\0" * 2997)
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertNotEqual(s["state"], "completed")
        self.assertIn("incomplete", s["message"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])


class Sets(Base):
    def setUp(self):
        super().setUp()
        self.one, self.two = track(11, "One"), track(12, "Two")
        self.site.tracks = [self.one, self.two]
        self.site.sets = [playlist(10, "Quiet Album", [self.one, {"id": 12, "kind": "track"}])]

    def test_a_set_downloads_every_track_in_order_with_stubs_asked_for_by_id(self):
        s = self.grab(self.client(), self.indexer(search_in="sets").search({"term": "quiet"})[0])
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(s["files"], ["01 - Example Artist - One.mp3", "02 - Example Artist - Two.mp3"])
        self.assertEqual(sorted(os.listdir(s["path"])), ["01 - Example Artist - One.mp3", "02 - Example Artist - Two.mp3", "cover.jpg"])
        self.assertIn("ids=12", self.api_calls("/tracks")[0][1])

    def test_the_delay_is_waited_between_the_files_of_a_set_and_not_before_the_first(self):
        release = self.indexer(search_in="sets").search({"term": "quiet"})[0]
        with mock.patch.object(arr.time, "sleep") as sleep:
            s = self.grab(self.client(delay="3"), release)
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual(sleep.call_args_list, [mock.call(3)])

    def test_a_set_with_one_closed_track_is_refused_whole_before_any_byte_is_fetched(self):
        release = self.indexer(search_in="sets").search({"term": "quiet"})[0]
        self.two.update(downloadable=False, license="all-rights-reserved")
        s = self.grab(self.client(), release)
        self.assertEqual(s["state"], "failed")
        self.assertIn("Two", s["message"])
        self.assertEqual(self.files.requests("GET"), [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_a_set_with_a_track_soundcloud_does_not_return_is_refused_whole(self):
        release = self.indexer(search_in="sets").search({"term": "quiet"})[0]
        self.site.tracks.remove(self.two)
        s = self.grab(self.client(), release)
        self.assertEqual(s["state"], "failed")
        self.assertIn("12", s["message"])
        self.assertEqual(self.files.requests("GET"), [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_a_set_listing_an_entry_without_an_id_is_refused_whole(self):
        release = self.indexer(search_in="sets").search({"term": "quiet"})[0]
        self.site.sets[0]["tracks"].append({"kind": "track"})
        s = self.grab(self.client(), release)
        self.assertEqual(s["state"], "failed")
        self.assertEqual(self.files.requests("GET"), [])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])

    def test_a_set_that_fails_halfway_removes_what_it_wrote(self):
        release = self.indexer(search_in="sets").search({"term": "quiet"})[0]
        self.files.routes.insert(0, ("GET", "/file/12", (500, {}, b"")))
        s = self.grab(self.client(), release)
        self.assertEqual(s["state"], "failed")
        self.assertEqual(self.files.requests("GET", "/file/11")[0].path, "/file/11.mp3")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "dl")), [])


if __name__ == "__main__":
    unittest.main()
