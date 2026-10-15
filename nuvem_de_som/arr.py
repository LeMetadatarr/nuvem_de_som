"""nuvem_de_som as an arrkit indexer and download client.

Installing nuvem_de_som into an app built on arrkit makes both available under the name ``soundcloud`` (entry points in the groups
``arrkit.indexers`` and ``arrkit.clients``). The app lists them as plugins and uses them only once its settings enable them. arrkit is
needed only here: the rest of the library never imports this module.

Every request to SoundCloud's API goes through arrkit.net: spaced by ``delay`` seconds per host, retried as arrkit retries, with arrkit's own
User-Agent, and no browser impersonation or bot-check work-around. The library's own session factory (unblock_requests, curl-cffi) is never
used; the library's SoundCloudAPI is given a plain session of this module's instead, and its client_id mechanism (read from the site's
script bundles, refreshed once when the API answers 401 or 403) is the one in use.

Indexer. ``search(query)`` searches tracks and sets (SoundCloud playlists, which is how albums are published) for ``term``, or for
"artist album", or for the item's ``title`` (the other names in ``titles`` are tried in turn until one finds something). A release is one
track or one set. It carries the library's typed fields: ``codec``, ``bitrate`` (kbps), ``quality`` (an audio quality name such as MP3-128),
``license`` (an SPDX id, or the raw SoundCloud value for all-rights-reserved), ``genres`` (the library's content_genres), ``runtime``
(seconds), and for a set ``tracks`` (position, title, seconds). ``kind`` is ``download`` when the client may fetch the release and
``stream`` when it may only be played; ``downloadable`` and ``streamable`` are the track's own flags and ``refusal`` says why a stream
release is not a download. Tracks that SoundCloud serves as a preview only (Go+) are left out. ``rss()`` lists the tracks the profiles
named in ``follow`` uploaded within ``rss_days``; the library has no tag feed, so tags cannot be followed.

Client. ``add(release)`` queues the release in a job queue in the app's state folder (arrkit's direct client, one job at a time). The job
asks SoundCloud again for the track or set, so the permission is judged on fresh data, and downloads into ``<download_dir>/<title>-<job>/``
only what is permitted, as described in docs/arrkit.md: the track's uploader enabled downloads or its licence is a Creative Commons one,
the full track is served (no Go+ preview) and a progressive stream exists. The file is the stream copy the library resolves, not the
uploader's original file. A set is all or nothing."""
import contextlib
import http.client
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import requests
from arrkit import net
from arrkit.bridge.sabnzbd import safe
from arrkit.clients import ClientError
from arrkit.clients.direct import CHUNK, Direct
from arrkit.clients.jobs import JobQueue
from arrkit.indexers import Indexer, IndexerError

from nuvem_de_som import (
    SoundCloudAPI,
    _build_genres,
    _map_license,
    _set_dict_to_release,
    _track_dict_to_release,
)

API_VERSION = 1
SLUG = "soundcloud"
API = "https://api-v2.soundcloud.com"
MAX_TERMS = 3
MAX_NAME = 255
OPEN_LICENSES = ("CC0-1.0", "CC-BY-")
EXTENSIONS = (("mpeg", "mp3"), ("opus", "opus"), ("ogg", "ogg"), ("mp4", "m4a"), ("aac", "m4a"))
SET_CHUNK = 50
MAX_REDIRECTS = 5
MAX_AUDIO = 256 * 1024 * 1024
MAX_COVER = 8 * 1024 * 1024
PAGE_SECONDS = 120
COVER_SECONDS = 120
AUDIO_SECONDS = 60
AUDIO_RATE = 32 * 1024
AUDIO_SECONDS_MAX = 3600


class Answer:
    """An arrkit.net Response in the shape the library reads from requests."""

    def __init__(self, status, headers, body, url=""):
        self.status_code, self.headers, self.content, self.url = status, headers, body, url
        self.text = body.decode("utf-8", "replace")

    def json(self):
        import json
        return json.loads(self.content or b"null")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code} from {net.host_of(self.url)}")


