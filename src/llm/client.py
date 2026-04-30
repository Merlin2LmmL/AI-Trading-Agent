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
from typing import Optional

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

        # Warn once at startup if ROCm gfx override is not set
        if not os.getenv("HSA_OVERRIDE_GFX_VERSION"):
            log.debug(
                "rocm.gfx_override_not_set",
                hint=(
                    "If Ollama fails to detect your RX 9070 XT, set "
                    "HSA_OVERRIDE_GFX_VERSION=11.0.0 before starting Ollama"
                ),
            )

    # ── Core completion ───────────────────────────────────────────────────────

    async def complete(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        thinking: bool = False,
        require_json: bool = False,
        num_ctx: int | None = None,
        num_gpu: int | None = None,
    ) -> tuple[str, Optional[str]]:
        """
        Send a completion request via the native ollama library (ASYNC).
        """
        actual_user_prompt = user_prompt
        if thinking and _is_qwen3(model):
            actual_user_prompt = f"/think\n\n{user_prompt}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": actual_user_prompt},
        ]

        options = _build_options(model, temperature, num_ctx, num_gpu)
        options["num_predict"] = max_tokens

        ctx_val = options["num_ctx"]
        if ctx_val > 6144:
            log.warning(
                "vram.ctx_large",
                num_ctx=ctx_val,
                model=model,
                hint="ctx > 6144 may overflow VRAM on a 16 GB GPU. Set OLLAMA_NUM_CTX=4096."
            )

        log.info(
            "llm.request",
            model=model,
            thinking=thinking,
            require_json=require_json,
            num_ctx=ctx_val,
            prompt_chars=len(user_prompt),
        )

        kwargs: dict = {
            "model":    model,
            "messages": messages,
            "options":  options,
            "stream":   True,
        }
        if require_json:
            kwargs["format"] = "json"

        # ── Retry loop ────────────────────────────────────────────────────────
        last_exc: Exception | None = None
        delay = self.retry_delay
        
        raw_text = ""
        elapsed = 0.0

        for attempt in range(1, self.max_retries + 1):
            try:
                start    = time.time()
                import sys
                print(f"\n\033[96m[{model} Generating...]\033[0m")
                
                response_stream = await self._client.chat(**kwargs)
                
                raw_text_chunks = []
                async for chunk in response_stream:
                    content = chunk.message.content
                    if content:
                        sys.stdout.write(content)
                        sys.stdout.flush()
                        raw_text_chunks.append(content)
                
                print("\n\033[96m[Generation Complete]\033[0m")
                raw_text = "".join(raw_text_chunks)
                elapsed  = time.time() - start
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

        thinking_trace: Optional[str] = None
        if thinking:
            import re
            match = re.search(r"<think>(.*?)</think>", raw_text, re.DOTALL)
            thinking_trace = match.group(1).strip() if match else None

        clean_text = _strip_thinking_tag(raw_text)
        return clean_text, thinking_trace

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

_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client