import os
import asyncio
import time
import re
from typing import Optional, List, Dict, Any
from google import genai
from google.genai import types
import structlog
import warnings
import logging

warnings.filterwarnings("ignore", category=UserWarning, module="google.genai")
warnings.filterwarnings("ignore", message="Async interactions client cannot use aiohttp")

# Silence noisy HTTP logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)

log = structlog.get_logger()

class GeminiClient:
    """
    Upgraded Gemini Client using both Models API (for thinking) and Interactions API (for tools).
    Supports built-in grounding, thinking summaries, and structured outputs.
    Supports rotating multiple API keys if GEMINI_API_KEY is a comma-separated list.
    """

    def __init__(self):
        # Support multiple keys comma separated (e.g. KEY1,KEY2,KEY3)
        raw_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not raw_key:
            log.warning("gemini.api_key_missing", msg="GEMINI_API_KEY not found in environment.")
            self.api_keys = []
        else:
            self.api_keys = [k.strip() for k in raw_key.split(",") if k.strip()]
        
        self.current_key_idx = 0
        self.provider = "gemini_api"
        self._init_client()

    def _init_client(self):
        if not self.api_keys:
            return
        key = self.api_keys[self.current_key_idx]
        self.client = genai.Client(
            api_key=key,
            http_options={"api_version": "v1beta"}
        )
        log.debug("gemini.client_initialized", key_idx=self.current_key_idx)

    def rotate_key(self):
        if len(self.api_keys) <= 1:
            return False
        self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
        self._init_client()
        log.info("gemini.key_rotated", new_idx=self.current_key_idx)
        return True

    def upload_file(self, path: str, display_name: Optional[str] = None):
        """Upload a file (audio, video, PDF) to Google's servers for processing."""
        log.info("gemini.file.uploading", path=path)
        file = self.client.files.upload(
            file=path, 
            config=types.UploadFileConfig(display_name=display_name)
        )
        log.info("gemini.file.uploaded", name=file.name, uri=file.uri)
        return file

    async def check_required_models(self, models: List[str]) -> List[str]:
        return []

    async def complete(
        self,
        model: str,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        thinking: bool = True,
        use_tools: bool = False,
        num_gpu: Optional[int] = None,
        **kwargs
    ) -> tuple[str, Optional[str], Optional[Dict]]:
        """
        Executes a completion using either the Models API (thinking) or Interactions API (tools).
        """
        actual_model_name = model
        
        # Prepare messages
        input_data = []
        
        if messages:
            for m in messages:
                if m.get("role") == "system":
                    content = m.get("content", "")
                    if isinstance(content, list):
                        content = " ".join([c.get("text", "") for c in content if c.get("type") == "text"])
                    system_prompt = content
                    continue
                content = m.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                input_data.append({"role": m["role"], "content": content})
        elif user_prompt:
            input_data.append({"role": "user", "content": [{"type": "text", "text": user_prompt}]})

        max_retries = 3
        from src.utils import dashboard
        dashboard.state.llm_model = actual_model_name
        dashboard.state.llm_thought = ""
        dashboard.state.llm_response = ""

        if use_tools:
            # --- USE INTERACTIONS API (Agent Mode for Grounding) ---
            create_params = {
                "model": actual_model_name,
                "input": input_data,
                "generation_config": types.GenerationConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
                "system_instruction": system_prompt if system_prompt else None,
                "tools": [{"type": "google_search"}]
            }

            for attempt in range(max_retries):
                try:
                    print(f"\n\033[95m[{actual_model_name} (Interactions) Starting...]\033[0m")
                    full_text = ""
                    thinking_trace = ""

                    interaction = await self.client.aio.interactions.create(**create_params)
                    while True:
                        interaction = await self.client.aio.interactions.get(interaction.id)
                        status = interaction.status
                        if status == "completed":
                            full_text = "".join([o.text for o in interaction.outputs if o.type == "text" and o.text])
                            thinking_trace = "\n".join([o.summary for o in interaction.outputs if o.type == "thought" and o.summary])
                            break
                        elif status in ["failed", "cancelled"]:
                            raise Exception(f"Agent failed with status: {status}")
                        await asyncio.sleep(2)
                    
                    print(f"\033[95m[Generation Complete]\033[0m\n")
                    return full_text, thinking_trace.strip() if thinking_trace else None, None
                except Exception as e:
                    if ("429" in str(e) or "quota" in str(e).lower()) and attempt < max_retries - 1:
                        self.rotate_key()
                        continue
                    raise e
        else:
            # --- USE STABLE MODELS API (Standard Mode for Thinking) ---
            contents = []
            for m in input_data:
                parts = []
                for c in m["content"]:
                    if c["type"] == "text":
                        parts.append(types.Part.from_text(text=c["text"]))
                    elif c["type"] in ["audio", "image", "video", "document"]:
                        parts.append(types.Part.from_uri(uri=c["uri"], mime_type=c["mime_type"]))
                contents.append(types.Content(role=m["role"], parts=parts))

            config = types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                system_instruction=types.Content(parts=[types.Part.from_text(text=system_prompt)]) if system_prompt else None,
                thinking_config=types.ThinkingConfig(include_thoughts=True) if thinking else None
            )

            for attempt in range(max_retries):
                try:
                    print(f"\n\033[95m[{actual_model_name} (Models) Starting...]\033[0m")
                    full_text = ""
                    thinking_trace = ""
                    
                    stream = await self.client.aio.models.generate_content_stream(
                        model=actual_model_name,
                        contents=contents,
                        config=config
                    )
                    
                    async for chunk in stream:
                        if not chunk.candidates: continue
                        for part in chunk.candidates[0].content.parts:
                            if hasattr(part, "thought") and part.thought:
                                # Thought text might be in part.text when thought is True
                                thought = part.text or ""
                                if not thought and hasattr(part, "thought") and isinstance(part.thought, str):
                                    thought = part.thought
                                
                                if thought:
                                    thinking_trace += thought
                                    dashboard.state.llm_thought += thought
                                    import sys
                                    sys.stdout.write(f"\033[90m{thought}\033[0m")
                                    sys.stdout.flush()
                            elif part.text:
                                text = part.text
                                full_text += text
                                dashboard.state.llm_response += text
                                import sys
                                sys.stdout.write(text)
                                sys.stdout.flush()
                    
                    print(f"\n\033[95m[Generation Complete]\033[0m\n")
                    return full_text, thinking_trace.strip() if thinking_trace else None, None
                    
                except Exception as e:
                    if ("429" in str(e) or "quota" in str(e).lower()) and attempt < max_retries - 1:
                        self.rotate_key()
                        continue
                    raise e

        return "", None, None

    async def generate(self, *args, **kwargs):
        """Minimal shim for generate method."""
        return await self.complete(*args, **kwargs)
