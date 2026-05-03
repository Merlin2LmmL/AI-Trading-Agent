"""
Podcast Transcription & Signal Extraction Utility.
Leverages Gemini's multimodal capabilities to 'listen' to audio and extract trade signals.
"""
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import structlog
from dotenv import load_dotenv

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from src.llm.gemini import GeminiClient
from src.data.storage import save_json

log = structlog.get_logger()

async def transcribe_podcast(audio_path: str, output_name: Optional[str] = None):
    """
    Uploads an audio file and transcribes it using the selected provider.
    """
    # Ensure we load the .env from the project root
    env_path = Path(__file__).parent.parent.parent / ".env"
    load_dotenv(dotenv_path=env_path)
    
    provider = os.getenv("TRANSCRIPTION_PROVIDER", "google").lower()
    print(f"\033[95m[Transcription Engine Selected: {provider.upper()}]\033[0m")
    
    if not os.path.exists(audio_path):
        log.error("transcribe.file_not_found", path=audio_path)
        return

    if provider == "google":
        await _transcribe_google(audio_path, output_name)
    else:
        await _transcribe_whisper(audio_path, output_name)

async def _transcribe_google(audio_path: str, output_name: Optional[str] = None):
    """Native Gemini Multimodal Transcription."""
    client = GeminiClient()
    print(f"\033[94m[Using Google Multimodal: {audio_path}...]\033[0m")
    
    g_file = client.upload_file(audio_path, display_name=Path(audio_path).name)
    
    import time
    print(f"\033[94m[Processing on Google Servers...]\033[0m")
    while True:
        file_status = client.client.files.get(name=g_file.name)
        if file_status.state.name == "ACTIVE":
            break
        elif file_status.state.name == "FAILED":
            log.error("transcribe.upload_failed")
            return
        await asyncio.sleep(2)

    system_prompt = Path("config/prompts/stage1_extract.md").read_text()
    user_prompt = "Perform Signal Intelligence. Extract trading ideas as JSON."
    model = os.getenv("API_STAGE1_MODEL", "gemini-2.0-flash")
    
    raw_response, _, _ = await client.complete(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        files=[g_file],
        thinking=True,
        require_json=True
    )
    _save_signals(raw_response, output_name)

async def _transcribe_whisper(audio_path: str, output_name: Optional[str] = None):
    """Local Whisper Transcription."""
    print(f"\033[94m[Using Local Whisper: {audio_path}...]\033[0m")
    try:
        import whisper
        model_size = os.getenv("WHISPER_MODEL_SIZE", "base")
        print(f"\033[94m[Loading Whisper Model: {model_size}...]\033[0m")
        model = whisper.load_model(model_size)
        
        result = model.transcribe(audio_path)
        text = result["text"]
        
        # Now we need an LLM to extract signals from the text
        from src.llm.client import get_client
        llm = get_client()
        system_prompt = Path("config/prompts/stage1_extract.md").read_text()
        
        print(f"\033[95m[Whisper done. LLM is extracting signals from text...]\033[0m")
        raw_response, _, _ = await llm.complete(
            model=os.getenv("STAGE1_MODEL", "gemini-3.1-flash-lite-preview"),
            system_prompt=system_prompt,
            user_prompt=f"Extract trade signals from this transcript:\n\n{text}",
            require_json=True
        )
        _save_signals(raw_response, output_name)
        
    except ImportError:
        log.error("transcribe.whisper_missing", msg="Please install 'openai-whisper' to use local transcription.")
        print("\033[91m[Error] openai-whisper not installed. Run: pip install openai-whisper\033[0m")

def _save_signals(raw_response: str, output_name: Optional[str]):
    from src.llm.structured import extract_json_from_response
    import json
    import time
    
    cleaned = extract_json_from_response(raw_response)
    if cleaned:
        try:
            signals = json.loads(cleaned)
            filename = output_name or f"podcast_signals_{int(time.time())}.json"
            save_json(signals, filename, "podcasts")
            print(f"\n\033[92m[Success] Extracted {len(signals)} signals to output/podcasts/{filename}\033[0m")
        except Exception as e:
            log.error("transcribe.json_error", error=str(e))
    else:
        print("\n\033[91m[Error] Failed to extract JSON.\033[0m")
        print(raw_response)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/utils/transcribe.py <path_to_audio_file>")
        sys.exit(1)
    
    asyncio.run(transcribe_podcast(sys.argv[1]))
