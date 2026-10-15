"""nuvem_de_som as an arrkit indexer and download client, against a fake SoundCloud.

arrkit.net.request is replaced by a router that answers as the home page, its script bundle and API v2 do; the stream addresses point at a
file server on 127.0.0.1. Every name is a placeholder and nothing reaches the real site. Run with `python -m pytest tests_arrkit`;
arrkit (the `arr` extra) is needed."""
import http.client
import importlib
import json
import socket
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
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
ART = "http://127.0.0.1:9/cover.jpg"  # replaced by the file server's address in Base.setUp


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
            return answer(body={"url": f"{self.files_url}/file/{u.path.split('/')[2]}.mp3"})
        raise net.NetError(f"HTTP 404 from {url}", 404)


class Base(unittest.TestCase):
    def setUp(self):
        global ART
        self.files = FakeServer().serve(self)
        ART = self.files.url + "/cover.jpg"
        self.files.on("GET", "/file/", lambda req: (200, {"Content-Type": "audio/mpeg"}, b"ID3" + b"\0" * 2045))
        self.files.on("GET", "/cover", lambda req: (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8" + b"\0" * 62))
        self.site = Site(self.files.url)
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
        self.assertEqual(project["optional-dependencies"]["arr"], ["arrkit>=0.1.0a9"])

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

    def test_a_short_file_fails_and_leaves_nothing(self):
        self.files.routes.insert(0, ("GET", "/file/", lambda req: (200, {"Content-Type": "audio/mpeg", "Content-Length": "9999"}, b"ID3" * 10)))
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "failed")
        self.assertIn("incomplete download", s["message"])
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
        real = arr.urllib.request.urlopen

        def urlopen(req, *a, **k):
            if req.full_url.endswith("cover.jpg"):
                raise http.client.IncompleteRead(b"")
            return real(req, *a, **k)
        with mock.patch.object(arr.urllib.request, "urlopen", urlopen):
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
        self.site.tracks[0]["artwork_url"] = f"http://127.0.0.1:{srv.getsockname()[1]}/cover.jpg"
        s = self.grab(self.client(), self.release_of("slow rain"))
        self.assertEqual(s["state"], "completed", s["message"])
        self.assertEqual([n for n in os.listdir(s["path"]) if n.endswith(".mp3")], ["Example Artist - Slow Rain.mp3"])
        self.assertFalse([n for n in os.listdir(s["path"]) if n.endswith(".part")])

    def test_content_length_that_is_not_ascii_digits_is_unknown(self):
        for bad in ("abc", "\u00b2", "\u0663", "-5", "1.5", ""):
            self.assertEqual(arr.length({"Content-Length": bad}), 0, repr(bad))
        self.assertEqual(arr.length({"Content-Length": " 42 "}), 42)

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
        self.site.tracks[0]["artwork_url"] = f"http://127.0.0.1:{srv.getsockname()[1]}/cover.jpg"
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
