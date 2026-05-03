# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Command Reference

| Purpose | Make Target | Typical usage |
|---------|-------------|---------------|
| Initialise environment | `setup` | `make setup` – creates a virtual env, installs dependencies, copies `.env.template`. |
| Pull LLM models | `pull-models` | `make pull-models` – downloads the models specified in `Makefile`. |
| Run full pipeline | `run` | `make run ARGS="--resume --dashboard"` – daily run, auto‑resume, shows dashboard on port 8080. |
| Re‑run a single stage | `rerun` | `make rerun ARGS="--stage 3 --force"` – forces a full re‑execution of stage 3. |
| Run tests | `test` | `make test` – executes the pytest suite. |
| Clean temporary files | `clean` | `make clean` – removes `__pycache__` and `.pyc`. |
| Remove pipeline outputs | `clean-output` | `make clean-output ARGS="--all --yes"` – delete all output JSON. |

### ARGS for `make run`
`--resume` – skip already‑generated outputs for the current day.
`--force` – overwrite existing outputs.
`--stage N` – run only stage N (1‑4).
`--date YYYY‑MM‑DD` – process a historical date.
`--skip-podcasts` – disable audio transcription.
`--score-threshold X` – minimum analyst score to pass Stage 3.
`--no-email` – skip the final report email.
`--dashboard` – launch the live monitoring UI.

## High‑Level Architecture

The repository implements a four‑stage agentic pipeline for autonomous trading intelligence.

```
src/
├─ llm/            # LLM client abstraction (supports local Ollama or API)
├─ stages/         # Each stage is a separate module with a `run` coroutine
│  ├─ stage1_ingest.py   # Media ingestion + entity extraction
│  ├─ stage2_plan.py     # Research planning (Librarian persona)
│  ├─ stage3_reason.py   # Deep reasoning (Analyst persona)
│  └─ stage4_portfolio.py # Portfolio synthesis & reporting
├─ fetchers/       # Helpers to pull data (RSS, news APIs, podcasts, Wikifolio)
├─ utils/          # Logging, dashboard, email notifier, data storage helpers
└─ main.py         # Orchestrator that parses args and sequentially invokes stages
```

*Pipeline flow* – `src.main` parses CLI args, resolves model names, checks that required LLMs are available, and then runs the selected stages in order. Each stage writes a JSON file under `output/<date>/` (e.g. `02_ideas.json`). Subsequent stages read those files.

*Data source configuration* – `config/sources.yaml` lists RSS feeds and API endpoints. Portfolio constraints live in `config/portfolio.yaml`.

*Environment variables* – API keys are stored in `.env` and `secrets.env`. The project uses `python-dotenv` to load them.

*LLM handling* – The `LLM_PROVIDER` env var determines whether to use local Ollama models or the Gemini API. The Makefile defaults to pulling local models.

## Useful Tips for Developers

* Run `make test` to catch regressions.
* If you modify any stage, add the new stage’s logic to `src/main.py`’s `run_pipeline` function.
* To quickly inspect the output of a stage, look in `output/<date>/`. Each file contains a list of objects in JSON format.
* Use the built‑in dashboard (`--dashboard`) to monitor progress live.
* When adding new data fetchers, update `config/sources.yaml` and ensure the new fetcher returns data in the format expected by the stage it feeds.

---

*Note: All commands are executed from the repository root.*
