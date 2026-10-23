# AGENTS.md — nuvem_de_som

SoundCloud search/stream/download client with three interchangeable backends and one orchestrator; all track methods return `mediavocab.Release`, people methods return `mediavocab.Entity`.

## Setup

```bash
pip install -e .            # core: bs4, requests, mediavocab
pip install -e .[test]      # + pytest, vcrpy, pytest-vcr, click
pip install -e .[yt-dlp]    # SoundCloudYTDLP backend + download/stream fallback
pip install -e .[cli]       # nds terminal app (click)
pip install -e .[stealth]   # curl_cffi browser impersonation transport
pip install --find-links <arrkit wheel folder> -e .[arr]   # arrkit (not on PyPI), for the indexer and download client plugin
```

## Test

```bash
pytest                                  # runs offline; excludes -m integration via addopts
pytest -m integration                   # opt-in live-network tests
pytest --vcr-record=all test/test_*_vcr.py   # re-record VCR cassettes
```

Default `addopts = -m 'not integration'`. HTTP-backed tests replay VCR cassettes under `test/cassettes/<module>/<test>.yaml` (record_mode `none`); cassette drift is caught by the nightly live CI run.

## Lint/Typecheck

Ruff configured (CI `lint.yml` runs `OpenVoiceOS/gh-automations` lint with `ruff: true`). No type checker wired in CI, but source is fully type-annotated (`from __future__ import annotations`). Run `ruff check .` locally.

## Layout

- `nuvem_de_som/__init__.py` — the whole library (~1.4k lines): module-level converter helpers (`_track_dict_to_release`, `_set_dict_to_release`, `_user_dict_to_entity`, `_parse_transcodings`, `_build_genres`, `_map_license`, client-id fetch/cache), the `SoundCloudBase` ABC, and four backends.
- `SoundCloudAPI` — SoundCloud internal v2 API via requests; full metadata, progressive/HLS streams, pure-requests downloads. Recommended.
- `SoundCloudHTML` — HTML scraper; title+URL search only, `resolve_stream` raises `NotImplementedError`. No extra deps.
- `SoundCloudYTDLP` — yt-dlp backed; best stream resolution, owns its networking (ignores injected `session=`).
- `SoundCloud` — orchestrator; tries API → yt-dlp → HTML, falls back on error; download methods delegate to the yt-dlp backend.
- `nuvem_de_som/transport.py` — `default_session()`; returns `curl_cffi` impersonating Chrome when `NUVEM_TRANSPORT=curl_cffi` and `[stealth]` installed, else `requests.Session()`.
- `nuvem_de_som/arr.py` — the arrkit indexer and download client (entry points `arrkit.indexers` / `arrkit.clients`, name `soundcloud`); imports arrkit, which the rest of the library never does. All its requests go through `arrkit.net` and a plain session handed to `SoundCloudAPI`; see `docs/arrkit.md`.
- `nuvem_de_som/cli.py` — `nds` entry point (search/browse/play/download), `--backend api|html|ytdlp|auto`, `NDS_PLAYER` override.
- `test/` — unit + VCR + CLI + license tests; `conftest.py` holds the VCR config.
- `tests_arrkit/` — the arrkit plugin tests. arrkit is not on PyPI, so they are not in the `test` extra and not in CI's `test` path, and the root `conftest.py` leaves them out of collection below Python 3.12 (arrkit and `tomllib`): install arrkit from its wheel (`uv pip install --find-links <folder with the arrkit wheel> -e ".[arr]"`) and run `pytest tests_arrkit`.
- `examples/` — numbered zero-to-hero scripts. `docs/` — per-topic reference.

## Conventions

- Branches: `dev` (work) / `master` (stable). Never `main`.
- Never edit `nuvem_de_som/version.py`; gh-automations bumps semver from conventional-commit prefixes (`feat:` / `fix:` / `feat!:`).
- New repos private by default.
- Commit identity: JarbasAi <jarbasai@mailfence.com>.
- Reference `OpenVoiceOS/gh-automations` reusable workflows at `@dev`.
- No Neon / `neon-*` references.
- No meta-commentary in code, docs, commits, or PRs (no history, dates, "before times").
- CI is provided by OpenVoiceOS/gh-automations.

## Gotchas

- `_get_client_id` scrapes and caches a SoundCloud client_id (thread-locked); `_invalidate_client_id` forces a refresh when the API 401s.
- `SoundCloudHTML.resolve_stream` deliberately raises `NotImplementedError` — HTML backend is search/metadata only.
- `_map_license` maps SoundCloud license strings to SPDX; `all-rights-reserved` has no SPDX and is passed through raw to stay distinct from "unknown".
- `_build_genres` resolves tokens against `mediavocab.taxonomy.genre.GENRE_*` constants, keeping unrecognised tags as free strings; SC `tag_list` is space-separated with quoted multi-word tags.
- `pyproject.toml` `[project.urls]` Homepage still points at `OpenJarbas/nuvem_de_som` while the repo lives at `TigreGotico/nuvem_de_som`.
- A duplicate lowercase `readme.md` is tracked alongside `README.md`.