class Session:
    """The session SoundCloudAPI is given: arrkit.net with spacing and arrkit's User-Agent. An error status is returned as an answer, not
    raised, because the library reads 401 and 403 to refresh its client_id; a service that did not answer is a requests ConnectionError."""

    HEADERS = {"Accept": "application/json, text/html;q=0.9"}

    def __init__(self, delay):
        self.delay = delay

    def get(self, url, params=None, headers=None, timeout=30, **_):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        sent = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        try:
            r = net.request("GET", url, headers={**self.HEADERS, **sent}, timeout=timeout, min_interval=self.delay)
        except net.NetError as e:
            if e.status is None:
                raise requests.ConnectionError(str(e)) from None
            return Answer(e.status, e.headers, e.body, url)
        return Answer(r.status, r.headers, r.body, url)


@contextlib.contextmanager
def guard(error):
    """The library's and the network's errors become `error`, with the text of the failure and never a URL's query (it holds the client_id)."""
    try:
        yield
    except (IndexerError, ClientError):
        raise
    except Exception as e:
        raise error(f"soundcloud: {type(e).__name__}: {e}") from None


def words(value):
    return [w.strip() for w in str(value or "").split(",") if w.strip()]


def date_of(raw):
    stamp = raw.get("display_date") or raw.get("created_at") or ""
    return stamp[:10] if len(stamp) >= 10 else ""


def progressive(raw):
    """The transcodings of a track that are a direct file, best quality first (the order the library resolves them)."""
    rank = {"hq": 0, "sq": 1}
    found = [t for t in (raw.get("media") or {}).get("transcodings") or []
             if ((t.get("format") or {}).get("protocol") or "").lower() == "progressive" and t.get("url")]
    return sorted(found, key=lambda t: rank.get((t.get("quality") or "").lower(), 9))


def quality_of(codec, bitrate):
    """(quality name, text for a title) of a mime type and a bitrate in kbps, the names an audio profile uses (MP3-128, AAC-256, Opus)."""
    c, rate = str(codec or "").lower(), int(bitrate or 0)
    if "mpeg" in c or "mp3" in c:
        return (f"MP3-{rate}", f"MP3 {rate}") if rate else ("Unknown", "MP3")
    if "opus" in c:
        return "Opus", "Opus"
    if "mp4" in c or "aac" in c:
        if rate >= 240:
            return f"AAC-{rate}", f"AAC {rate}"
        return "AAC-VBR", "AAC"
    return "Unknown", ""


def extension_of(codec):
    c = str(codec or "").lower()
    return next((ext for key, ext in EXTENSIONS if key in c), "audio")


def restricted(raw):
    """True when SoundCloud serves the track only as a preview or not at all (Go+)."""
    return (raw.get("access") not in (None, "playable") or raw.get("policy") in ("SNIP", "BLOCK")
            or raw.get("monetization_model") == "SUB_HIGH_TIER"
            or any(t.get("snipped") for t in (raw.get("media") or {}).get("transcodings") or []))


def refusal(raw):
    """Why a track cannot be downloaded, or "" when it may be. A track the data does not describe fully (a stub in a set) is judged on
    what it has."""
    title = raw.get("title") or raw.get("permalink") or raw.get("id")
    if restricted(raw):
        return f"{title}: SoundCloud Go+ restricted, only a preview or nothing is available"
    if raw.get("streamable") is False:
        return f"{title}: SoundCloud does not stream this track"
    if "media" in raw and not progressive(raw):
        return f"{title}: SoundCloud offers no direct (progressive) stream for this track, only HLS or none, which is not downloaded"
    license = _map_license(raw.get("license"))
    if not raw.get("downloadable") and not license.startswith(OPEN_LICENSES):
        return (f"{title}: the uploader has not enabled downloads and the licence "
                f"({license or 'none given'}) does not allow copies")
    return ""


