"""Central configuration, loaded from .env (see .env.example)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # secrets / integrations
    jimaku_token: str = ""
    mal_client_id: str = ""
    mal_client_secret: str = ""
    mal_redirect_uri: str = "http://localhost:8000/api/mal/callback"

    # Anthropic (optional) — Claude Haiku for smart release matching. Read from
    # the ANTHROPIC_API_KEY env var / .env. Blank disables LLM matching (the
    # heuristic ranking still works).
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    # English secondary subtitles — a native-language REFERENCE/translation track
    # shown alongside the Japanese study track in Migaku. This is NOT a second study
    # language: Japanese stays the only tokenized / comprehension / known-words
    # source; English is never tokenized.
    #  * english_subs_enabled: master switch for the whole English pipeline.
    #  * english_source: where the English text comes from —
    #       "human" (default): a real human subtitle — the embedded English softsub
    #          extracted from the downloaded release.
    #       "llm": machine-translate the Japanese lines with Claude (translation_model).
    #  * translation_model: the Claude model used when english_source == "llm"
    #     (overridable here if the id differs).
    english_subs_enabled: bool = True
    english_source: str = "human"  # "human" (embedded softsub) | "llm" (Claude MT)
    translation_model: str = "claude-sonnet-4-6"

    # Connector topology (notes/REMOTE_PLAYBACK_DESIGN.md). Playback no longer runs on
    # the server: a user-side Connector holds a CDP link to *your* Chrome+Migaku
    # and dials out to this server over a WebSocket. The server only streams media
    # + relays play commands.
    #
    # mimi_lab_token: the single shared secret the Connector presents (WS auth +
    #   upload auth); per-play media URLs carry a short-lived signed token derived
    #   from it. Blank = loopback/dev mode (auth disabled — set one before exposing
    #   the server beyond localhost).
    mimi_lab_token: str = ""
    # server_public_url: the base URL the Connector (and Migaku, inside it) can
    #   reach this server at — e.g. https://lab.example.net. Media URLs are minted
    #   against it. Blank = derive from the incoming
    #   request (fine for same-LAN; required for a remote Connector + HTTPS).
    server_public_url: str = ""
    # how long a minted media token stays valid (seconds). Long enough to watch an
    # episode + let the Connector pre-fetch the next one; short enough to be cheap.
    media_token_ttl: int = 21600  # 6h
    # Migaku Early-Access extension id (informational; the Connector uses its own).
    migaku_ext_id: str = "dmeppfcidcpcocleneopiblmpnbokhep"

    # paths
    mimi_lab_db: Path = ROOT / "data" / "mimi_lab.db"
    library_dir: Path = ROOT / "Library"
    inbox_dir: Path = ROOT / "data" / "inbox"
    clips_dir: Path = ROOT / "data" / "clips"

    # transmission (headless torrent daemon — JSON-RPC)
    transmission_host: str = "127.0.0.1"
    transmission_port: int = 9091
    transmission_path: str = "/transmission/rpc"

    # seeding policy: stop seeding a torrent once its upload/download ratio
    # reaches this value. 0.0 (default) = stop seeding the instant the download
    # completes. A negative value means "seed unlimited" (impose no limit).
    # Applied per-torrent at add time so it covers everything we download.
    max_seed_ratio: float = 0.0

    # pipeline automation
    prune_originals: bool = True

    # job queue: number of concurrent worker threads. >1 stops a long transcode
    # from head-of-line-blocking interactive work (comprehension, subtitle fetch);
    # priority lanes still run interactive jobs first. SQLite (WAL) + short claim
    # transactions handle a small pool comfortably.
    job_workers: int = 3
    # retention: delete 'done' jobs and read events older than this many days
    # (0 = keep forever). Keeps the jobs/events tables from growing unbounded.
    job_retention_days: int = 14
    event_retention_days: int = 45

    # media transcode (HEVC / 10-bit -> browser-playable 8-bit h264).
    # Plain h264 sources are remuxed losslessly and ignore these knobs.
    #  * transcode_encoder: "libx264" (software, default) or "h264_videotoolbox".
    #    libx264 generally offers predictable quality across platforms, while a
    #    supported hardware encoder can reduce CPU use. Benchmark both on the
    #    deployment host before changing the default.
    #  * transcode_crf: libx264 constant-quality (lower = better/larger; 18-20 is
    #    visually transparent for anime). Content-adaptive -> no banding on busy
    #    scenes. Only used by the libx264 encoder.
    #  * transcode_preset: libx264 speed/efficiency preset (ultrafast..veryslow).
    #  * transcode_video_bitrate_1080p: only used by the h264_videotoolbox fallback
    #    (its -q:v constant-quality is unreliable here, so it targets a fixed
    #    resolution-scaled bitrate instead). ~10M is near-transparent at 1080p.
    #  * audio_bitrate: only used when the source audio is NOT already a browser-
    #    playable AAC-LC track (FLAC/Opus/AC3/HE-AAC). AAC-LC is copied losslessly.
    transcode_encoder: str = "libx264"
    transcode_crf: int = 19
    transcode_preset: str = "faster"
    transcode_video_bitrate_1080p: str = "10M"
    audio_bitrate: str = "192k"

    # server
    host: str = "127.0.0.1"
    port: int = 8000

    # --- network exposure / browser-attack hardening -------------------------
    # A Host-header allowlist defeats DNS-rebinding (a malicious web page that
    # rebinds its domain to 127.0.0.1 to reach this API from the browser); an
    # Origin allowlist + tight CORS defeat cross-site reads/CSRF. Both are
    # overridable here for non-standard deployments (comma-separated).
    #  * allowed_hosts: extra Host values on top of localhost / 127.0.0.1 /
    #    configured allowed_hosts / the server_public_url host.
    #  * allowed_origins: extra browser Origins on top of the localhost dev
    #    ports + the server_public_url origin.
    allowed_hosts: str = ""
    allowed_origins: str = ""

    def _public_host(self) -> Optional[str]:
        if not self.server_public_url:
            return None
        from urllib.parse import urlparse
        return urlparse(self.server_public_url).hostname

    def trusted_hosts(self) -> list[str]:
        """Host-header allowlist for TrustedHostMiddleware (anti-DNS-rebinding)."""
        hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        ph = self._public_host()
        if ph:
            hosts.add(ph)
        hosts.update(h.strip() for h in self.allowed_hosts.split(",") if h.strip())
        return sorted(hosts)

    def cors_origins(self) -> list[str]:
        """Explicit CORS origin allowlist (never '*')."""
        origins = {
            "http://localhost:5173", "http://127.0.0.1:5173",   # vite dev server
            "http://localhost:8000", "http://127.0.0.1:8000",   # served SPA
        }
        if self.server_public_url:
            origins.add(self.server_public_url.rstrip("/"))
        origins.update(o.strip() for o in self.allowed_origins.split(",") if o.strip())
        return sorted(origins)

    def ensure_dirs(self) -> None:
        for p in (
            self.mimi_lab_db.parent,
            self.library_dir,
            self.inbox_dir,
            self.clips_dir,
        ):
            Path(p).mkdir(parents=True, exist_ok=True)


settings = Settings()
