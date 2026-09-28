"""
Music download engine for Music Downloader Web App.

Handles:
- Parsing Spotify / YouTube / YouTube Music links (songs, albums, playlists, artists)
- Download & convert to MP3 (metadata + cover art automatically via spotdl)
- Per-song progress for display in the web UI
- Skip duplicates (existing files are not re-downloaded)
- Automatic folder organization:
    downloads/<Artist>/<Album>/<Title>.mp3
- Auto-download FFmpeg if not already available

This file is imported by app.py — do not run it directly.
"""

import logging
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("music-downloader")

# Silence third-party library logs to keep the server console clean
logging.getLogger("spotdl").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)
logging.getLogger("ytmusicapi").setLevel(logging.WARNING)


# ============================================================
# Spotdl matcher patches (bug: feat. songs vs single-artist results)
# ============================================================
# Spotdl v4.5.x bug: in calc_main_artist_match, when a song has multiple
# artists (e.g. "feat. Feby Putri") but the YouTube Music result only lists
# the primary artist, the function ONLY checks secondary artists â€” the main
# artist is never compared -> score 0 -> all correct candidates are discarded
# (LookupError: No results found), even though the song exists.
# Patch: compare the main artist first, then add the secondary artist portion.

def _patched_calc_main_artist_match(song, result) -> float:
    """Patched calc_main_artist_match that also compares the main artist."""
    from spotdl.utils.matching import based_sort, sort_string

    if not result.artists:
        return 0.0

    song_artists = list(map(_slugify, song.artists))
    result_artists = list(map(_slugify, result.artists))
    sorted_song_artists, sorted_result_artists = _based_sort(
        song_artists, result_artists
    )

    slug_song_main = _slugify(song.artists[0])
    slug_result_main = sorted_result_artists[0]

    if len(song.artists) > 1 and len(result.artists) == 1:
        # --- BUG CASE: compare main artist first ---
        main_match = _ratio(slug_song_main, slug_result_main)

        # Secondary artists may be "merged" into the result artist name
        # (e.g. result "Biru Baru" includes feat. in the title, not in artists)
        extra = 0.0
        for artist in song_artists[1:]:
            artist_sorted = _sort_string(_slugify(artist).split("-"), "-")
            res_main_sorted = _sort_string(slug_result_main.split("-"), "-")
            if artist_sorted in res_main_sorted:
                extra += 100 / len(song.artists)

        return max(main_match, extra)

    # Normal case: follow spotdl's default behavior
    main_match = _ratio(slug_song_main, slug_result_main)
    if main_match < 50 and len(song_artists) > 1:
        for song_artist, result_artist in product(
            song_artists[:2], sorted_result_artists[:2]
        ):
            pair = _ratio(song_artist, result_artist)
            if pair > main_match:
                main_match = pair
    return main_match


def _patched_calc_artists_match(song, result) -> float:
    """
    Patched calc_artists_match that recognizes secondary artists listed
    in the result TITLE (e.g. "Song [Live] (feat. Feby Putri)") even when
    they are not present in the YouTube Music result's artist field.
    """
    from itertools import zip_longest

    from spotdl.utils.matching import based_sort

    if len(song.artists) == 1 or not result.artists:
        return 0.0

    artist1_list, artist2_list = based_sort(
        list(map(_slugify, song.artists)), list(map(_slugify, result.artists))
    )
    # Remove the main artist from both lists (same behavior as spotdl)
    artist1_list, artist2_list = artist1_list[1:], artist2_list[1:]

    if not artist1_list:
        return 0.0

    result_name_flat = _slugify(result.name).replace("-", "")

    artists_match = 0.0
    for artist1, artist2 in zip_longest(artist1_list, artist2_list):
        if artist1 is None:
            continue
        if artist2 is None:
            # PATCH: song's secondary artist is not in the result's artist field,
            # but is often mentioned in the result title -> "(feat. X)"
            if artist1.replace("-", "") in result_name_flat:
                artists_match += 100
            continue
        artists_match += _ratio(artist1, artist2)

    return artists_match / len(artist1_list)


