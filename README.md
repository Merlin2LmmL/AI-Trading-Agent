# AI Trading Insider Processor

An automated, local-first artificial intelligence pipeline designed to systematically ingest financial media (news, podcasts, and global publications), extract actionable trading ideas, conduct deep fundamental research, and synthesize portfolio recommendations. 

**Total recurring cost: $0.00** — entirely self-hosted on your local hardware.

---

## Architecture

The pipeline is split into three distinct agentic stages:

```text
Stage 1 (Fast Information Extraction)   → Fetches media and extracts structured JSON trading ideas.
Stage 2 (Deep Reasoning & Research)     → Conducts deep fundamental research and scores each idea.
Stage 3 (Portfolio Synthesis & Report)  → Evaluates ideas against live portfolio constraints and generates a daily advisory report.
```

## Hardware Requirements & Recommendations

This system leverages large language models (LLMs) which are highly dependent on **VRAM (Video RAM)** and **System RAM**.

### Memory Guidelines
- **Barely Runs:** 0GB VRAM + 16GB System RAM. This configuration limits you to the smallest possible models and requires heavy CPU offloading.
- **Minimum Recommendation:** 8GB VRAM + 32GB System RAM. This configuration limits you to smaller, less capable reasoning models (8B-14B parameters).
- **Recommended Setup:** 16GB+ VRAM + 64GB+ System RAM. This enables you to run highly capable frontier reasoning models (like 70B parameter models) by offloading neural network layers between the GPU and System RAM.

### GPU Compatibility
- **NVIDIA GPUs:** Fully supported out-of-the-box on both Windows and Linux environments via Ollama.
- **AMD GPUs:** If using AMD hardware (e.g., Radeon RX series), **a Linux distribution (such as Ubuntu) is strongly recommended**, as AMD ROCm and Vulkan support on Linux is significantly more mature. You will likely need to explicitly enable Ollama's Vulkan backend for optimal compatibility.

---

## Setup

### 1. Install Ollama
Ollama is the local inference engine powering the AI models.
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**For AMD GPU Users on Linux:** If your GPU is not automatically detected, enable the Vulkan backend:
```bash
# Edit /etc/systemd/system/ollama.service — add to [Service]:
Environment="OLLAMA_VULKAN=1"
systemctl daemon-reload && systemctl restart ollama
```

### 2. Install Python Dependencies & Configure
Ensure you are running in a virtual environment.
```bash
make setup
```
Then edit `.env` and add your free API keys:
- **Finnhub** (free tier): https://finnhub.io/register
- **NewsAPI** (free tier): https://newsapi.org/register

### 3. Pull AI Models
By default, the pipeline is configured to use `gemma4` for fast extraction and `deepseek-r1:70b` for deep reasoning.
```bash
make pull-models
```
*Note: This will download approximately 50GB of model weights.*

### 4. (Optional) Build whisper.cpp for Podcast Transcription
```bash
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
make GGML_HIP=1          # Build with AMD GPU support (use appropriate flags for NVIDIA)
./models/download-ggml-model.sh large-v3
cd ..
```

---

## Usage

```bash
# Run the full daily pipeline
make run

# Run individual stages manually
make run-stage1   # Fetch media and extract ideas only
make run-stage2   # Research and score (requires Stage 1 output)
make run-stage3   # Portfolio synthesis (requires Stage 2 output)

# Run with custom parameters
python -m src.main --skip-podcasts        # Skips audio transcription for faster execution
python -m src.main --date 2026-04-28      # Reprocess a specific past date
python -m src.main --stage 1              # Run only Stage 1
python -m src.main --score-threshold 7.0  # Apply a stricter filter for idea passing
```

### Output Files (located in `output/YYYY-MM-DD/`)

| File | Contents |
|---|---|
| `01_raw_articles.json` | Comprehensive log of all fetched media (useful for debugging) |
| `02_ideas.json` | Extracted ideas formatted as structured `IdeaSummary` objects |
| `03_scored_ideas.json` | Detailed research, fundamental scores, and AI thinking traces |
| `04_portfolio_update.json` | Recommended portfolio adjustments |
| `daily_report.md` | The final, human-readable advisory report |

---

## Configuration

### `config/sources.yaml`
Manage your media ingestion sources. Add or remove RSS feeds, APIs, and podcast RSS links. Global sources are pre-configured.

### `config/portfolio.yaml`
Define your portfolio identity, investment thesis, active holdings, and strict risk constraints (e.g., maximum sector allocation, cash reserves).

### `config/prompts/`
Customize the specific instructions and constraints provided to the AI at each stage.

### `.env`
Your environment configuration file controls API access and model selection.
```env
FINNHUB_API_KEY=your_key
NEWSAPI_API_KEY=your_key
STAGE1_MODEL=gemma4
STAGE2_MODEL=deepseek-r1:70b
STAGE3_MODEL=deepseek-r1:70b
```

---

---

---

## Typical Runtime Estimates

Performance varies significantly based on your hardware (specifically VRAM limits and CPU offloading speeds).

| Stage | Task | Estimated Time |
|---|---|---|
| Data Ingestion | Concurrent API and RSS fetching | 2–3 min |
| Transcription | 2–3 podcast episodes (1hr each) | 15–25 min |
| Stage 1 | Information Extraction (Fast Model) | 15–25 min |
| Stage 2 | Deep Research & Scoring (Reasoning Model) | 50–75 min |
| Stage 3 | Portfolio Synthesis (Reasoning Model) | 10–15 min |
| **Total** | | **~1.5–2.5 hours** |

---

## Tuning Large Reasoning Models (Layer Offloading)

When running massive reasoning models (like a 70B parameter model) on hardware with limited VRAM (e.g., 16GB), Ollama will automatically split the model layers between your fast GPU VRAM and your slower System RAM.

To optimize inference speed, you should tune the number of layers loaded into VRAM:

```bash
# In your Ollama service config (/etc/systemd/system/ollama.service):
Environment="OLLAMA_NUM_GPU_LAYERS=20"
```

**Tuning Strategy:**
Higher layer counts equal faster performance, but setting the number too high will result in Out-Of-Memory (OOM) errors. Start with a conservative number (e.g., `20`), monitor your GPU VRAM usage during a run, and increase it incrementally until you reach your VRAM limit.

---

## Adding New Sources

**Adding an RSS feed:**
```yaml
# config/sources.yaml → rss_feeds:
- name: Defense News Insider
  url: https://example.com/feed.rss
  category: news
  language: en   # Supported: 'en', 'de'
  credibility: HIGH
```

**Adding a Podcast:**
```yaml
# config/sources.yaml → podcasts:
- name: Geopolitics Daily
  rss: https://feeds.example.com/podcast.rss
  language: en
  credibility: MEDIUM
  max_episodes_per_day: 1
```
