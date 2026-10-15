# nuvem-de-som

nuvem-de-som is a SoundCloud client for search, streaming, and download. It has
three independent backends and one orchestrator that tries them in order. It
also has a terminal app, `nds`. All methods return
[`mediavocab`](https://github.com/OpenVoiceOS/mediavocab) `Release` and
`Entity` objects, so track and artist metadata come back typed and structured.

## Install

```bash
pip install nuvem_de_som           # search + stream (no yt-dlp)
pip install "nuvem_de_som[yt-dlp]" # adds yt-dlp for download & stream fallback
pip install "nuvem_de_som[cli]"    # adds the nds terminal app
pip install "nuvem_de_som[yt-dlp,cli]"  # everything
pip install "nuvem_de_som[stealth]"     # adds curl_cffi for browser-impersonating HTTP
pip install "nuvem_de_som[arr]"         # registers an indexer and download client for arrkit apps
```

### Stealth transport (`curl_cffi`)

SoundCloud's API and HTML endpoints increasingly block requests based on
TLS (JA3) and HTTP/2 fingerprints. To reduce the risk of a block, install the
`[stealth]` extra and turn it on with an environment variable:

```bash
pip install "nuvem_de_som[stealth]"
NUVEM_TRANSPORT=curl_cffi python -m yourapp
```

When `NUVEM_TRANSPORT=curl_cffi` is set and `curl_cffi` is importable,
`SoundCloudAPI`, `SoundCloudHTML`, and the `SoundCloud` orchestrator use a
`curl_cffi.requests.Session(impersonate="chrome")` for every request.
Otherwise they fall back to the stdlib `requests.Session`.

You can also inject a compatible session directly:

```python
from curl_cffi import requests as cffi_requests
from nuvem_de_som import SoundCloudAPI

sc = SoundCloudAPI(session=cffi_requests.Session(impersonate="chrome120"))
```

Note: `SoundCloudYTDLP` uses yt-dlp for its HTTP traffic and does not use an
injected session. yt-dlp manages its own networking.

## Python API quick start

```python
from nuvem_de_som import SoundCloud

sc = SoundCloud()   # orchestrator: API → yt-dlp → HTML

# Search tracks → mediavocab.Release objects
for release in sc.search_tracks("nuclear chill", limit=5):
    artist = release.work.credits[0].entity.name if release.work.credits else ""
    print(release.work.title, artist, release.work.runtime)  # runtime in seconds

# Browse an artist or set page
for release in sc.get_tracks("https://soundcloud.com/acidkid", limit=50):
    print(release.work.title, release.uri)

# Search artists → mediavocab.Entity objects
for entity in sc.search_people("acidkid", limit=5):
    print(entity.name, entity.extra.get("artist_url"))

# Resolve a direct stream URL (no yt-dlp)
url = sc.resolve_stream("https://soundcloud.com/acidkid/piratech-nuclear-chill")
url = sc.resolve_stream("...", prefer="hls")   # or "progressive" (default)

# Download: SoundCloudAPI uses pure requests. SoundCloudYTDLP uses yt-dlp
path = sc.download_track("https://soundcloud.com/acidkid/piratech-nuclear-chill",
                          output_dir="~/Music")
sc.download_playlist("https://soundcloud.com/acidkid", output_dir="~/Music")
```

### Return types at a glance

**`Release`** comes from `search_tracks`, `get_tracks`, `resolve_track`, and `search_sets`:

```python
release.uri                          # SoundCloud permalink
release.image                        # artwork URL
release.work.title                   # track title
release.work.runtime                 # duration in seconds (float or None)
release.work.credits[0].entity.name  # artist display name (if available)
release.work.extra.get("artist_url") # artist profile URL
release.work.external_ids            # {"soundcloud_track_id": "...", "soundcloud_user_id": "..."}
release.work.aka                     # [permalink slug]
release.work.content_genres          # ["electronic", "ambient", ...]   from genre + tag_list
release.work.production_country      # ISO 3166 alpha-2 of the uploader, when known
release.work.tracklist               # [Appearance, ...] for sets/playlists
release.codec                        # e.g. "audio/mpeg"
release.bitrate                      # "128" (sq) / "256" (hq) when known
release.audio_channels               # "stereo"
release.license                      # SPDX id ("CC-BY-NC-4.0", "CC0-1.0") or raw
release.release_date                 # ISO date validated by mediavocab
```

For SoundCloud sets and playlists, `Work.tracklist` holds typed
`mediavocab.Appearance` entries, each with the per-track `Work`, positioned
1..N. See `examples/set_tracklist.py`.

**`Entity`** comes from `search_people`, `resolve_user`, `get_followers`, and `get_following`:

```python
entity.name                              # display name
entity.extra.get("artist_url")           # profile URL
entity.extra.get("image")               # avatar URL
entity.external_ids                      # {"soundcloud_user_id": "..."}
entity.extra.get("followers_count")      # string int, e.g. "12345"
entity.extra.get("followings_count")     # string int
entity.extra.get("track_count")          # string int
entity.extra.get("verified")             # "1" when verified, absent otherwise
```

## Frontier crawling

`SoundCloudAPI.crawl()` finds artists with a breadth-first search over the
social graph. Seeds are profile URLs or keyword queries. Each visited
profile's followers and followings go into the queue, up to `social_depth`:

```python
from nuvem_de_som import SoundCloudAPI

sc = SoundCloudAPI()
seen = set()
for entity in sc.crawl(["https://soundcloud.com/noisia", "black metal"],
                        social_depth=20, max_artists=100, seen=seen):
    print(entity.name, entity.extra.get("followers_count", "?"),
          "verified" if entity.extra.get("verified") else "")
```

Pass `seen` across calls to resume without visiting the same profile twice.

## Backends

| Backend | Search | Stream | Download | Extra dep |
|---|---|---|---|---|
| `SoundCloudAPI` | full metadata | progressive or HLS | pure requests | n/a |
| `SoundCloudHTML` | title+URL only | raises `NotImplementedError` | n/a | n/a |
| `SoundCloudYTDLP` | full metadata | yt-dlp | yt-dlp | `yt-dlp` |
| `SoundCloud` | API → yt-dlp → HTML | API first, yt-dlp fallback | API first, yt-dlp fallback | optional `yt-dlp` |

Use a concrete backend class when you need one specific backend. Use
`SoundCloud` when you want it to fall back automatically.

```python
from nuvem_de_som import SoundCloudAPI, SoundCloudHTML, SoundCloudYTDLP, SoundCloud

sc = SoundCloudAPI()     # recommended: full metadata, no yt-dlp
sc = SoundCloudHTML()    # no extra deps, title+URL from search pages
sc = SoundCloudYTDLP()   # yt-dlp backed, slowest but most resilient
sc = SoundCloud()        # tries all three in order
```

## Terminal app: `nds`

```bash
nds search "nuclear chill"          # interactive: pick a track, then play or download
nds search "acidkid" --people
nds browse https://soundcloud.com/acidkid   # browse artist page interactively
nds play   https://soundcloud.com/acidkid/piratech-nuclear-chill
nds download https://soundcloud.com/acidkid/piratech-nuclear-chill -o ~/Music
nds download https://soundcloud.com/acidkid --playlist -o ~/Music

nds --backend api search "chill"    # force a specific backend (api/html/ytdlp/auto)
NDS_PLAYER=vlc nds play <url>       # override player; auto-detects mpv → vlc → ffplay
```

Playback uses the `--player` flag or the `NDS_PLAYER` environment variable,
or auto-detects a player in this order: **mpv** → vlc → ffplay → mplayer →
afplay → cvlc. Any binary name or full path works, on Termux, Windows, and
macOS.

## Docs

- [Getting started](docs/getting-started.md)
- [Backends reference](docs/backends.md)
- [Streams and transcodings](docs/streams.md)
- [mediavocab converters](docs/converters.md)
- [Transport](docs/transport.md)
- [CLI reference](docs/cli.md)
- [API extras: followers, following, reposts, crawl](docs/api.md)
- [Use from an arrkit app](docs/arrkit.md)

## Related projects

- [`mediavocab`](https://github.com/OpenVoiceOS/mediavocab): the typed
  metadata models (`Release`, `Entity`) that every nuvem-de-som method returns.
- [`soundcloud-ma-provider`](https://github.com/TigreGotico/soundcloud-ma-provider):
  a Music Assistant provider built on nuvem-de-som.

## Examples

See [`examples/`](examples/) for numbered scripts, from basic to advanced.

## License

Apache 2.0
