# Autonomous Trading Intelligence Pipeline

A high-performance, local-first intelligence architecture designed for the systematic ingestion of global financial media, fundamental research, and portfolio strategy synthesis.

---

## Executive Summary

The Autonomous Trading Intelligence Pipeline provides a robust framework for processing unstructured financial data into actionable investment intelligence. By leveraging state-of-the-art local Large Language Models (LLMs), the system eliminates recurring API costs while maintaining strict data privacy and sovereignty.

## System Architecture

The pipeline is engineered as a three-stage agentic workflow, transitioning from high-volume data acquisition to refined strategic decision-making.

### Agentic Cognitive Architecture

The system operates on a continuous cognitive cycle:
1.  **Ingestion & Perception**: Monitoring of hundreds of RSS feeds, podcast transcripts, and financial APIs to identify emerging market signals.
2.  **Autonomous Reasoning**: Upon signal identification, the system invokes high-parameter reasoning models to conduct a fundamental analysis, incorporating historical context and risk assessment.
3.  **Strategic Synthesis**: The final stage evaluates intelligence against specific portfolio constraints and risk parameters to generate optimized recommendations.

### Operational Stages

#### Stage 1: Media Ingestion and Feature Extraction
*   **Data Sources**: Concurrent ingestion of RSS feeds, News APIs, and Audio Podcasts.
*   **Audio Processing**: High-fidelity transcription via GPU-accelerated Whisper.cpp.
*   **Intelligence Layer**: Efficient models (e.g., Qwen-32B) extract structured entities, identifying relevant tickers, sentiment, and core theses.

#### Stage 2: Fundamental Research and Scoring
*   **Deep Analysis**: For every identified signal, the system executes a multi-dimensional fundamental research protocol.
*   **Reasoning Layer**: Frontier reasoning models (e.g., DeepSeek-R1 70B) utilize Chain-of-Thought (CoT) processing to score ideas based on fundamental strength and news urgency.
*   **Audit Trail**: The system persists full reasoning traces for transparency and debugging.

#### Stage 3: Portfolio Synthesis and Reporting
*   **Constraint Matching**: Intelligence is cross-referenced with active holdings and risk management parameters.
*   **Optimization**: Automated calculation of position sizing and sector concentration checks.
*   **Deliverables**: Generation of comprehensive daily advisory reports and structured portfolio update files.

---

## Hardware and Model Optimization

System performance is directly correlated with available Video RAM (VRAM) and System RAM. The following tiers represent optimized configurations for local inference.

| Configuration Tier | Hardware Specifications | Extraction Model (Stage 1) | Reasoning Model (Stage 2/3) |
| :--- | :--- | :--- | :--- |
| **Minimum** | 8GB VRAM / 32GB RAM | Gemma-9B | Mistral-7B |
| **Standard** | 12GB VRAM / 48GB RAM | Qwen-14B | DeepSeek-V3-16B |
| **Professional** | 16GB+ VRAM / 64GB+ RAM | Qwen-32B | **DeepSeek-R1-70B** (Quantized) |
| **Enterprise** | Multi-GPU (48GB+ VRAM) | Qwen-72B | **DeepSeek-R1-70B** (Full Weights) |

### GPU Acceleration Notes
*   **NVIDIA Systems**: Full CUDA support via Ollama.
*   **AMD Systems**: Optimized for ROCm and Vulkan on Linux environments. Enabling `OLLAMA_VULKAN=1` is recommended for maximum throughput.

---

## Installation and Configuration

### 1. Inference Engine
Install Ollama and initialize the required models:
```bash
make pull-models
```

### 2. Environment Configuration
Initialize the environment and configure API credentials:
```bash
make setup
cp .env.template .env
```
Ensure `FINNHUB_API_KEY` and `NEWSAPI_API_KEY` are correctly defined in the `.env` file.

### 3. Audio Intelligence (Optional)
To enable podcast processing, build the Whisper.cpp binary for your specific architecture:
```bash
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
make GGML_HIP=1 # AMD ROCm
# or
make GGML_CUDA=1 # NVIDIA CUDA
```

---

## Execution and Parameters

The pipeline is managed through a comprehensive Command Line Interface (CLI).

| Flag | Description |
| :--- | :--- |
| `--stage [1,2,3]` | Executes only the specified stage of the pipeline. |
| `--date YYYY-MM-DD` | Processes data for a specific historical date. |
| `--skip-podcasts` | Disables audio transcription for accelerated execution. |
| `--score-threshold X.X` | Minimum threshold for signals to pass Stage 2 (Default: 6.5). |
| `--resume` | Resumes execution from Stage 2 if Stage 1 data is present. |
| `--force` | Overwrites existing output and bypasses deduplication logic. |
| `--dashboard` | Activates a real-time monitoring dashboard on port 8080. |

### Usage Example
```bash
python -m src.main --resume --score-threshold 7.5 --dashboard
```

---

## Project Structure

```text
├── config/             # YAML configurations and LLM instruction sets
├── output/             # Persistent storage for reports and data traces
├── src/
│   ├── fetchers/       # Ingestion modules for RSS, NewsAPI, and Media
│   ├── stages/         # Core pipeline logic and stage management
│   ├── llm/            # Inference client and prompt engineering
│   └── main.py         # Orchestration entry point
└── Makefile            # Standardized build and execution commands
```

---

## Legal Disclaimer
This software is provided for research and educational purposes. It does not constitute financial advice. Automated trading involves significant capital risk. Users should independently verify all AI-generated signals with primary data sources.