def length(headers):
    """The Content-Length of an answer: 0 (unknown) when the header is absent, its number otherwise. A header that is not a single whole
    number in ASCII digits (a sign, an underscore, a list, a repeat) raises ClientError, because the standard library may read another
    length from it and the check against the declared length would not run."""
    values = headers.get_all("Content-Length") or []
    if not values:
        return 0
    value = values[0].strip()
    if len(values) > 1 or not (value.isascii() and value.isdigit()):
        raise ClientError(f"the answer has a Content-Length that is not a whole number: {', '.join(values)[:60]!r}")
    return int(value)


class TooLarge(Exception):
    """An answer is longer than the limit for it."""


class Truncated(Exception):
    """An answer ended before the length it declared: args are the bytes read and the bytes declared."""


class TooSlow(Exception):
    """An answer did not finish within its time budget in seconds, which is args[0]."""


def audio_seconds(declared):
    """The time an audio file may take to arrive: a base plus the time at AUDIO_RATE bytes a second, up to AUDIO_SECONDS_MAX."""
    return min(AUDIO_SECONDS_MAX, AUDIO_SECONDS + (declared or MAX_AUDIO) / AUDIO_RATE)


def cover_seconds(declared):
    return COVER_SECONDS


def read_capped(response, limit, sink, budget, cancelled=lambda: False, progress=lambda size, declared: None, watchdog=None):
    """Writes the body of `response` to `sink` and returns its size. Raises TooLarge as soon as the body passes `limit`, TooSlow when it is
    not complete `budget(declared length)` seconds after the first read, and Truncated when it ends short of its declared length. A
    read takes what has arrived (read1), so a server that trickles bytes meets the deadline, and a cancel is seen, within one read. The
    deadline counts from the start of the `watchdog` (the time the headers took is spent) and replaces its header allowance."""
    declared = length(response.headers)
    if declared > limit:
        raise TooLarge(limit)
    deadline = (watchdog.start if watchdog else time.monotonic()) + budget(declared)
    if watchdog:
        watchdog.seconds = budget(declared)
    size = 0
    while not cancelled():
        chunk = response.read1(CHUNK)
        if not chunk:
            if declared and size != declared:
                raise Truncated(size, declared)
            break
        size += len(chunk)
        if size > limit:
            raise TooLarge(limit)
        sink.write(chunk)
        progress(size, declared)
        if time.monotonic() > deadline:
            raise TooSlow(round(budget(declared)))
    return size


def clean(url):
    """True when `url` holds no whitespace and no control character; nothing is stripped, an address with either is refused."""
    return not re.search(r"[\s\x00-\x1f\x7f]", url)


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to another https address, at most MAX_REDIRECTS times."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise ClientError("SoundCloud redirected to an address that is not https, which is not fetched")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Local(threading.local):
    watch = None


_local = Local()


class Watchdog:
    """Shuts the connections of one download when its time runs out or the job is cancelled. urlopen cannot be interrupted any other way: its
    timeout is per socket operation, so a server that sends its headers a byte at a time holds it for as long as it likes, through every
    redirect. The time runs from the creation of the watchdog, before the first connection, and `seconds` is replaced by the file's own
    budget once its headers name a length. `fired` is "slow" or "cancelled" once it has acted. Used as a context manager."""

    def __init__(self, seconds, cancelled):
        self.start, self.seconds, self.cancelled = time.monotonic(), seconds, cancelled
        self.fired, self.handles = "", []
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def watch(self, sock):
        """Keeps a duplicate of the connection's descriptor: a shutdown through it ends the connection even after TLS has taken the socket."""
        handle = socket.socket(fileno=os.dup(sock.fileno()))
        with self.lock:
            self.handles.append(handle)
            if self.fired:
                self.shut()

    def shut(self):
        for handle in self.handles:
            with contextlib.suppress(OSError):
                handle.shutdown(socket.SHUT_RDWR)

    def fire(self, reason):
        with self.lock:
            self.fired = reason
            self.shut()

    def run(self):
        while not self.stop.wait(0.1):
            if self.cancelled():
                return self.fire("cancelled")
            if time.monotonic() - self.start >= self.seconds:
                return self.fire("slow")

    def __enter__(self):
        _local.watch = self.watch
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        _local.watch = None
        for handle in self.handles:
            handle.close()


