# Use from an arrkit app

nuvem_de_som registers itself with apps built on arrkit (Musicarr and the other *arr-style apps) as an indexer and a download client, both named `soundcloud`. Install it with its `arr` extra into the app's environment and restart the app:

    uv pip install "nuvem_de_som[arr]"

The app then lists `soundcloud` among its plugins, disabled. Add `soundcloud` to the app's enabled plugins, then create one indexer and one download client of implementation `soundcloud` in its settings. Nothing is called on a disabled plugin. Neither class is marked adult.

## Indexer

A search looks for sets (SoundCloud playlists, which is how albums and EPs are published) and tracks. The search text is the query's `term`, else "artist album", else the item's `title`; the other names in `titles` are tried in turn, three texts at most, until one finds something. A release is one set or one track.

| Setting | Meaning |
|---|---|
| `delay` | seconds between two requests to one host (default 2) |
| `search_in` | `both` (default), `sets` or `tracks` |
| `max_results` | results per kind at most (default 20, never more than 50) |
| `follow` | profile addresses the RSS feed follows, comma separated (empty by default) |
| `rss_days` | how far back RSS looks (default 30) |

A release has arrkit's keys (`title`, `guid`, `url`, `info_url`, `size`, `published`, `protocol` `soundcloud`, `kind`) and the library's typed fields: `codec` (the mime type), `bitrate` (kbps), `quality` (an audio quality name such as `MP3-128` or `AAC-256`, also written in the title so a profile reads it from there), `license` (an SPDX id; `all-rights-reserved` has none and stays as written), `genres` (the library's `content_genres`), `runtime` (seconds), `country`, `thumbnail`, `sc_kind` (`track` or `set`) and `soundcloud_id`. A set adds `album`, `set_type`, `track_count` and `tracks` (position, title, seconds). `size` is an estimate: the length times the bitrate. Codec and bitrate describe the best direct stream SoundCloud offers, as the library reads them from the track's transcodings; a set shows those of its first track that SoundCloud described in the answer.

`kind` is `download` when the client may fetch the release and `stream` when it may only be played; `downloadable` is the uploader's own flag, `streamable` SoundCloud's, and `refusal` says why a stream release is not a download. A track SoundCloud serves as a preview only (Go+, a snipped stream) is not listed at all, nor is a set that holds one among the tracks the answer describes. SoundCloud describes only the first tracks of a long set in a search answer, so a set is judged on those; the client judges every track when it grabs.

RSS lists the tracks the profiles in `follow` uploaded within `rss_days`, newest first. A profile that cannot be read is skipped, and the feed fails only when no profile could be read; a readable profile with nothing new is not a failure. The library offers no tag or genre feed, so tags cannot be followed.

## Download client

The client takes releases of protocol `soundcloud`. A grab is queued in the app's state folder and worked one at a time. The job asks SoundCloud again for the track or set, so the permission is judged on current data, and writes into `<download_dir>/<title>-<job>/`:

| Setting | Meaning |
|---|---|
| `download_dir` | where jobs are written |
| `delay` | seconds between two requests to SoundCloud, and between two files of a set (default 2) |
| `timeout` | seconds without data before a download fails (default 60) |

A track is downloaded when all of these hold:

- the uploader enabled downloads on it, or its licence is CC0 or a Creative Commons Attribution licence (CC-BY with any of the NC, ND and SA terms);
- SoundCloud serves the full track: no Go+ restriction, no preview, no snipped stream;
- a direct (progressive) stream exists. HLS-only tracks are refused, because a playlist file is not audio.

The file is the stream copy the library resolves (the best progressive transcoding, whose codec and bitrate the release shows), not the uploader's original file, which the library cannot fetch. Refusals fail the job with the reason, for example `Slow Preview: SoundCloud Go+ restricted, only a preview or nothing is available` or `Slow Snow: the uploader has not enabled downloads and the licence (all-rights-reserved) does not allow copies`. Nothing circumvents a paywall, a preview limit or a bot check.

A set is all or nothing: if any track is refused, the job fails before a byte is fetched and says which track and how many more. Tracks that a set lists only by id are asked for by id; a set with an entry that has no id, or a track SoundCloud does not return, fails the job. Files are named `NN - Artist - Title.ext` in a set and `Artist - Title.ext` for a track, `.mp3`, `.m4a`, `.opus` or `.ogg` by the stream's type, cut so that the name and its `.part` suffix fit in 255 bytes. The set's or track's artwork is saved as `cover.<ext>` when SoundCloud names one; a failed cover fetch does not fail the job. The library offers no tag writer, so tags are not embedded.

A file is written as `<name>.part` and renamed when whole. A download that ends short of its announced length, that returns text or a playlist instead of audio, that stops sending for `timeout` seconds, or that is cancelled removes every file the job wrote and the job folder. `status()` adds `files` (the names written) to arrkit's fields, and `path` is the job folder.

## Requests

Every request to SoundCloud's API goes through `arrkit.net`: spaced by `delay`, retried as arrkit retries, with arrkit's User-Agent, and without browser impersonation or any bot-check handling. The library's own session factory (unblock_requests, curl-cffi) is not used; `SoundCloudAPI` is given a plain session instead, and a site that refuses the client is reported as an error. The library's client_id mechanism is unchanged: the id is read from the site's script bundles once per process and refreshed once when the API answers 401 or 403. Files are fetched from the stream address SoundCloud returns with the same User-Agent.
