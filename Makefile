# AI Trading Agent - Pipeline Orchestration
# Professional Makefile for automated trading workflows.

.PHONY: help setup pull-models check-ollama run test clean clean-output

# Configuration (Defaults)
STAGE1_MODEL ?= gemma4:latest
STAGE2_MODEL ?= gemma4:latest
STAGE3_MODEL ?= qwen3:32b
STAGE5_MODEL ?= deepseek-r1:70b

PYTHON := env PYTHONPATH="" PYTHONUNBUFFERED=1 .venv/bin/python
PIP    := env PYTHONPATH="" .venv/bin/pip

# Default target
help:
	@echo "AI Trading Agent - Command Interface"
	@echo ""
	@echo "General Commands:"
	@echo "  setup                Initialize environment, venv, and dependencies."
	@echo "  pull-models          Download required LLM models from Ollama library."
	@echo "  test                 Run the full pytest suite for logic validation."
	@echo "  clean                Remove __pycache__ and temporary .pyc files."
	@echo "  clean-output         Remove research outputs. Use ARGS=\"--today\" or \"--all\"."
	@echo ""
	@echo "Execution Commands:"
	@echo "  run                  Execute the pipeline. Pass ARGS for customization."
	@echo "  rerun                Shortcut for 'make run ARGS=\"--force\"'. Resets progress."
	@echo ""
	@echo "Available ARGS for 'make run':"
	@echo "  --resume             Skip stages that already have output files for today."
	@echo "  --force              Overwrite all previous progress and re-run everything."
	@echo "  --dashboard          Open the web-based real-time monitor (Port 8080)."
	@echo "  --stage [1-5]        Run ONLY one specific stage."
	@echo "  --from-stage [1-5]   Run starting from this stage through to the end."
	@echo "  --date YYYY-MM-DD    Process a specific date instead of today."
	@echo "  --skip-podcasts      Skip audio processing (saves time/VRAM)."
	@echo "  --score-threshold X  Min score (default 6.5) to pass the Analyst stage."
	@echo "  --no-email           Skip the final report email notification."
	@echo ""
	@echo "Examples:"
	@echo "  make run ARGS=\"--resume --dashboard\"     # Typical daily run"
	@echo "  make run ARGS=\"--stage 3 --force\"        # Re-run only the Analyst"
	@echo "  make clean-output ARGS=\"--all --yes\"     # Total output reset"

# Environment Setup
setup:
	@echo "Initializing virtual environment..."
	@python3 -m venv .venv
	@echo "Installing dependencies..."
	@$(PIP) install -r requirements.txt -q
	@echo "Configuring environment files..."
	@cp -n .env.template .env || true
	@mkdir -p output
	@echo "Setup complete. Please configure .env and run 'make pull-models'."

# Model Management
pull-models: check-ollama
	@echo "Pulling Stage 1 model ($(STAGE1_MODEL))..."
	@ollama pull $(STAGE1_MODEL)
	@echo "Pulling Stage 2 model ($(STAGE2_MODEL))..."
	@ollama pull $(STAGE2_MODEL)
	@echo "Pulling Stage 3 model ($(STAGE3_MODEL))..."
	@ollama pull $(STAGE3_MODEL)
	@echo "Pulling Stage 5 model ($(STAGE5_MODEL))..."
	@ollama pull $(STAGE5_MODEL)
	@echo "Models successfully updated."

check-ollama:
	@which ollama > /dev/null || (echo "Error: Ollama not found. Install from https://ollama.com" && exit 1)
	@ollama list > /dev/null 2>&1 || (echo "Error: Ollama is not running. Start it with 'ollama serve'." && exit 1)

# Pipeline Execution
run:
	@echo "Starting pipeline execution..."
	@$(PYTHON) -m src.main $(ARGS)

rerun:
	@echo "Rerunning pipeline with force overwrite..."
	@$(PYTHON) -m src.main --force $(ARGS)

# Testing
test:
	@$(PYTHON) -m pytest tests/ -v $(ARGS)

# Cleanup
clean:
	@echo "Cleaning temporary files..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleanup complete."

clean-output:
	@$(PYTHON) -m src.utils.clean_output $(ARGS)
