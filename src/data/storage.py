"""
Output storage — manages the daily output directory and JSON serialization.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


def get_output_dir(date: str | None = None) -> Path:
    """Return (and create) the output directory for today's run."""
    base = Path(os.getenv("OUTPUT_DIR", "output"))
    date = date or datetime.now().strftime("%Y-%m-%d")
    out_dir = base / date
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def save_json(data: Any, filename: str, date: str | None = None) -> Path:
    """Serialize data to JSON and save to today's output directory."""
    out_dir = get_output_dir(date)
    path = out_dir / filename

    if hasattr(data, "model_dump"):
        # Pydantic model
        payload = data.model_dump(mode="json")
    elif isinstance(data, list) and data and hasattr(data[0], "model_dump"):
        payload = [item.model_dump(mode="json") for item in data]
    else:
        payload = data

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log.info("storage.saved", path=str(path), size_bytes=path.stat().st_size)
    return path


def load_json(filename: str, date: str | None = None) -> Any:
    """Load a JSON file from today's output directory."""
    out_dir = get_output_dir(date)
    path = out_dir / filename

    if not path.exists():
        raise FileNotFoundError(f"Output file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_markdown(content: str, filename: str, date: str | None = None) -> Path:
    """Save a markdown report."""
    out_dir = get_output_dir(date)
    path = out_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("storage.saved_markdown", path=str(path))
    return path


def load_seen_media() -> set[str]:
    """Load the global cache of seen article URLs/titles."""
    base = Path(os.getenv("OUTPUT_DIR", "output"))
    path = base / "seen_media.json"
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception as e:
        log.warning("storage.load_seen_media_failed", error=str(e))
        return set()


def save_seen_media(seen: set[str]) -> None:
    """Save the global cache of seen article URLs/titles."""
    base = Path(os.getenv("OUTPUT_DIR", "output"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / "seen_media.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("storage.save_seen_media_failed", error=str(e))
