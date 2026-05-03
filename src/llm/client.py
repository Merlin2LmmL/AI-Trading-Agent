"""
Ollama LLM client — native ollama library (not the OpenAI /v1 shim).

GPU backend notes (RX 9070 XT — 16 GB VRAM, RDNA 4 / gfx1201):
  - ROCm 7.x (HSA) is now supported for RDNA 4. After installing ROCm via
    https://rocm.docs.amd.com/projects/install-on-linux/en/latest/install/quick-start.html
    set the following before starting Ollama:
        export HSA_OVERRIDE_GFX_VERSION=11.0.0   # only if Ollama misdetects gfx1201
        export OLLAMA_GPU_LAYERS=-1               # let Ollama decide layer count
    Previously the Vulkan/radv path was used as a fallback; with ROCm installed
    Ollama will now prefer the ROCm HIP backend, which is significantly faster.

  - VRAM budget: model weights + KV cache must fit in 16 GB.
    deepseek-r1:70b (Q4_K_M, ~40 GB) → does NOT fit on one GPU; Ollama will
    automatically split across GPU+CPU. Expect slow generation on Stage 2.
    Recommended alternative: deepseek-r1:32b (Q4_K_M, ~20 GB) → still OOM.
    Best single-GPU option: qwen3:14b or deepseek-r1:14b (Q4_K_M ~9 GB).

  - KV cache cost per token at different ctx sizes (rough estimate, fp16):
        num_ctx=2048  → ~0.5 GB
        num_ctx=4096  → ~1.0 GB
        num_ctx=8192  → ~2.0 GB
    Recommended STAGE2_NUM_CTX=4096 with a 14b model.

Why native ollama and NOT the OpenAI /v1 shim:
  - response_format: json_object is ignored by Ollama's shim for most open
    source models → causes JSON formatting errors.
  - Options (num_ctx, num_gpu) passed via extra_body may be silently dropped
    by the shim → causes VRAM overflow crashes.
  - The native library sends options directly to the Ollama runner where they
    are always honoured.

Environment variables (all optional):
    OLLAMA_BASE_URL          Ollama server URL (default: http://localhost:11434)
    OLLAMA_NUM_CTX           Default KV cache size (default: 4096)
    OLLAMA_NUM_GPU_LAYERS    Override GPU layer count for all models
    OLLAMA_NUM_GPU_LAYERS_<MODEL_TAG>  Per-model override (e.g. QWEN3_14B)
    OLLAMA_REQUEST_TIMEOUT   Seconds before a request times out (default: 600)
    OLLAMA_MAX_RETRIES       Retry attempts on transient failures (default: 3)
    OLLAMA_RETRY_DELAY       Base delay in seconds between retries (default: 5)
    HSA_OVERRIDE_GFX_VERSION Set to 11.0.0 if ROCm doesn't detect gfx1201
"""
from __future__ import annotations

import os
import time
from typing import Optional, Any

import asyncio
import ollama
import structlog

log = structlog.get_logger()


# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_NUM_CTX     = 4096
_DEFAULT_TIMEOUT     = 600   # seconds — Stage 2 with a 70b model can be slow
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 5     # seconds (doubled on each retry — exponential backoff)