def _apply_matcher_patch() -> None:
    """
    Install patches for spotdl (idempotent, safe if spotdl changes):
    1. Artist matcher: "feat. X" songs vs single-artist YTM results never
       compare the main artist -> score 0 -> all candidates discarded.
    2. YouTubeMusic client: default language="de" causes certain songs
       (e.g. Indonesian songs) to return 0 results.
    3. embed_metadata with retry: new .mp3 files on Windows are sometimes
       briefly locked by antivirus when ID3 tags are written.
    """
    try:
        import spotdl.utils.matching as matching

        if not getattr(matching, "_main_artist_patched", False):
            matching.calc_main_artist_match = _patched_calc_main_artist_match
            matching.calc_artists_match = _patched_calc_artists_match
            matching._main_artist_patched = True

        # YTMusic client with English language (default "de" is problematic)
        try:
            from ytmusicapi import YTMusic

            def _create_client_en():
                return YTMusic(language="en")

            import spotdl.providers.audio.ytmusic as ytm_provider

            if not getattr(ytm_provider.YouTubeMusic, "_client_patched", False):
                ytm_provider.YouTubeMusic._create_client = staticmethod(
                    _create_client_en
                )
                ytm_provider.YouTubeMusic._client_patched = True
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to patch YTMusic client: %s", exc
            )

        # Retry embed metadata (file briefly locked by Windows antivirus)
        try:
            import spotdl.download.downloader as spotdl_downloader

            if not getattr(spotdl_downloader, "_embed_retry_patched", False):
                spotdl_downloader.embed_metadata = _patched_embed_metadata
                spotdl_downloader._embed_retry_patched = True
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to patch embed metadata retry: %s", exc
            )

        logging.getLogger(__name__).info(
            "Spotdl patches installed (feat. artist matcher + YTM client en "
            "+ embed metadata retry)"
        )
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "Failed to install spotdl patches: %s", exc
        )


def _slugify(string: str) -> str:
    """Wrapper for spotdl's slugify (called lazily to keep imports fast)."""
    from spotdl.utils.formatter import slugify

    return slugify(string)


def _ratio(string1: str, string2: str) -> float:
    """Wrapper for spotdl's ratio."""
    from spotdl.utils.formatter import ratio

    return ratio(string1, string2)


# Module-level imports for patching (used inside _patched_calc_main_artist_match)
def _based_sort(strings, based_on):
    from spotdl.utils.matching import based_sort

    return based_sort(strings, based_on)


def _sort_string(strings, join_str="-"):
    from spotdl.utils.matching import sort_string

    return sort_string(strings, join_str)

# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"

# Default spotdl public client â€” only for reading public Spotify metadata
DEFAULT_CLIENT_ID = "f8a606e5583643beaa27ce62c48e3fc1"
DEFAULT_CLIENT_SECRET = "f6f4c8f73f0649939286cf417c811607"

# Quality options in UI: label -> spotdl bitrate setting
BITRATE_CHOICES: Dict[str, Optional[str]] = {
    "auto": None,   # follow source bitrate
    "128": "128k",
    "192": "192k",
    "256": "256k",
    "320": "320k",
}

QUALITY_LABELS = {
    "auto": "Otomatis (ikuti sumber)",
    "320": "320 kbps (terbaik)",
    "256": "256 kbps",
    "192": "192 kbps",
    "128": "128 kbps (hemat)",
}

# Output folder & filename template. Filename is title-only; artist and album
# are used as folder names to keep the collection organized and avoid collisions.
OUTPUT_TEMPLATE = "{artist}/{album}/{title}.{output-ext}"

HISTORY_LIMIT = 50

# ============================================================
# Status models
# ============================================================

STATUS_QUEUED = "queued"
STATUS_RESOLVING = "resolving"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

_FINISHED = (STATUS_DONE, STATUS_SKIPPED, STATUS_ERROR)


@dataclass
class SongState:
    """Status of a single song within a download task."""

    name: str = ""
    artist: str = "Unknown Artist"
    album: str = ""
    cover_url: Optional[str] = None
    url: Optional[str] = None           # Source URL (Spotify), for progress matching
    download_url: Optional[str] = None  # Audio URL (YouTube Music), for progress matching
    progress: int = 0
    status: str = STATUS_QUEUED
    message: str = ""
    file_path: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "artist": self.artist,
            "album": self.album,
            "cover_url": self.cover_url,
            "progress": self.progress,
            "status": self.status,
            "message": self.message,
            "file_path": self.file_path,
            "error": self.error,
        }


def _song_state_from_song(song, status: str = STATUS_QUEUED) -> SongState:
    """Create UI state from a Song, including partial metadata during resolving."""
    return SongState(
        name=getattr(song, "name", None) or "(reading metadata...)",
        artist=(
            (song.artists[0] if getattr(song, "artists", None) else None)
            or getattr(song, "artist", None)
            or "Unknown Artist"
        ),
        album=getattr(song, "album_name", None) or "",
        cover_url=getattr(song, "cover_url", None),
        url=getattr(song, "url", None),
        download_url=getattr(song, "download_url", None),
        status=status,
        message="Waiting for metadata..." if status == STATUS_RESOLVING else "",
    )