class Watched:
    """Hands each socket a connection makes to the watchdog of the download running in this thread."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        connect = self._create_connection

        def watched(*a, **k):
            sock = connect(*a, **k)
            if _local.watch:
                _local.watch(sock)
            return sock
        self._create_connection = watched


class WatchedHTTP(Watched, http.client.HTTPConnection):
    pass


class WatchedHTTPS(Watched, http.client.HTTPSConnection):
    pass


class WatchedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(WatchedHTTP, req)


class WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(WatchedHTTPS, req, context=self._context)


def open_url(req, timeout):
    return urllib.request.build_opener(HttpsOnly, WatchedHTTPHandler, WatchedHTTPSHandler).open(req, timeout=timeout)


def stub(raw):
    return not raw.get("title")


def clip(text, limit):
    """text cut to at most `limit` UTF-8 bytes, on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


class SoundCloud(Indexer):
    implementation = SLUG
    name = "SoundCloud"
    protocol = SLUG
    caps = {"search": True, "rss": True}
    API_VERSION = API_VERSION
    fields = [("delay", "Seconds between requests to SoundCloud", "2", {"address": False}),
              ("search_in", "What a search looks for", "both", {"choices": ["both", "sets", "tracks"]}),
              ("max_results", "Results per kind at most", "20", {"address": False}),
              ("follow", "Profile addresses the RSS feed follows (comma separated)", "", {"address": False}),
              ("rss_days", "How many days back RSS looks", "30", {"address": False})]

    def api(self):
        return SoundCloudAPI(session=Session(self.num("delay", 2)))

    def limit(self):
        return min(max(int(self.num("max_results", 20)), 1), 50)

    def terms(self, query):
        q = {"term": query} if isinstance(query, str) else dict(query or {})
        out = []
        for text in (q.get("term"), " ".join(x for x in (q.get("artist"), q.get("album")) if x), q.get("title"), *(q.get("titles") or ())):
            text = str(text or "").strip()
            if text and text not in out:
                out.append(text)
        return out[:MAX_TERMS]

    def track(self, raw, artist_url=None):
        """The release of one track, or None when SoundCloud serves only a preview of it."""
        if restricted(raw):
            return None
        why = refusal(raw)
        parsed = SoundCloudAPI._parse_track(raw, artist_url=artist_url)
        typed = _track_dict_to_release(parsed)
        artist, name = parsed["artist"], parsed["title"]
        q, label = quality_of(typed.codec, typed.bitrate)
        bitrate = int(typed.bitrate or 0)
        seconds = parsed["duration"] or 0
        return self.release(
            title=f"{artist} - {name}" + (f" [{label}]" if label else ""), guid=f"{SLUG}:track:{raw.get('id')}", url=parsed["url"],
            info_url=parsed["url"], size=seconds * bitrate * 125, published=parsed["release_date"] or "", kind="stream" if why else "download",
            refusal=why, sc_kind="track", soundcloud_id=str(raw.get("id") or ""), artist=artist, name=name, codec=typed.codec, bitrate=bitrate,
            quality=q, license=typed.license, genres=list(typed.work.content_genres), runtime=seconds, thumbnail=typed.image,
            downloadable=bool(raw.get("downloadable")), streamable=raw.get("streamable") is not False, country=parsed["country"])

    def album(self, raw):
        """The release of one set (a playlist, an album, an EP), or None when a track SoundCloud describes in it is a preview only."""
        user = raw.get("user") or {}
        artist_url = user.get("permalink_url") or ""
        tracks = raw.get("tracks") or []
        described = [t for t in tracks if not stub(t)]
        reasons = [refusal(t) for t in described]
        if any(restricted(t) for t in described):
            return None
        why = next((r for r in reasons if r), "")
        parsed = [SoundCloudAPI._parse_track(t, artist_url=artist_url) for t in described]
        typed = _set_dict_to_release({
            "title": raw.get("title") or "", "url": raw.get("permalink_url") or "", "artist": user.get("username") or "", "artist_url": artist_url,
            "image": raw.get("artwork_url") or user.get("avatar_url") or "", "playlist_id": raw.get("id"), "user_id": user.get("id"),
            "permalink": raw.get("permalink") or "", "content_genres": _build_genres(raw.get("genre"), raw.get("tag_list")), "license": _map_license(raw.get("license")),
            "release_date": date_of(raw) or None, "tracks": parsed})
        first = next(((p["codec"], p["bitrate"]) for p in parsed if p["codec"]), ("", ""))
        q, label = quality_of(*first)
        rate = int(first[1] or 0)
        seconds = int((raw.get("duration") or 0) / 1000)
        artist = user.get("username") or ""
        return self.release(
            title=f"{artist} - {raw.get('title') or ''}" + (f" [{label}]" if label else ""), guid=f"{SLUG}:set:{raw.get('id')}",
            url=raw.get("permalink_url") or "", info_url=raw.get("permalink_url") or "", size=seconds * rate * 125, published=date_of(raw),
            kind="stream" if why else "download", refusal=why, sc_kind="set", soundcloud_id=str(raw.get("id") or ""), artist=artist,
            name=raw.get("title") or "", codec=first[0], bitrate=rate, quality=q, license=typed.license, genres=list(typed.work.content_genres),
            runtime=seconds, thumbnail=typed.image, album=bool(raw.get("is_album")), set_type=raw.get("set_type") or "",
            track_count=raw.get("track_count") or len(tracks),
            tracks=[{"position": a.position, "title": a.work.title, "seconds": a.work.runtime} for a in typed.work.tracklist],
            downloadable=not why, streamable=True)

    def search(self, query):
        terms = self.terms(query)
        if not terms:
            return []
        kinds = {"sets": ("sets",), "tracks": ("tracks",)}.get(self.settings["search_in"], ("sets", "tracks"))
        with guard(IndexerError):
            api = self.api()
            for term in terms:
                found = []
                for kind in kinds:
                    data = api._call(f"{API}/search/{'playlists' if kind == 'sets' else 'tracks'}", q=term, limit=self.limit())
                    for raw in data.get("collection") or []:
                        r = self.album(raw) if kind == "sets" else self.track(raw)
                        if r:
                            found.append(r)
                if found:
                    return found
        return []

    def rss(self):
        profiles = words(self.settings["follow"])
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - self.num("rss_days", 30) * 86400))
        out, failed, read = [], [], 0
        with guard(IndexerError):
            api = self.api()
            for url in profiles:
                try:
                    user = api._call(f"{API}/resolve", url=url)
                    if user.get("kind") != "user":
                        raise ValueError("not a profile address")
                    page = api._call(f"{API}/users/{user['id']}/tracks", limit=self.limit(), linked_partitioning=1)
                except Exception as e:
                    failed.append(f"{url}: {type(e).__name__}: {e}")
                    continue
                read += 1
                for raw in page.get("collection") or []:
                    r = self.track(raw, artist_url=user.get("permalink_url") or url) if date_of(raw) >= cutoff else None
                    if r:
                        out.append(r)
        if failed and not read:
            raise IndexerError("soundcloud: " + "; ".join(failed))
        return sorted(out, key=lambda r: r["published"], reverse=True)

    def test(self):
        try:
            with guard(IndexerError):
                self.api()._call(f"{API}/search/tracks", q="test", limit=1)
        except IndexerError as e:
            return False, str(e)
        return True, "SoundCloud answered"