# Errors that are safe to retry (transient / recoverable)
_RETRYABLE_ERRORS = (
    "connection refused",
    "connection reset",
    "timeout",
    "eof",
    "broken pipe",
    "temporarily unavailable",
    "resource temporarily unavailable",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_qwen3(model: str) -> bool:
    """Qwen3 uses /think prefix to activate chain-of-thought reasoning."""
    return "qwen3" in model.lower()


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception looks like a transient network/server error."""
    msg = str(exc).lower()
    return any(token in msg for token in _RETRYABLE_ERRORS)


def _build_options(model: str, temperature: float, num_ctx: int | None, num_gpu: int | None = None) -> dict:
    """
    Build the Ollama options dict passed directly to the llama.cpp / ROCm runner.
    All keys here bypass the /v1 shim — no silent drops.
    """
    resolved_ctx = num_ctx or int(os.getenv("OLLAMA_NUM_CTX", str(_DEFAULT_NUM_CTX)))

    opts: dict = {
        "temperature":    temperature,
        "num_ctx":        resolved_ctx,
        "repeat_penalty": 1.5,  # Stricter penalty for gemma4
        "stop":           ["\n\n\n", "```\n", "}]"], # Force end of JSON/blocks
    }

    # 1. Explicit parameter (e.g. from Stage settings)
    if num_gpu is not None:
        opts["num_gpu"] = num_gpu
    else:
        # 2. Per-model GPU layer override, e.g. OLLAMA_NUM_GPU_LAYERS_QWEN3_14B=40
        tag     = model.replace(":", "_").replace(".", "_").replace("-", "_").upper()
        specific = os.getenv(f"OLLAMA_NUM_GPU_LAYERS_{tag}")
        global_v = os.getenv("OLLAMA_NUM_GPU_LAYERS")
        raw      = specific if specific is not None else global_v
        if raw is not None:
            opts["num_gpu"] = int(raw)

    return opts


def _get_vram_gb() -> int:
    """
    Detect total VRAM in GB using:
    1. OLLAMA_VRAM_GB environment variable (override)
    2. rocm-smi (AMD)
    3. nvidia-smi (NVIDIA)
    Returns 0 if all detection methods fail.
    """
    import subprocess
    import re

    # 1. Manual override
    env_val = os.getenv("OLLAMA_VRAM_GB")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass

    # 2. Try AMD (rocm-smi)
    try:
        res = subprocess.check_output(["rocm-smi", "--showmeminfo", "vram"], 
                                    stderr=subprocess.STDOUT, encoding="utf-8")
        match = re.search(r"VRAM Total Memory \(B\):\s*(\d+)", res)
        if match:
            return (int(match.group(1)) // (1024**3)) + 1
    except Exception:
        pass

    # 3. Try NVIDIA (nvidia-smi)
    try:
        res = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], 
                                    stderr=subprocess.STDOUT, encoding="utf-8")
        match = re.search(r"(\d+)", res)
        if match:
            return (int(match.group(1)) // 1024) + 1
    except Exception:
        pass

    return 0  # Signal failure


def _strip_thinking_tag(text: str) -> str:
    """Remove <think>…</think> wrapper so callers get clean content."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ── Main client ───────────────────────────────────────────────────────────────

class OllamaClient:
    """
    Thin, crash-resistant wrapper around the native ollama Python library.

    Key improvements over the original:
      - Fully ASYNCHRONOUS: uses ollama.AsyncClient to prevent blocking the event loop.
      - Exponential-backoff retry on transient failures.
      - Configurable request timeout.
      - Explicit VRAM / ctx warnings logged before large requests.
    """

    def __init__(self, base_url: Optional[str] = None):
        self.base_url    = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.timeout     = int(os.getenv("OLLAMA_REQUEST_TIMEOUT", str(_DEFAULT_TIMEOUT)))
        self.max_retries = int(os.getenv("OLLAMA_MAX_RETRIES",     str(_DEFAULT_MAX_RETRIES)))
        self.retry_delay = float(os.getenv("OLLAMA_RETRY_DELAY",   str(_DEFAULT_RETRY_DELAY)))

        self._client = ollama.AsyncClient(host=self.base_url)
        self.vram_gb = _get_vram_gb()
        
        log.debug("llm.hw_detected", vram_gb=self.vram_gb)

        # Warn once at startup if ROCm gfx override is not set
        if not os.getenv("HSA_OVERRIDE_GFX_VERSION"):
            log.debug(
                "rocm.gfx_override_not_set",
                hint=(
                    "If Ollama fails to detect your RX 9070 XT, set "
                    "HSA_OVERRIDE_GFX_VERSION=11.0.0 before starting Ollama"
                ),
            )

    def build_chatml_prompt(self, messages: list[dict]) -> str:
        """
        Construct a ChatML string for Qwen/DeepSeek models.
        """
        prompt = ""
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"
        prompt += "<|im_start|>assistant\n"
        return prompt

    # ── Core completion ───────────────────────────────────────────────────────

    async def complete(
        self,
        model: str,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        thinking: bool = False,
        require_json: bool = False,
        num_ctx: int | None = None,
        num_gpu: int | None = None,
        stop_on_search: bool = False,
        context: Optional[list[int]] = None,
        use_tools: bool = False,
        **kwargs
    ) -> tuple[str, Optional[str], Optional[str], Optional[list[int]]]:
        """
        Send a completion request via the native ollama library (ASYNC).
        Returns: (final_response, thinking_trace, search_query, context)
        """
        if messages:
            actual_messages = messages
        else:
            actual_user_prompt = user_prompt
            if thinking and _is_qwen3(model):
                actual_user_prompt = f"/think\n\n{user_prompt}"

            actual_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": actual_user_prompt},
            ]

        options = _build_options(model, temperature, num_ctx, num_gpu)
        options["num_predict"] = max_tokens

        ctx_val = options["num_ctx"]
        
        if self.vram_gb <= 0:
            # Detection failed and no override set
            log.warning(
                "vram.detection_failed",
                num_ctx=ctx_val,
                hint="Could not detect GPU VRAM. Smart Context limits not enabled."
            )
            vram_threshold = 4096 # Safe fallback
        else:
            # Rule of thumb: ~384 context tokens per GB is "safe" for 14b-32b models
            vram_threshold = self.vram_gb * 384
        
        if ctx_val > vram_threshold:
            log.warning(
                "vram.ctx_large",
                num_ctx=ctx_val,
                model=model,
                vram_gb=self.vram_gb,
                hint=(
                    f"ctx > {vram_threshold} may overflow VRAM on your {self.vram_gb}GB GPU. "
                    "Consider reducing OLLAMA_NUM_CTX."
                )
            )

        log.info(
            "llm.request",
            model=model,
            thinking=thinking,
            require_json=require_json,
            num_ctx=ctx_val,
            prompt_chars=sum(len(m.get("content", "")) for m in actual_messages),
        )

        kwargs: dict = {
            "model":    model,
            "messages": actual_messages,
            "options":  options,
            "stream":   True,
        }
        if require_json:
            # CRITICAL: For DeepSeek-R1 and Qwen3 models, Ollama's native JSON mode
            # often STRIPS the <think> trace or thinking field from the output.
            # If we want thinking, we disable native JSON mode and rely on our 
            # bracket-matching parser in structured.py.
            is_reasoning_model = "deepseek-r1" in model.lower() or "qwen3" in model.lower()
            if not (thinking and is_reasoning_model):
                kwargs["format"] = "json"
            else:
                log.debug("llm.native_json_disabled_for_thinking", model=model)

        # ── Retry loop ────────────────────────────────────────────────────────
        last_exc: Exception | None = None
        delay = self.retry_delay
        
        raw_text = ""
        thinking_trace: Optional[str] = None
        search_query: Optional[str] = None
        elapsed = 0.0

        for attempt in range(1, self.max_retries + 1):
            try:
                start    = time.time()
                import sys
                from src.utils import dashboard
                
                dashboard.state.llm_model = model
                dashboard.state.llm_thought = ""
                dashboard.state.llm_response = ""
                
                print(f"\n\033[96m[{model} Generating...]\033[0m")
                
                response_stream = await self._client.chat(**kwargs)
                
                raw_text_chunks = []
                import re
                
                # Regex for [SEARCH: "query"] or [SEARCH: query]
                SEARCH_REGEX = r"\[SEARCH:\s*['\"]?(.*?)['\"]?\s*\]"
                
                async for chunk in response_stream:
                    # In some versions of ollama-python, chunk is a dict; in others an object
                    msg = getattr(chunk, 'message', None) or chunk.get('message', {})
                    content = getattr(msg, 'content', '') or msg.get('content', '') or ""

                    # ── Native Ollama thinking field (Qwen3, DeepSeek-R1 ≥ Ollama 0.6) ──
                    # Models can return thinking in 'thinking' or 'thought' fields.
                    native_thinking = (
                        getattr(msg, 'thinking', None) or 
                        getattr(msg, 'thought', None) or 
                        (msg.get('thinking') if isinstance(msg, dict) else None) or
                        (msg.get('thought') if isinstance(msg, dict) else None)
                    )

                    if native_thinking:
                        dashboard.state.llm_thought += native_thinking
                        sys.stdout.write(f"\033[90m{native_thinking}\033[0m")
                        sys.stdout.flush()
                        
                        # Check for [SEARCH: "query"] in thinking
                        if stop_on_search:
                            match = re.search(SEARCH_REGEX, dashboard.state.llm_thought)
                            if match:
                                search_query = match.group(1).strip()
                                log.info("llm.search_tag_detected", query=search_query)
                                break

                    if content:
                        sys.stdout.write(content)
                        sys.stdout.flush()
                        raw_text_chunks.append(content)

                        current_full_text = "".join(raw_text_chunks)

                        if "<think>" in current_full_text:
                            # ── <think> tag approach (models that embed thinking in content) ──
                            parts = current_full_text.split("<think>", 1)
                            preamble = parts[0].strip()
                            rest = parts[1]

                            if "</think>" in rest:
                                # Thinking finished — split thought and real response
                                thought_part, after = rest.split("</think>", 1)
                                dashboard.state.llm_thought = thought_part.strip()
                                response_text = (preamble + "\n" + after).strip() if preamble else after.strip()
                                dashboard.state.llm_response = response_text
                            else:
                                # Still inside <think> — stream thought live
                                dashboard.state.llm_thought = rest
                                if preamble:
                                    dashboard.state.llm_response = preamble
                                else:
                                    dashboard.state.llm_response = "Thinking..."
                            
                            # Check for [SEARCH: "query"] in thinking tag
                            if stop_on_search:
                                match = re.search(SEARCH_REGEX, dashboard.state.llm_thought)
                                if match:
                                    search_query = match.group(1).strip()
                                    log.info("llm.search_tag_detected", query=search_query)
                                    break

                        elif not native_thinking:
                            # Plain response — no thinking involved or thinking already ended
                            dashboard.state.llm_response = current_full_text.strip()
                            
                            # If model doesn't use <think> tags but we want to catch a search tag in main content
                            if stop_on_search:
                                match = re.search(SEARCH_REGEX, dashboard.state.llm_response)
                                if match:
                                    search_query = match.group(1).strip()
                                    log.info("llm.search_tag_detected_in_content", query=search_query)
                                    break
                
                if search_query:
                    print(f"\n\033[93m[Search Triggered: {search_query}]\033[0m")
                else:
                    print("\n\033[96m[Generation Complete]\033[0m")
                
                raw_text = "".join(raw_text_chunks)
                elapsed  = time.time() - start
                
                # Consolidate thinking trace for the return value
                thinking_trace = None
                if thinking:
                    # Priority 1: Extract from tags in the raw text
                    match = re.search(r"<think>(.*?)</think>", raw_text, re.DOTALL)
                    if match:
                        thinking_trace = match.group(1).strip()
                    # Priority 2: Use the dashboard state if it was updated via msg.thought
                    elif dashboard.state.llm_thought:
                        thinking_trace = dashboard.state.llm_thought.strip()
                
                break  # success

            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                if attempt >= self.max_retries:
                    log.error("llm.max_retries_exceeded", model=model, error=str(exc))
                    raise
                log.warning("llm.retry", model=model, attempt=attempt, error=str(exc), retry_in_s=delay)
                await asyncio.sleep(delay)
                delay *= 2

        log.info(
            "llm.response",
            model=model,
            elapsed_s=round(elapsed, 1),
            output_chars=len(raw_text),
        )

        clean_text = _strip_thinking_tag(raw_text)
        return clean_text, thinking_trace, search_query

    async def generate(
        self,
        model: str,
        prompt: str,
        system: Optional[str] = None,
        template: Optional[str] = None,
        context: Optional[list[int]] = None,
        options: Optional[dict] = None,
        raw: bool = False,
        stop_on_search: bool = False,
        format: Optional[str] = None,
    ) -> tuple[str, Optional[list[int]], Optional[str]]:
        """
        Low-level generation API for 'True Resumption' or specific prompt engineering.
        Returns: (text, context, search_query)
        """
        kwargs = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "raw": raw,
        }
        if system: kwargs["system"] = system
        if template: kwargs["template"] = template
        if context: kwargs["context"] = context
        if options: kwargs["options"] = options
        if format: kwargs["format"] = format

        log.info("llm.generate", model=model, prompt_chars=len(prompt), raw=raw, has_context=context is not None, format=format)

        full_text = ""
        new_context = []
        search_query = None
        
        from src.utils import dashboard
        dashboard.state.llm_model = model
        import re
        SEARCH_REGEX = r"\[SEARCH:\s*['\"]?(.*?)['\"]?\s*\]"
        
        # Determine if we should print in grey (thinking) or standard (content)
        # For raw generation starting with <think>, we start in grey.
        is_thinking = prompt.strip().endswith("<think>") or "<think>" in prompt
        
        try:
            print(f"\n\033[96m[{model} Generating...]\033[0m")
            response_stream = await self._client.generate(**kwargs)
            async for chunk in response_stream:
                content = chunk.get("response", "")
                if content:
                    full_text += content
                    
                    # Console output with coloring
                    if is_thinking:
                        sys.stdout.write(f"\033[90m{content}\033[0m")
                    else:
                        sys.stdout.write(content)
                    sys.stdout.flush()
                    
                    # Update dashboard
                    if "<think>" in full_text:
                        parts = full_text.split("<think>", 1)
                        rest = parts[1]
                        if "</think>" in rest:
                            thought, after = rest.split("</think>", 1)
                            dashboard.state.llm_thought = thought.strip()
                            dashboard.state.llm_response = after.strip()
                            is_thinking = False # Switched to content
                        else:
                            dashboard.state.llm_thought = rest
                    else:
                        dashboard.state.llm_response = full_text.strip()

                    # Detect search
                    if stop_on_search:
                        match = re.search(SEARCH_REGEX, full_text)
                        if match:
                            search_query = match.group(1).strip()
                            log.info("llm.generate.search_tag_detected", query=search_query)
                            break
                
                if chunk.get("done"):
                    new_context = chunk.get("context", [])
            
            return full_text, new_context, search_query

        except Exception as e:
            log.error("llm.generate_error", error=str(e))
            raise

    # ── Model availability ────────────────────────────────────────────────────

    async def is_model_available(self, model: str) -> bool:
        """Return True if the model is pulled and available in Ollama."""
        try:
            models    = await self._client.list()
            available = [m.model for m in models.models]
            return model in available
        except Exception:
            return False

    async def check_required_models(self, models: list[str]) -> list[str]:
        """Return the subset of models that have NOT been pulled yet."""
        try:
            available_list = await self._client.list()
            available_ids  = {m.model for m in available_list.models}
            return [m for m in models if m not in available_ids]
        except Exception as e:
            log.warning("ollama.check_failed", error=str(e))
            return models

    async def health_check(self) -> bool:
        """Ping the Ollama server."""
        try:
            await self._client.list()
            return True
        except Exception as e:
            log.error("ollama.unreachable", url=self.base_url, error=str(e))
            return False


# ── Shared singleton ──────────────────────────────────────────────────────────

_client: Optional[Any] = None


def get_client() -> Any:
    """
    Factory function to get the configured LLM client.
    Supports 'local' (Ollama) and 'api' (Gemini).
    """
    global _client
    if _client is not None:
        return _client

    provider = os.getenv("LLM_PROVIDER", "local").lower()
    
    if provider == "api":
        from src.llm.gemini import GeminiClient
        log.info("llm.factory", provider="gemini_api")
        _client = GeminiClient()
    else:
        log.info("llm.factory", provider="ollama_local")
        _client = OllamaClient()
        
    return _client