@dataclass
class TaskState:
    """Status of a single download task (one link submitted by the user)."""

    task_id: str
    url: str
    quality: str = "auto"
    source: str = "spotify"  # spotify | youtube
    metadata_enabled: bool = True
    created_at: float = field(default_factory=time.time)
    # pending | resolving | downloading | done | error
    status: str = "pending"
    title: Optional[str] = None
    kind: Optional[str] = None  # track | album | playlist | artist
    songs: List[SongState] = field(default_factory=list)
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        songs = [s.to_dict() for s in self.songs]
        return {
            "task_id": self.task_id,
            "url": self.url,
            "quality": self.quality,
            "source": self.source,
            "metadata_enabled": self.metadata_enabled,
            "status": self.status,
            "title": self.title,
            "kind": self.kind,
            "songs": songs,
            "total": len(songs),
            "done": sum(1 for s in self.songs if s.status == STATUS_DONE),
            "skipped": sum(1 for s in self.songs if s.status == STATUS_SKIPPED),
            "failed": sum(1 for s in self.songs if s.status == STATUS_ERROR),
            "error": self.error,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ============================================================
# Helpers
# ============================================================

def normalize_url(url: str) -> str:
    """
    Normalize YouTube links to the format recognized by spotdl:
    - youtu.be/<id>            -> music.youtube.com/watch?v=<id>
    - (www.|m.)youtube.com/... -> music.youtube.com/...
    """
    url = url.strip()

    match = re.match(r"^(?:https?://)?youtu\.be/([\w-]+)", url)
    if match:
        return f"https://music.youtube.com/watch?v={match.group(1)}"

    # Anchor ^ ensures "music.youtube.com" is not replaced twice
    url = re.sub(
        r"^https?://(?:www\.|m\.)youtube\.com/",
        "https://music.youtube.com/",
        url,
    )
    return url


def _is_youtube_url(url: str) -> bool:
    """Return True if the original URL is from YouTube/YouTube Music."""
    lowered = (url or "").lower()
    return (
        "youtube.com" in lowered
        or "youtu.be/" in lowered
        or "music.youtube.com" in lowered
    )


def _youtube_video_id(url: str) -> Optional[str]:
    """Extract the video ID from a YouTube URL (normalized or not)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0] or None

    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id:
        return query_id

    match = re.match(r"^/(?:shorts|embed)/([\w-]+)", parsed.path)
    return match.group(1) if match else None


def _resolve_youtube_track(url: str):
    """Create a Song from YouTube Music metadata only, without querying Spotify."""
    from spotdl.types.song import Song
    from ytmusicapi import YTMusic

    video_id = _youtube_video_id(url)
    if not video_id:
        raise ValueError(f"Cannot extract video ID from YouTube URL: {url}")

    details = YTMusic(language="en").get_song(video_id)
    video = (details or {}).get("videoDetails") or {}
    title = (video.get("title") or "").strip()
    artist = (video.get("author") or "").strip()
    if not title:
        raise ValueError(f"YouTube metadata has no title: {url}")

    duration = video.get("lengthSeconds")
    try:
        duration = int(duration) if duration else None
    except (TypeError, ValueError):
        duration = None

    # The album field is only used for folder organization. Audio metadata
    # will be stripped again after spotdl finishes writing the file.
    song = Song.from_missing_data(
        name=title,
        artists=[artist] if artist else [],
        artist=artist or "Unknown Artist",
        genres=[],
        disc_number=1,
        disc_count=1,
        album_name="YouTube",
        album_artist=artist or "Unknown Artist",
        duration=duration,
        year=0,
        date="",
        track_number=1,
        tracks_count=1,
        album_id=f"youtube:{video_id}",
        url=url,
        download_url=url,
        cover_url=None,
    )
    song._skip_metadata = True
    song._source = "youtube"
    return song


def _resolve_youtube_collection(url: str):
    """Resolve a YouTube Music playlist/album without Song.from_search_term."""
    from spotdl.utils.search import create_ytm_album, create_ytm_playlist

    lowered = url.lower()
    if "olak5uy" in lowered or "album" in lowered:
        collection = create_ytm_album(url, fetch_songs=False)
    else:
        collection = create_ytm_playlist(url, fetch_songs=False)

    songs = []
    for song in collection.songs:
        songs.append(_mark_youtube_song(song))
    return songs


def _mark_youtube_song(song):
    """Mark a YouTube Song so it never receives Spotify tags."""
    song._skip_metadata = True
    song._source = "youtube"
    # Do not use YouTube playlist/song cover art as APIC.
    song.cover_url = None
    if not getattr(song, "download_url", None) and getattr(song, "url", None):
        song.download_url = song.url
    if not getattr(song, "url", None) and getattr(song, "download_url", None):
        song.url = song.download_url
    # Fill fields that are normally populated by reinit_song. Without this,
    # spotdl will automatically call reinit_song and pick Spotify metadata.
    song.genres = getattr(song, "genres", None) or []
    song.disc_number = getattr(song, "disc_number", None) or 1
    song.disc_count = getattr(song, "disc_count", None) or 1
    song.track_number = getattr(song, "track_number", None) or 1
    song.tracks_count = getattr(song, "tracks_count", None) or 1
    song.album_name = getattr(song, "album_name", None) or "YouTube"
    song.album_artist = (
        getattr(song, "album_artist", None)
        or getattr(song, "artist", None)
        or "Unknown Artist"
    )
    song.album_id = getattr(song, "album_id", None) or "youtube"
    song.date = getattr(song, "date", None) or ""
    song.year = getattr(song, "year", None) or 0
    return song


def detect_kind(url: str) -> str:
    lowered = url.lower()
    if "playlist" in lowered or "/browse/vlpl" in lowered or "list=" in lowered:
        return "playlist"
    if "album" in lowered or "olak5uy" in lowered:
        return "album"
    if "artist" in lowered:
        return "artist"
    return "track"


# Words that make YouTube Music search queries too specific
# (used for fallback search when a song is not found)
_QUERY_STRIP_WORDS = (
    "live", "acoustic", "remix", "remastered", "remaster", "version",
    "demo", "reverb", "slowed", "instrumental", "cover", "session",
)
_QUERY_STRIP_RE = re.compile(
    r"\s*[-â€“()\[\]]*\s*(" + "|".join(_QUERY_STRIP_WORDS) + r")\s*[-â€“()\[\]]*\s*",
    re.IGNORECASE,
)


def _simplify_for_search(song):
    """
    Create a copy of the song with a "looser" search query:
    - Only the main artist (drop feat./secondary)
    - Remove version markers (Live, Remix, Acoustic, etc.) from the title

    Returns a new Song object; the original is not modified.
    """
    import copy

    simple = copy.deepcopy(song)

    # Title: "Malam - Berlayar - Live" -> "Malam - Berlayar"
    simple.name = _QUERY_STRIP_RE.sub(" ", song.name or "").strip(" -â€“()[]")
    if not simple.name:
        simple.name = song.name

    # Artist: only the main artist
    if song.artists and len(song.artists) > 1:
        simple.artists = [song.artists[0]]
        simple.artist = song.artists[0]

    # Remove ISRC so fallback search is not locked to ISRC-based results
    simple.isrc = None

    return simple


def _relative_to_downloads(path: Path) -> str:
    """Path relative to the downloads folder (for the download button in UI)."""
    try:
        return path.resolve().relative_to(DOWNLOADS_DIR.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_error_list(errors: List[str]) -> Dict[str, str]:
    """
    Convert spotdl's error list (format: "<url> - <ErrorType>: <message>")
    into a dict {url: message} so it can be displayed per song.
    """
    parsed: Dict[str, str] = {}
    for entry in errors:
        head, sep, rest = entry.partition(" - ")
        if sep and head.startswith("http"):
            parsed[head] = rest
    return parsed


def _match_song_state(task: TaskState, song) -> Optional[SongState]:
    """
    Match a Song object from spotdl's progress callback with a SongState in the task.
    Attempt order: source URL -> audio URL -> name + main artist.
    Finished songs are not matched again (so duplicates within a playlist
    are still tracked one by one).
    """
    if song.url:
        for state in task.songs:
            if state.status not in _FINISHED and state.url == song.url:
                return state

    if song.download_url:
        for state in task.songs:
            if (
                state.status not in _FINISHED
                and state.download_url == song.download_url
            ):
                return state

    if song.name:
        main_artist = song.artists[0] if song.artists else None
        if main_artist:
            for state in task.songs:
                if (
                    state.status not in _FINISHED
                    and state.name == song.name
                    and state.artist == main_artist
                ):
                    return state

    return None


# ============================================================
# Engine
# ============================================================

class DownloadEngine:
    """
    Runs downloads in a single worker thread separate from the Flask server.

    - Tasks are processed SEQUENTIALLY via a queue (safe to add tasks anytime).
    - The spotdl instance is created lazily in the worker thread, so its
      asyncio event loop is created in the correct thread (important on Windows).
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskState] = {}
        self._lock = threading.RLock()
        self._queue: "queue.Queue[TaskState]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._spotdl = None
        self._init_lock = threading.Lock()
        self._stop = False

    # ---------- API for app.py ----------

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="download-worker", daemon=True
        )
        self._worker.start()

    def submit(self, url: str, quality: str = "auto") -> TaskState:
        if quality not in BITRATE_CHOICES:
            quality = "auto"
        task = TaskState(
            task_id=uuid.uuid4().hex[:12],
            url=url.strip(),
            quality=quality,
            source="youtube" if _is_youtube_url(url) else "spotify",
            metadata_enabled=not _is_youtube_url(url),
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._queue.put(task)
        return task

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def list_tasks(self, limit: int = 20) -> List[dict]:
        with self._lock:
            ordered = sorted(
                self._tasks.values(), key=lambda t: t.created_at, reverse=True
            )
            return [t.to_dict() for t in ordered[:limit]]

    # ---------- Worker ----------

    def _worker_loop(self) -> None:
        while not self._stop:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._process(task)
            except Exception:
                logger.exception("Task %s failed due to uncaught exception", task.task_id)
                task.status = "error"
                task.error = traceback.format_exc(limit=3)
            finally:
                task.finished_at = time.time()
                self._queue.task_done()

    def _process(self, task: TaskState) -> None:
        task.status = "resolving"
        task.started_at = time.time()
        task.progress = 2
        task.message = "Preparing to read link..."

        spotdl = self._get_spotdl()
        from spotdl.utils.search import get_simple_songs, reinit_song

        url = normalize_url(task.url)
        is_youtube = task.source == "youtube"
        logger.info(
            "Processing task %s: source=%s metadata_enabled=%s url=%s",
            task.task_id,
            task.source,
            task.metadata_enabled,
            task.url,
        )

        # ---- 1. Resolve link into a list of songs ----
        try:
            task.progress = 8
            task.message = "Reading songs from link..."
            # get_simple_songs performs blocking network requests.
            # Run it in a helper thread so the main worker can still update
            # task status, preventing the progress bar from appearing stuck.
            with ThreadPoolExecutor(max_workers=1) as resolver_executor:
                if is_youtube:
                    if detect_kind(url) == "track":
                        resolver_future = resolver_executor.submit(
                            lambda: [_resolve_youtube_track(url)]
                        )
                    else:
                        resolver_future = resolver_executor.submit(
                            _resolve_youtube_collection, url
                        )
                else:
                    resolver_future = resolver_executor.submit(
                        get_simple_songs,
                        [url],
                        use_ytm_data=is_youtube,
                    )
                while not resolver_future.done():
                    task.progress = min(11, task.progress + 1)
                    task.message = (
                        "Contacting Spotify / YouTube Music..."
                    )
                    time.sleep(0.5)
                simple_songs = resolver_future.result()
        except Exception as exc:
            logger.exception("Failed to read link for task %s: %s", task.task_id, exc)
            task.status = "error"
            task.progress = 100
            task.error = f"Failed to read link: {exc}"
            task.message = "Failed to read link"
            return

        if not simple_songs:
            task.status = "error"
            task.progress = 100
            task.message = "No songs found"
            task.error = (
                "No songs were found. Make sure the link is valid, public, "
                "and accessible."
            )
            return

        # get_simple_songs has already obtained the album/playlist list, but
        # each Spotify song's metadata needs to be re-populated by reinit_song.
        # For YouTube, reinit_song is FORBIDDEN because it always searches for
        # a Spotify song by title/artist and may inject incorrect metadata.
        # Run them in parallel and update the UI as each future completes,
        # instead of waiting for parse_query to finish entirely like spotdl's
        # default behavior.
        task.songs = [
            _song_state_from_song(song, STATUS_RESOLVING)
            for song in simple_songs
        ]
        task.progress = 12
        task.message = f"Found {len(simple_songs)} songs, reading metadata..."

        resolved_by_index = {}
        resolve_errors = {}
        resolver = _mark_youtube_song if is_youtube else reinit_song
        with ThreadPoolExecutor(max_workers=min(4, len(simple_songs))) as executor:
            futures = {
                executor.submit(resolver, song): index
                for index, song in enumerate(simple_songs)
            }
            completed = 0
            for future in as_completed(futures):
                index = futures[future]
                completed += 1
                state = task.songs[index]
                try:
                    resolved = future.result()
                    resolved_by_index[index] = resolved
                    state.name = resolved.name or state.name
                    state.artist = (
                        resolved.artists[0]
                        if resolved.artists
                        else (resolved.artist or state.artist)
                    )
                    state.album = resolved.album_name or state.album
                    state.cover_url = resolved.cover_url or state.cover_url
                    state.url = resolved.url or state.url
                    state.download_url = (
                        resolved.download_url or state.download_url
                    )
                    state.message = "Metadata loaded"
                except Exception as exc:
                    logger.exception(
                        "Failed to read metadata for song index %d in task %s: %s",
                        index,
                        task.task_id,
                        exc,
                    )
                    resolve_errors[index] = str(exc)
                    state.status = STATUS_ERROR
                    state.progress = 100
                    state.message = "Failed to read metadata"
                    state.error = str(exc)

                task.progress = 12 + int(83 * completed / len(simple_songs))
                task.message = (
                    f"Reading metadata: {completed}/{len(simple_songs)} songs"
                )

        # Build the final list in the original link order. Songs that failed
        # to resolve are still shown as errors, but are not sent to spotdl.
        songs = [
            resolved_by_index[index]
            for index in range(len(simple_songs))
            if index in resolved_by_index
        ]
        if not songs:
            task.status = "error"
            task.progress = 100
            task.message = "All songs failed to resolve"
            task.error = "All songs failed to resolve from the provided link."
            return

        task.songs = [
            _song_state_from_song(song)
            for song in songs
        ]
        task.progress = 0
        task.message = f"Metadata loaded: {len(songs)} songs"

        # Ensure each song has a track number for clean metadata
        # (priority: original track number > playlist position > list order)
        for idx, song in enumerate(songs, start=1):
            if not getattr(song, "track_number", None):
                song.track_number = getattr(song, "list_position", None) or idx

        # Task title & kind
        list_name = next(
            (s.list_name for s in songs if getattr(s, "list_name", None)), None
        )
        task.kind = detect_kind(url)
        if list_name:
            task.title = list_name
        else:
            first = songs[0]
            artist = first.artists[0] if first.artists else "Unknown"
            task.title = f"{artist} - {first.name or 'Unknown'}"

        task.songs = [
            SongState(
                name=song.name or "(untitled)",
                artist=(song.artists[0] if song.artists else song.artist)
                or "Unknown Artist",
                album=song.album_name or "",
                cover_url=song.cover_url,
                url=song.url,
                download_url=song.download_url,
            )
            for song in songs
        ]
        task.status = STATUS_DOWNLOADING
        task.progress = 0
        task.message = "Starting download..."

        # Keep the canonical Spotify metadata separate from the simplified
        # search objects used by the retry flow. The retry object may contain
        # incomplete metadata and must never be used for the final ID3 repair.
        canonical_songs = list(songs)

        # ---- 2. Set up progress callback & bitrate for this task ----
        downloader = spotdl.downloader
        downloader.errors.clear()
        downloader.settings["bitrate"] = BITRATE_CHOICES.get(task.quality)
        downloader.progress_handler.update_callback = _make_progress_callback(task)

        # ---- 3. Download all songs ----
        results = downloader.download_multiple_songs(songs)
        # ---- 3b. Fallback: retry failed songs with a simplified query ----
        # (common issue: Spotify title "X - Berlayar - Live" does not match
        #  YTM title "X - Berlayar [Live] (feat. Y)" -> spotdl matcher discards all results)
        failed_idx = [i for i, (_, p) in enumerate(results) if p is None]
        if failed_idx:
            # A metadata failure can still leave a complete audio file on
            # disk. Repair that file first instead of downloading the audio a
            # second time. This is the important path for Windows file locks.
            unrecovered_idx = []
            for index in failed_idx:
                existing_path = _find_existing_file(canonical_songs[index])
                if existing_path is None:
                    unrecovered_idx.append(index)
                    continue

                if task.metadata_enabled:
                    _repair_spotify_metadata(
                        existing_path, canonical_songs[index]
                    )
                    recovered = _audio_has_spotify_metadata(existing_path)
                else:
                    _strip_audio_tags(existing_path)
                    recovered = True

                if recovered:
                    results[index] = (canonical_songs[index], existing_path)
                    logger.info(
                        "Recovered existing audio after download failure: %s",
                        existing_path,
                    )
                else:
                    unrecovered_idx.append(index)

            failed_idx = unrecovered_idx
        if failed_idx:
            retry_songs = [
                (i, _simplify_for_search(songs[i])) for i in failed_idx
            ]

            # Update SongState so fallback progress is tracked
            for i, simple in retry_songs:
                state = task.songs[i]
                state.status = STATUS_DOWNLOADING
                state.progress = 0
                state.message = "Retrying (alternative search)"
                songs[i] = simple

            downloader.errors.clear()
            retry_results = downloader.download_multiple_songs(
                [simple for _, simple in retry_songs]
            )
            # Copy retry results back to their original index positions
            for (i, _), r in zip(retry_songs, retry_results):
                results[i] = r
        # ---- 4. Finalize per-song status ----
        # asyncio.gather preserves order: results[i] always pairs with songs[i]
        error_map = _parse_error_list(downloader.errors)
        for i, (result_song, path) in enumerate(results):
            if i >= len(task.songs):
                break
            state = task.songs[i]
            canonical_song = canonical_songs[i]

            # Update final metadata (spotdl may enrich data during the process)
            if result_song.name:
                state.name = result_song.name
            if result_song.artists:
                state.artist = result_song.artists[0]
            if result_song.album_name:
                state.album = result_song.album_name
            if result_song.cover_url:
                state.cover_url = result_song.cover_url

            if path is not None:
                # path is set = success OR skipped because file already exists
                output_path = Path(path)
                if task.metadata_enabled:
                    _repair_spotify_metadata(output_path, canonical_song)
                    metadata_ready = _audio_has_spotify_metadata(output_path)
                else:
                    _strip_audio_tags(output_path)
                    metadata_ready = True
                state.file_path = _relative_to_downloads(output_path)
                state.progress = 100
                if state.status != STATUS_SKIPPED:
                    state.status = STATUS_DONE
                    state.message = (
                        "Completed"
                        if metadata_ready
                        else "Completed (metadata repair failed)"
                    )
            else:
                state.status = STATUS_ERROR
                state.progress = 100
                state.error = (
                    error_map.get(result_song.url)
                    or state.message
                    or "Download failed"
                )
                # --- Rescue: song marked as failed, but the file may already
                # be on disk (e.g. embed metadata failed briefly due to
                # antivirus file lock, then spotdl's internal retry failed
                # entirely). Check whether the output file actually exists.
                # A simplified retry may use a different title/artist in the
                # output template. Check both the canonical and retry objects,
                # but always repair using the canonical Spotify metadata.
                existing_path = (
                    _find_existing_file(canonical_song)
                    or _find_existing_file(result_song)
                )
                if existing_path is not None:
                    if task.metadata_enabled:
                        _repair_spotify_metadata(existing_path, canonical_song)
                        metadata_ready = _audio_has_spotify_metadata(existing_path)
                    else:
                        _strip_audio_tags(existing_path)
                        metadata_ready = True
                    state.status = STATUS_DONE
                    state.file_path = _relative_to_downloads(existing_path)
                    state.message = (
                        "Completed"
                        if metadata_ready
                        else "Completed (metadata repair failed)"
                    )
                    state.error = None

        failed = sum(1 for s in task.songs if s.status == STATUS_ERROR)
        if task.songs and failed == len(task.songs):
            task.status = "error"
            task.error = task.songs[0].error or "All songs failed to download."
        else:
            task.status = "done"
        task.progress = 100
        task.message = "Completed" if task.status == "done" else "Failed"

        self._prune_history()

    # ---------- Spotdl & FFmpeg initialization ----------

    def _get_spotdl(self):
        if self._spotdl is not None:
            return self._spotdl

        with self._init_lock:
            if self._spotdl is not None:
                return self._spotdl

            # Install matcher patches before spotdl is used
            _apply_matcher_patch()

            self._ensure_ffmpeg()
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

            from spotdl import Spotdl

            settings = {
                # youtube-music primary, youtube as fallback search
                "audio_providers": ["youtube-music", "youtube"],
                "format": "mp3",
                "bitrate": None,  # mutated per task based on user choice
                "output": (DOWNLOADS_DIR / OUTPUT_TEMPLATE).as_posix(),
                "overwrite": "skip",   # duplicate skip feature
                "threads": 4,
                "filter_results": True,
                "simple_tui": True,    # disable Rich progress bar in console
                "ytm_data": True,
            }
            self._spotdl = Spotdl(
                client_id=DEFAULT_CLIENT_ID,
                client_secret=DEFAULT_CLIENT_SECRET,
                downloader_settings=settings,
            )
            return self._spotdl

    @staticmethod
    def _ensure_ffmpeg() -> None:
        """Download FFmpeg to spotdl's folder if not installed on the system."""
        if shutil.which("ffmpeg"):
            return
        try:
            from spotdl.utils.ffmpeg import download_ffmpeg, get_local_ffmpeg

            if get_local_ffmpeg():
                return
            logging.getLogger(__name__).info(
                "FFmpeg not found â€” downloading automatically (one-time only)..."
            )
            download_ffmpeg()
        except Exception as exc:
            raise RuntimeError(
                "FFmpeg is not available and auto-download failed. "
                "Install FFmpeg manually from https://ffmpeg.org/download.html "
                "then restart the application."
            ) from exc

    # ---------- History ----------

    def _prune_history(self) -> None:
        with self._lock:
            if len(self._tasks) <= HISTORY_LIMIT:
                return
            ordered = sorted(
                self._tasks.values(), key=lambda t: t.created_at, reverse=True
            )
            for task in ordered[HISTORY_LIMIT:]:
                self._tasks.pop(task.task_id, None)


def _make_progress_callback(task: TaskState):
    """
    Spotdl progress callback: called from the worker thread with
    signature (tracker, message). Only mutates state â€” must not
    execute Flask code here.
    """

    def on_progress(tracker, message: str) -> None:
        try:
            song = tracker.song
            state = _match_song_state(task, song)
            if state is None:
                return

            progress = int(getattr(tracker, "progress", 0) or 0)
            state.progress = max(state.progress, min(progress, 100))
            state.message = message

            if message == "Done":
                state.status = STATUS_DONE
                state.progress = 100
            elif message == "Skipped":
                state.status = STATUS_SKIPPED
                state.progress = 100
            elif message == "Error":
                state.status = STATUS_ERROR
                state.progress = 100
            elif state.status in (STATUS_QUEUED, STATUS_ERROR):
                state.status = STATUS_DOWNLOADING
        except Exception:
            # Progress callback must not crash the download
            pass

    return on_progress

def _patched_embed_metadata(output_file, song, id3_separator: str = "/", skip_album_art: bool = False):
    """
    Original spotdl embed_metadata + retry.

    On Windows, antivirus/Defender often locks a new .mp3 file briefly
    after ffmpeg finishes writing it -> mutagen fails to save ID3 tags
    (PermissionError) -> spotdl marks the song as failed even though
    the audio is intact. Retry a few times with delays before giving up.
    """
    if getattr(song, "_skip_metadata", False):
        logger.info("Metadata skipped for YouTube source: %s", output_file)
        return None

    # spotdl 4.5.2 passes ``None`` to EasyID3 for the TSRC frame when
    # Spotify does not provide an ISRC. Mutagen rejects None (ValueError:
    # Invalid MultiSpec data), so the audio completes but all metadata
    # fails to save. An empty string is accepted by EasyID3 and still
    # means "ISRC not available".
    if not getattr(song, "isrc", None):
        logger.debug("ISRC empty for %s; using empty string", output_file)
        song.isrc = ""

    from spotdl.utils.metadata import embed_metadata as _orig_embed_metadata

    last_exc = None
    for attempt in range(3):
        try:
            return _orig_embed_metadata(
                output_file,
                song,
                id3_separator=id3_separator,
                skip_album_art=skip_album_art,
            )
        except Exception as exc:
            last_exc = exc
            logger.exception(
                "Embed metadata failed (attempt %d/3) for %s: %s",
                attempt + 1,
                output_file,
                exc,
            )
            if attempt < 2:
                delay = 2.0 + attempt * 2.0  # 2s lalu 4s
                logger.warning(
                    "Retrying embed metadata in %ss: %s",
                    delay,
                    output_file,
                )
                time.sleep(delay)
    raise last_exc


def _audio_has_spotify_metadata(path: Path) -> bool:
    """Check core tags and cover art before performing metadata repair."""
    try:
        from mutagen import File

        audio = File(path, easy=False)
        tags = getattr(audio, "tags", None)
        if not tags:
            return False
        keys = {str(key).upper() for key in tags.keys()}
        return bool(
            {"TIT2", "TPE1", "TALB"}.issubset(keys)
            and any(key.startswith("APIC:") for key in keys)
        )
    except Exception:
        logger.exception("Failed to check audio metadata: %s", path)
        return False


def _repair_spotify_metadata(path: Path, song) -> bool:
    """Repair Spotify tags on new results and skipped files."""
    if not path.is_file() or _audio_has_spotify_metadata(path):
        return False

    logger.warning("Spotify metadata incomplete, repairing: %s", path)
    try:
        _patched_embed_metadata(path, song)
        logger.info("Spotify metadata successfully repaired: %s", path)
        return True
    except Exception:
        logger.exception("Spotify metadata repair failed: %s", path)
        return False


def _strip_audio_tags(path: Path) -> bool:
    """Remove all audio tags/cover art from files downloaded via YouTube links."""
    if not path.is_file():
        return False
    try:
        from mutagen import File

        audio = File(path, easy=False)
        if audio is None or not getattr(audio, "tags", None):
            return False
        audio.delete()
        logger.info("Metadata and cover art removed from YouTube file: %s", path)
        return True
    except Exception:
        logger.exception("Failed to remove metadata from YouTube file: %s", path)
        return False

def _find_existing_file(song) -> Optional[Path]:
    """
    Check whether the output file for a song already exists on disk.

    Used as a rescue: if spotdl marks a song as failed (e.g. embed
    metadata hit an antivirus lock) but the audio is actually intact,
    the song is still counted as completed. Returns the full file path,
    or None if the file is not found.
    """
    try:
        from spotdl.utils.formatter import create_file_name

        for ext in ("mp3", "m4a", "flac", "ogg", "opus", "wav"):
            candidate = create_file_name(
                song, OUTPUT_TEMPLATE, file_extension=ext
            )
            if candidate is None:
                continue
            full = DOWNLOADS_DIR / candidate
            if full.exists():
                return full
    except Exception:
        pass
    return None

