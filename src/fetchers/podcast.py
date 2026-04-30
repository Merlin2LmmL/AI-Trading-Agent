"""
Podcast fetcher — downloads new episodes via RSS and transcribes with whisper.cpp.
Requires whisper.cpp to be built with GGML_HIP=1 for AMD GPU acceleration.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import aiohttp
import feedparser
import structlog

from src.data.models import RawArticle, SourceType, Credibility

log = structlog.get_logger()

def _get_whisper_path() -> Path:
    """Find the whisper.cpp binary in common locations."""
    env_path = os.getenv("WHISPER_CPP_PATH")
    if env_path:
        return Path(env_path)

    # Search for modern whisper-cli or legacy main
    # prioritize the build directory we just created
    candidates = [
        "./whisper.cpp/build/bin/whisper-cli",
        "./whisper.cpp/build/bin/main",
        "./whisper.cpp/whisper-cli",
        "./whisper.cpp/main",
        "./bin/whisper.cpp/main",
    ]
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return Path("./whisper.cpp/build/bin/whisper-cli")  # Fallback to new standard


WHISPER_CPP_PATH = _get_whisper_path()
WHISPER_MODEL = Path(os.getenv("WHISPER_MODEL", "./whisper.cpp/models/ggml-large-v3.bin"))


def whisper_available() -> bool:
    """Check if whisper.cpp binary and model are present."""
    # Re-check path in case it was built since last import
    global WHISPER_CPP_PATH
    if not WHISPER_CPP_PATH.exists():
        WHISPER_CPP_PATH = _get_whisper_path()
    return WHISPER_CPP_PATH.exists() and WHISPER_MODEL.exists()


async def transcribe_audio(audio_path: Path, name: str = "Unknown Podcast") -> Optional[str]:
    """
    Transcribe audio file using whisper.cpp (ASYNC).
    Built with GGML_HIP=1 for AMD GPU acceleration.

    Returns transcript text or None on failure.
    """
    from src.utils.dashboard import state as dash_state
    
    if not whisper_available():
        log.warning(
            "whisper.not_available",
            hint="Build whisper.cpp with: make GGML_HIP=1 && ./models/download-ggml-model.sh large-v3",
        )
        return None

    dash_state.transcription_current_podcast = name
    dash_state.transcription_progress = 0

    txt_output = audio_path.with_suffix(".txt")
    cmd = [
        str(WHISPER_CPP_PATH),
        "-m", str(WHISPER_MODEL),
        "-f", str(audio_path),
        "-l", "auto",           # Auto-detect language (handles German + English)
        "--output-txt",
        "--output-file", str(audio_path.with_suffix("")),
        "--no-timestamps",      # Cleaner output for LLM ingestion
        "--print-progress",     # Capture transcription progress
    ]

    log.info("whisper.transcribing", file=str(audio_path), podcast=name)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Read stderr line by line for progress updates
        async def _read_stderr(stream):
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if "progress =" in decoded:
                    # Parse "whisper_full_with_state: progress = 10%"
                    import re
                    match = re.search(r"progress\s*=\s*(\d+)%", decoded)
                    if match:
                        prog = int(match.group(1))
                        dash_state.transcription_progress = prog
        
        # Run stderr reading and process waiting concurrently
        # We don't use communicate() because it tries to read the streams we are already reading
        await asyncio.gather(
            _read_stderr(process.stderr),
            process.wait()
        )
        
        if process.returncode != 0:
            log.warning("whisper.failed", code=process.returncode)
            return None

        if txt_output.exists():
            text = txt_output.read_text(encoding="utf-8").strip()
            log.info("whisper.done", chars=len(text))
            return text
    except asyncio.TimeoutError:
        log.warning("whisper.timeout")
    except Exception as e:
        log.warning("whisper.error", error=str(e))
    finally:
        dash_state.transcription_current_podcast = ""
        dash_state.transcription_progress = 0

    return None


async def download_episode(
    session: aiohttp.ClientSession,
    audio_url: str,
    dest_path: Path,
) -> bool:
    """Download a podcast episode audio file."""
    try:
        async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200:
                log.warning("podcast.download_failed", url=audio_url, status=resp.status)
                return False
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        log.info("podcast.downloaded", path=str(dest_path))
        return True
    except Exception as e:
        log.warning("podcast.download_error", url=audio_url, error=str(e))
        return False


def _get_audio_url(entry) -> Optional[str]:
    """Extract audio enclosure URL from a feed entry."""
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("audio"):
                return enc.get("href") or enc.get("url")
    return None


def _entry_is_recent(entry, max_age_hours: int = 24) -> bool:
    """Check if podcast episode was published recently."""
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                return dt >= cutoff
            except Exception:
                pass
    return False  # If no date, skip (podcasts publish on schedule, err on side of skipping)


async def fetch_podcast_transcripts(
    podcast_configs: list[dict],
    max_age_hours: int = 48,  # Wider window for podcasts (not all publish daily)
) -> list[RawArticle]:
    """
    For each podcast RSS feed, check for new episodes, download audio,
    and transcribe with whisper.cpp. Returns transcripts as RawArticle objects.
    """
    if not whisper_available():
        log.warning(
            "podcast.skipping_all",
            reason="whisper.cpp not built yet — see README for setup instructions",
        )
        return []

    articles = []

    async with aiohttp.ClientSession() as session:
        for pod in podcast_configs:
            rss_url = pod.get("rss")
            if not rss_url:
                log.warning("podcast.missing_rss", name=pod.get("name", "Unknown"))
                continue
            
            name = pod.get("name", "Unknown Podcast")
            log.info("podcast.fetching", name=name)
            language = pod.get("language", "en")
            credibility = Credibility(pod.get("credibility", "MEDIUM"))
            max_eps = pod.get("max_episodes_per_day", 1)

            try:
                async with session.get(rss_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    content = await resp.read()
                feed = feedparser.parse(content)
                log.debug("podcast.feed_parsed", name=name, entries=len(feed.entries))
            except Exception as e:
                log.warning("podcast.rss_error", name=name, error=str(e))
                continue

            eps_processed = 0
            for entry in feed.entries:
                if eps_processed >= max_eps:
                    break

                recent = _entry_is_recent(entry, max_age_hours)
                if not recent:
                    log.debug("podcast.entry_skipped_old", title=entry.get("title", "?"))
                    continue

                audio_url = _get_audio_url(entry)
                if not audio_url:
                    log.debug("podcast.entry_no_audio", title=entry.get("title", "?"))
                    continue

                title = entry.get("title", f"{name} episode")
                published = entry.get("published", "")

                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                success = await download_episode(session, audio_url, tmp_path)
                if not success:
                    tmp_path.unlink(missing_ok=True)
                    continue

                transcript = await transcribe_audio(tmp_path, name=title)
                tmp_path.unlink(missing_ok=True)

                if transcript:
                    article = RawArticle(
                        source_name=name,
                        source_type=SourceType.PODCAST,
                        language=language,
                        credibility=credibility,
                        title=title,
                        url=entry.get("link"),
                        published=published,
                        full_text=transcript,
                    )
                    articles.append(article)
                    eps_processed += 1

    log.info("podcast.all_done", transcripts=len(articles))
    return articles
