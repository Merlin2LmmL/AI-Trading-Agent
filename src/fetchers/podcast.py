"""
Podcast fetcher — downloads new episodes via RSS and transcribes with whisper.cpp.
Requires whisper.cpp to be built with GGML_HIP=1 for AMD GPU acceleration.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import hashlib
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
    return Path("./whisper.cpp/build/bin/whisper-cli")  # Fallback


WHISPER_CPP_PATH = _get_whisper_path()
WHISPER_MODEL = Path(os.getenv("WHISPER_MODEL", "./whisper.cpp/models/ggml-large-v3.bin"))


def whisper_available() -> bool:
    """Check if whisper.cpp binary and model are present."""
    global WHISPER_CPP_PATH
    if not WHISPER_CPP_PATH.exists():
        WHISPER_CPP_PATH = _get_whisper_path()
    return WHISPER_CPP_PATH.exists() and WHISPER_MODEL.exists()


def _get_cache_dir() -> Path:
    path = Path("output/podcasts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_cache_key(url: str, title: str) -> str:
    combined = f"{title}_{url}"
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


async def transcribe_audio(audio_path: Optional[Path], name: str = "Unknown Podcast", audio_url: Optional[str] = None) -> Optional[str]:
    """
    Transcribe audio file using either Google Gemini or Local Whisper.
    """
    from src.utils.dashboard import state as dash_state
    provider = os.getenv("TRANSCRIPTION_PROVIDER", "google").lower()
    
    import time
    dash_state.transcription_current_podcast = name
    dash_state.transcription_progress = 0
    dash_state.task_start_time = time.time()

    try:
        if provider == "google":
            from src.llm.gemini import GeminiClient
            client = GeminiClient()
            log.info("podcast.transcribe.google", podcast=name)
            
            user_prompt = f"Summarize the content of the podcast '{name}'."
            if audio_url:
                user_prompt += f" Source URL: {audio_url}"
            
            # If we have a local path, upload it for true transcription
            files = []
            if audio_path and audio_path.exists():
                g_file = client.upload_file(str(audio_path), display_name=name)
                # Wait for ACTIVE state
                while True:
                    f_status = client.client.files.get(name=g_file.name)
                    if f_status.state.name == "ACTIVE": break
                    await asyncio.sleep(2)
                files = [g_file]

            text, _, _ = await client.complete(
                model=os.getenv("API_STAGE1_MODEL", "gemini-3.1-flash-lite-preview"),
                user_prompt=user_prompt,
                files=files,
                thinking=True,
                use_tools=True
            )
            return text

        else:
            # Local Whisper logic
            if not whisper_available():
                log.warning("whisper.not_available")
                return None

            txt_output = audio_path.with_suffix(".txt")
            cmd = [
                str(WHISPER_CPP_PATH),
                "-m", str(WHISPER_MODEL),
                "-f", str(audio_path),
                "-l", "auto",
                "--output-txt",
                "--output-file", str(audio_path.with_suffix("")),
                "--no-timestamps",
                "--print-progress",
                "--threads", "8"
            ]

            log.info("whisper.process.start", podcast=name)
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL, # IMPORTANT: avoid pipe deadlock
                stderr=asyncio.subprocess.PIPE
            )
            
            async def _read_stderr(stream):
                while True:
                    line = await stream.readline()
                    if not line: break
                    decoded = line.decode().strip()
                    if "progress =" in decoded:
                        import re
                        match = re.search(r"progress\s*=\s*(\d+)%", decoded)
                        if match:
                            prog = int(match.group(1))
                            dash_state.transcription_progress = prog
                            dash_state.task_progress = prog
            
            # 15 minute timeout for local whisper
            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_stderr(process.stderr), process.wait()),
                    timeout=900
                )
            except asyncio.TimeoutError:
                log.error("whisper.timeout", podcast=name)
                process.terminate()
                return None
            
            if process.returncode != 0:
                log.warning("whisper.failed", code=process.returncode)
                return None

            if txt_output.exists():
                text = txt_output.read_text(encoding="utf-8").strip()
                log.info("whisper.done", chars=len(text))
                return text

    except Exception as e:
        log.error("transcription.error", error=str(e))
    finally:
        dash_state.transcription_current_podcast = ""
        dash_state.transcription_progress = 0
        dash_state.task_progress = 0
        dash_state.current_task = ""

    return None


async def download_episode(session: aiohttp.ClientSession, audio_url: str, dest_path: Path) -> bool:
    try:
        async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200: return False
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        return True
    except: return False


def _get_audio_url(entry) -> Optional[str]:
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("audio"):
                return enc.get("href") or enc.get("url")
    return None


def _entry_is_recent(entry, max_age_hours: int = 48) -> bool:
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
                return dt >= cutoff
            except: pass
    return False


async def fetch_podcast_transcripts(podcast_configs: list[dict], max_age_hours: int = 48) -> list[RawArticle]:
    provider = os.getenv("TRANSCRIPTION_PROVIDER", "google").lower()
    articles = []

    async with aiohttp.ClientSession() as session:
        for pod in podcast_configs:
            rss_url = pod.get("rss")
            if not rss_url: continue
            
            name = pod.get("name", "Unknown Podcast")
            log.info("podcast.fetching", name=name)
            max_eps = pod.get("max_episodes_per_day", 1)

            try:
                async with session.get(rss_url, timeout=15) as resp:
                    content = await resp.read()
                feed = feedparser.parse(content)
            except: continue

            eps_processed = 0
            for entry in feed.entries:
                if eps_processed >= max_eps: break
                if not _entry_is_recent(entry, max_age_hours): continue
                
                audio_url = _get_audio_url(entry)
                if not audio_url: continue

                title = entry.get("title", f"{name} episode")
                duration = entry.get("itunes_duration", "unknown")
                log.info("podcast.processing", title=title, duration=duration)
                
                # Caching check
                cache_key = _get_cache_key(audio_url, title)
                cache_path = _get_cache_dir() / f"{cache_key}.txt"
                
                transcript = None
                if cache_path.exists():
                    log.info("podcast.cache_hit", title=title)
                    transcript = cache_path.read_text(encoding="utf-8")
                else:
                    log.info("podcast.cache_miss", title=title, duration=duration)
                    from src.utils import dashboard
                    dashboard.state.current_task = f"Transcribing: {title}"
                    
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                        tmp_path = Path(tmp.name)

                    if await download_episode(session, audio_url, tmp_path):
                        transcript = await transcribe_audio(tmp_path, name=title, audio_url=audio_url)
                        if transcript:
                            cache_path.write_text(transcript, encoding="utf-8")
                            dashboard.state.podcasts_transcribed += 1
                    
                    tmp_path.unlink(missing_ok=True)
                    if provider == "google": await asyncio.sleep(10) # Pacing

                if transcript:
                    articles.append(RawArticle(
                        source_name=name,
                        source_type=SourceType.PODCAST,
                        language=pod.get("language", "en"),
                        credibility=Credibility(pod.get("credibility", "MEDIUM")),
                        title=title,
                        url=entry.get("link"),
                        published=entry.get("published", ""),
                        full_text=transcript,
                    ))
                    eps_processed += 1

    return articles
