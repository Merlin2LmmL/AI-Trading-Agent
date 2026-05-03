# Autonomous Trading Intelligence Pipeline

A high-performance, local-first intelligence architecture designed for the systematic ingestion of global financial media, fundamental research, and portfolio strategy synthesis.

---

## Executive Summary

The Autonomous Trading Intelligence Pipeline provides a robust framework for processing unstructured financial data into actionable investment intelligence. By leveraging state-of-the-art local Large Language Models (LLMs), the system eliminates recurring API costs while maintaining strict data privacy and sovereignty.

## System Architecture

The pipeline is engineered as a four-stage agentic workflow, transitioning from high-volume data acquisition to refined strategic decision-making.

### Agentic Cognitive Architecture

The system operates on a continuous cognitive cycle:
1.  **Ingestion & Perception**: Monitoring of feeds to identify emerging market signals.
2.  **Research Planning (The Librarian)**: Identification of critical information gaps and generation of targeted search queries to fill them.
3.  **Autonomous Reasoning (The Analyst)**: Parallel execution of research plans followed by deep fundamental and geopolitical analysis.
4.  **Strategic Synthesis**: Evaluation of intelligence against specific portfolio constraints and risk parameters.

### Operational Stages

#### Stage 1: Media Ingestion and Feature Extraction
*   **Data Sources**: RSS feeds, News APIs, and Audio Podcasts.
*   **Intelligence Layer**: Efficient models extract structured entities, identifying relevant tickers, sentiment, and core theses.

#### Stage 2: Research Planning (The Librarian)
*   **Gap Analysis**: The "Librarian" persona identifies what data is missing from the initial extract (e.g., specific earnings call details).
*   **Strategy**: Generates 3-5 surgical search queries to gather missing context.

#### Stage 3: Analytical Reasoning (The Analyst)
*   **Parallel Research**: Executes the Librarian's queries simultaneously.
*   **Deep Analysis**: High-parameter reasoning models (e.g., DeepSeek-R1) synthesize the new data into a comprehensive research report with scores.

#### Stage 4: Portfolio Synthesis and Reporting
*   **Constraint Matching**: Cross-references intelligence with active holdings and risk management parameters.
*   **Deliverables**: Generation of daily advisory reports and structured portfolio updates.

---

## Hardware and Model Optimization

| Configuration Tier | Hardware | Extraction (S1) | Planning (S2) | Reasoning (S3) | Portfolio (S4) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Minimum** | 12GB VRAM | Gemma-9B | Gemma-9B | Qwen-14B | Qwen-14B |
| **Professional** | 24GB+ VRAM | Qwen-32B | Qwen-32B | **R1-32B** | **R1-32B** |
| **Enterprise** | 48GB+ VRAM | Qwen-72B | Qwen-72B | **R1-70B** | **R1-70B** |

---

## Execution and Parameters

| Flag | Description |
| :--- | :--- |
| `--stage [1,2,3,4]` | Executes only the specified stage of the pipeline. |
| `--date YYYY-MM-DD` | Processes data for a specific historical date. |
| `--skip-podcasts` | Disables audio transcription. |
| `--score-threshold X.X` | Minimum threshold for signals to pass Stage 3 (Default: 6.5). |
| `--resume` | Resumes execution from the next available stage. |
| `--force` | Overwrites existing output. |
| `--dashboard` | Activates a real-time monitoring dashboard on port 8080. |

### Usage Example
```bash
make run ARGS="--resume --dashboard"
```

---

## Legal Disclaimer
This software is provided for research and educational purposes. It does not constitute financial advice. Automated trading involves significant capital risk.
