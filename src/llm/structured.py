"""
Structured output enforcement for Ollama LLM responses.
Handles JSON parsing, validation, and retry logic.
"""
from __future__ import annotations

import json
import re
from typing import TypeVar, Type, Any

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

    def _close_balanced_blocks(s: str) -> str:
        """Add missing closing brackets to a truncated JSON string."""
        stack = []
        in_string = False
        escape_next = False
        for i in range(len(s)):
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
            if ch == '{':
                stack.append('}')
            elif ch == '[':
                stack.append(']')
            elif ch in ('}', ']'):
                if stack and stack[-1] == ch:
                    stack.pop()
        
        # Close remaining open blocks in reverse order
        return s + "".join(reversed(stack))

    # Find all top-level balanced blocks
    blocks = []
    i = 0
    while i < len(text):
        if text[i] in ('[', '{'):
            opener = text[i]
            closer = ']' if opener == '[' else '}'
            end = _find_balanced(text, opener, closer, i)
            if end != -1:
                blocks.append(text[i:end + 1])
                i = end + 1
                continue
            else:
                # Truncated block found: try to repair it
                repaired = _close_balanced_blocks(text[i:])
                blocks.append(repaired)
                break
        i += 1

    if not blocks:
        return text.strip()

    if len(blocks) == 1:
        return blocks[0].strip()
    
    # If multiple blocks found (e.g. model output multiple objects without a wrapper array)
    # wrap them in an array ourselves
    combined = []
    for b in blocks:
        b = b.strip()
        if b.startswith('['):
            # Extract elements from the array
            try:
                data = json.loads(b)
                if isinstance(data, list):
                    combined.extend([json.dumps(item) for item in data])
                else:
                    combined.append(b)
            except:
                combined.append(b)
        else:
            combined.append(b)
    
    return "[" + ",".join(combined) + "]"



def extract_thinking_trace(text: str) -> str | None:
    """Extract DeepSeek-R1 thinking trace for auditability."""
    match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _recursive_strip_nulls(data: Any) -> Any:
    """Recursively remove None values from lists and dicts, or replace with empty defaults."""
    if isinstance(data, list):
        # Remove None, but if the list becomes empty, it's fine (defaults in Pydantic handle it)
        return [_recursive_strip_nulls(v) for v in data if v is not None]
    if isinstance(data, dict):
        return {k: _recursive_strip_nulls(v) for k, v in data.items() if v is not None}
    return data


def unwrap_json(data: Any, root_keys: list[str] = None) -> Any:
    """
    Handle the case where an LLM wraps the entire response in a single root key.
    Example: {"research_report": {...}} -> {...}
    """
    if not isinstance(data, dict) or len(data) != 1:
        return data
    
    key = list(data.keys())[0]
    # Standard keys to unwrap automatically if they are the ONLY key
    auto_keys = ["research_report", "report", "output", "data", "analysis", "portfolio_update", "result", "json", "research"]
    if root_keys:
        auto_keys.extend(root_keys)
        
    if key.lower() in [k.lower() for k in auto_keys]:
        nested = data[key]
        if isinstance(nested, dict):
            log.info("parser.unwrapping_nested_json", root_key=key)
            return nested
    return data



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

    # Hardening: unwrap nested structures and strip nulls
    data = unwrap_json(data)
    data = _recursive_strip_nulls(data)

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
                        return [_recursive_strip_nulls(v) for v in val if isinstance(v, dict)]
                    elif isinstance(val, str):
                        try:
                            parsed = json.loads(val)
                            if isinstance(parsed, list):
                                return [_recursive_strip_nulls(p) for p in parsed if isinstance(p, dict)]
                        except json.JSONDecodeError:
                            pass
            
            # Case B: Model returned a single object instead of a list
            # If it has keys like 'ticker' or 'headline', it's an IdeaSummary
            if "ticker" in data or "headline" in data:
                return [_recursive_strip_nulls(data)]
                
        # If it's a string that might be JSON, try one more parse
        if isinstance(data, str) and (data.strip().startswith('{') or data.strip().startswith('[')):
            try:
                second_parse = json.loads(data)
                if isinstance(second_parse, (list, dict)):
                    return parse_idea_list(data) # Recursive call with the inner JSON
            except:
                pass

        return []

    # Ensure we only return a list of dictionaries and strip nulls
    return [_recursive_strip_nulls(item) for item in data if isinstance(item, dict)]
