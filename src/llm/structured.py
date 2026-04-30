"""
Structured output enforcement for Ollama LLM responses.
Handles JSON parsing, validation, and retry logic.
"""
from __future__ import annotations

import json
import re
from typing import TypeVar, Type

from pydantic import BaseModel, ValidationError

import structlog

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


def extract_json_from_response(text: str) -> str:
    """
    Extract JSON from an LLM response that may contain markdown fences,
    thinking traces, or other noise.

    Uses a bracket-depth scanner instead of rfind() to correctly handle
    nested JSON (e.g. {"ideas": [{"ticker": "AAPL", "data": {...}}]}).
    rfind() would match the wrong closing bracket on nested structures.
    """
    # Strip thinking traces: <think>...</think> (DeepSeek-R1, Qwen3)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Strip markdown code fences  ```json ... ``` or ``` ... ```
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    def _find_balanced(s: str, open_ch: str, close_ch: str, start: int) -> int:
        """Return the index of the closing bracket that balances open_ch at start."""
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        return -1

    # Find the first [ or { and extract the correctly balanced block
    array_start = text.find('[')
    object_start = text.find('{')

    candidates: list[tuple[int, str]] = []

    if array_start != -1:
        end = _find_balanced(text, '[', ']', array_start)
        if end != -1:
            candidates.append((array_start, text[array_start:end + 1]))

    if object_start != -1:
        end = _find_balanced(text, '{', '}', object_start)
        if end != -1:
            candidates.append((object_start, text[object_start:end + 1]))

    if candidates:
        # Pick whichever valid block starts earliest in the string
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1].strip()

    return text


def extract_thinking_trace(text: str) -> str | None:
    """Extract DeepSeek-R1 thinking trace for auditability."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else None


def parse_and_validate(
    raw_response: str,
    model: Type[T],
) -> T:
    """
    Parse LLM response as JSON and validate against a Pydantic model.
    Raises ValueError on failure (caller handles retries).
    """
    cleaned = extract_json_from_response(raw_response)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}\n\nRaw (cleaned):\n{cleaned[:500]}") from e

    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Pydantic validation error: {e}") from e


def parse_idea_list(raw_response: str) -> list[dict]:
    """
    Parse Stage 1 response which is a JSON array of IdeaSummary objects.
    Returns raw dicts for flexible handling.
    """
    cleaned = extract_json_from_response(raw_response)
    if not cleaned or cleaned in ("{}", "[]"):
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e}\n\nRaw:\n{cleaned[:500]}") from e

    if not isinstance(data, list):
        # Case A: Model returns a dictionary
        if isinstance(data, dict):
            # If the model returned an error explanation or is empty, treat as 0 ideas
            if "error" in data or not data:
                log.debug("parser.suppressed_error_dict", content=data)
                return []

            # Check for wrapped lists
            for key in ("ideas", "results", "items", "IdeaSummary", "json"):
                if key in data:
                    val = data[key]
                    if isinstance(val, list):
                        return val
                    elif isinstance(val, str):
                        try:
                            parsed = json.loads(val)
                            if isinstance(parsed, list):
                                return parsed
                        except json.JSONDecodeError:
                            pass
            
            # Case B: Model returned a single object instead of a list
            # If it has keys like 'ticker' or 'headline', it's an IdeaSummary
            if "ticker" in data or "headline" in data:
                return [data]
                
        raise ValueError(f"Expected a JSON array, got {type(data)} with keys {list(data.keys()) if isinstance(data, dict) else None}")

    return data