class SoundCloudClient(Direct):
    implementation = SLUG
    name = "SoundCloud"
    protocols = (SLUG,)
    category = ""
    API_VERSION = API_VERSION
    fields = [("download_dir", "Download folder", ""), ("delay", "Seconds between requests to SoundCloud", "2", {"address": False}),
              ("timeout", "Seconds without data before a download fails", "60", {"address": False})]

    def __init__(self, inst=None, state_dir=None):
        super().__init__(inst, state_dir)
        self.queue = JobQueue(state_dir, f"{SLUG}-{self.inst.get('id') or 0}.json", prefix=f"{SLUG}_")

    def add(self, release):
        url = release.get("url") or ""
        host = net.host_of(url)
        if not re.match(r"https://", url) or not clean(url) or not (host == "soundcloud.com" or host.endswith(".soundcloud.com")):
            raise ClientError("the release has no soundcloud.com address")
        if release.get("kind") != "download":
            raise ClientError(f"{release.get('title') or url} is not downloaded: {release.get('refusal') or 'it can only be streamed'}")
        self.folder()
        title = clip(safe(release.get("title") or "") or "soundcloud", 200).strip(" .")
        jid = self.queue.add(url, title, self.category or "*", release.get("size") or 0, own_folder=True)
        self.start()
        return jid

    def status(self):
        files = {j["id"]: j.get("files") or [] for j in self.queue.load()}
        return [dict(s, files=files.get(s["client_id"], [])) for s in super().status()]

    def run(self, job, ctl):
        written, target = [], os.path.join(self.folder(), job.get("folder") or job["title"] or "soundcloud")
        made = not os.path.isdir(target)
        try:
            with guard(ClientError):
                result = self.fetch(job, ctl, target, written)
        except ClientError as e:
            result = str(e)
        if result != "done" or ctl.cancelled:
            for path in written:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            if made:
                with contextlib.suppress(OSError):
                    os.rmdir(target)
        return "cancelled" if ctl.cancelled else result

    def tracks_of(self, api, resource):
        """The full track objects of a resolved track or set, in order; a set's tracks SoundCloud listed only by id are asked for."""
        if resource.get("kind") == "track":
            return [resource]
        if resource.get("kind") != "playlist":
            raise ClientError(f"{resource.get('permalink_url') or 'this address'} is not a track or a set")
        listed = resource.get("tracks") or []
        if any(t.get("id") is None for t in listed):
            raise ClientError("SoundCloud lists a track of this set without naming it")
        full = {t["id"]: t for t in listed if not stub(t)}
        missing = [t["id"] for t in listed if stub(t)]
        for i in range(0, len(missing), SET_CHUNK):
            chunk = missing[i:i + SET_CHUNK]
            for t in api._call(f"{API}/tracks", ids=",".join(str(x) for x in chunk)) or []:
                full[t["id"]] = t
        absent = [str(t["id"]) for t in listed if t["id"] not in full]
        if absent:
            raise ClientError(f"SoundCloud did not return {len(absent)} of the {len(listed)} tracks of this set (ids {', '.join(absent[:5])})")
        return [full[t["id"]] for t in listed]

    def fetch(self, job, ctl, target, written):
        delay = self.num("delay", 2)
        api = SoundCloudAPI(session=Session(delay))
        resource = api._call(f"{API}/resolve", url=job["ref"])
        tracks = self.tracks_of(api, resource)
        if not tracks:
            raise ClientError("SoundCloud lists no track here")
        refused = [r for r in map(refusal, tracks) if r]
        if refused:
            more = f" (and {len(refused) - 1} more of {len(tracks)} tracks)" if len(refused) > 1 else ""
            raise ClientError(f"refused: {refused[0]}{more}")
        os.makedirs(target, exist_ok=True)
        names = []
        for i, raw in enumerate(tracks):
            if ctl.cancelled:
                return "cancelled"
            if i:
                time.sleep(delay)
            chosen = progressive(raw)[0]
            url = api._call(chosen["url"]).get("url")
            if not url:
                raise ClientError(f"{raw.get('title')}: SoundCloud gave no stream address")
            codec = (chosen.get("format") or {}).get("mime_type") or ""
            who = (raw.get("user") or {}).get("username") or ""
            stem = " - ".join(x for x in ((f"{i + 1:02d}" if resource.get("kind") == "playlist" else ""), who, raw.get("title") or "") if x)
            name = self.file_name(stem, extension_of(codec))
            self.save(url, target, name, ctl, lambda pct, i=i: ctl.progress((100 * i + pct) // len(tracks)), written)
            if ctl.cancelled:
                return "cancelled"
            names.append(name)
        self.cover(resource, target, ctl, written)
        ctl.set(bytes=sum(os.path.getsize(p) for p in written if os.path.exists(p)), path=target, file=names[0], files=names)
        return "done"

    def cover(self, resource, target, ctl, written):
        """The artwork of the track or set as cover.<ext> next to the audio, when the library's data names one; a failure leaves no file."""
        url = resource.get("artwork_url") or (resource.get("user") or {}).get("avatar_url") or ""
        if not url:
            return
        name = "cover" + (os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".jpg")
        try:
            self.save(url, target, name, ctl, lambda pct: None, written, audio=False)
        except Exception:  # a cover never fails the job
            for path in (os.path.join(target, name), os.path.join(target, name + ".part")):
                if path in written:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
                    written.remove(path)

    @staticmethod
    def file_name(stem, ext):
        """<stem>.<ext> with the stem cut so that the name and its ".part" suffix are within 255 bytes."""
        stem = safe(stem) or "track"
        room = MAX_NAME - len(f".{ext}.part".encode())
        return clip(stem, room).strip(" .") + "." + ext

    def save(self, url, target, name, ctl, progress, written, audio=True):
        """One file from `url` into `target` as `name`, written as name.part and renamed when whole; a file that ends short, an HTML page or a
        playlist instead of audio, an answer that stops for `timeout` seconds, or one that is not whole within its time budget (headers and
        redirects included) fails; a cancel ends the wait at once. Nothing is left behind by a failure."""
        if not url.startswith("https://") or not clean(url):
            raise ClientError("SoundCloud gave a stream address that is not https, which is not fetched")
        part, final = os.path.join(target, name + ".part"), os.path.join(target, name)
        if os.path.lexists(final):
            raise ClientError(f"{name} already exists and is not overwritten")
        req = urllib.request.Request(url, headers={"User-Agent": net.USER_AGENT})
        limit, budget = (MAX_AUDIO, audio_seconds) if audio else (MAX_COVER, cover_seconds)
        dog = Watchdog(min(budget(0), PAGE_SECONDS), lambda: ctl.cancelled)
        try:
            with dog:
                try:
                    with open_url(req, self.num("timeout", 60)) as r:
                        kind = (r.headers.get("Content-Type") or "").lower()
                        if audio and (kind.startswith("text/") or "mpegurl" in kind or "json" in kind):
                            raise ClientError(f"{net.host_of(url)} sent {kind}, not audio")
                        if not audio and not kind.startswith("image/"):
                            raise ClientError(f"{net.host_of(url)} sent {kind or 'no content type'}, not an image")
                        try:
                            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
                        except FileExistsError:
                            raise ClientError(f"{name}.part already exists and is not overwritten") from None
                        written.append(part)
                        with os.fdopen(fd, "wb") as f:
                            got = read_capped(r, limit, f, budget, lambda: ctl.cancelled,
                                              lambda size, total: progress(100 * size // total) if total else None, dog)
                except Exception:
                    if dog.fired == "slow":
                        raise TooSlow(round(dog.seconds)) from None
                    if dog.fired == "cancelled":
                        return
                    raise
                if dog.fired == "slow":
                    raise TooSlow(round(dog.seconds))
            if ctl.cancelled:
                return
            if not got:
                raise ClientError(f"{net.host_of(url)} sent an empty answer, not a file")
            os.replace(part, final)
            written[written.index(part)] = final
        except TooLarge as e:
            raise ClientError(f"{net.host_of(url)} sent more than {e.args[0]} bytes; refused") from None
        except Truncated as e:
            raise ClientError(f"incomplete download: {e.args[0]} of {e.args[1]} bytes") from None
        except TooSlow as e:
            raise ClientError(f"{net.host_of(url)} did not finish within {e.args[0]} seconds") from None
        except urllib.error.HTTPError as e:
            e.close()
            raise ClientError(f"HTTP {e.code} from {net.host_of(url)}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise ClientError(f"{net.host_of(url)} did not answer: {getattr(e, 'reason', e)}") from None


IMPLEMENTATION = SoundCloud
