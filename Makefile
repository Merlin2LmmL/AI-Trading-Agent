# AI Trading Agent - Pipeline Orchestration
# Professional Makefile for automated trading workflows.

.PHONY: help setup pull-models check-ollama run test clean

# Configuration
PYTHON := .venv/bin/python
PIP    := .venv/bin/pip
STAGE1_MODEL ?= gemma4
STAGE2_MODEL ?= qwen3:32b

# Default target
help:
	@echo "AI Trading Agent - Command Interface"
	@echo ""
	@echo "Available commands:"
	@echo "  setup                Initialize environment and install dependencies"
	@echo "  pull-models          Download required LLM models via Ollama"
	@echo "  run                  Execute the full pipeline (use ARGS for parameters)"
	@echo "  rerun                Execute the pipeline with --force (ignores previous progress)"
	@echo "  test                 Run the test suite"
	@echo "  clean                Remove temporary files and caches"
	@echo ""
	@echo "Examples:"
	@echo "  make run ARGS=\"--dashboard --resume\""
	@echo "  make run ARGS=\"--stage 1 --skip-podcasts\""

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
	@echo "Pulling Stage 2+3 model ($(STAGE2_MODEL))..."
	@ollama pull $(STAGE2_MODEL)
	@echo "Models successfully updated."

check-ollama:
	@which ollama > /dev/null || (echo "Error: Ollama not found. Install from https://ollama.com" && exit 1)
	@ollama list > /dev/null 2>&1 || (echo "Error: Ollama is not running. Start it with 'ollama serve'." && exit 1)

# Pipeline Execution
# Use ARGS to pass multiple parameters, e.g., make run ARGS="--dashboard --resume"
run:
	@echo "Starting pipeline execution..."
	@$(PYTHON) -m src.main $(ARGS)

# Shortcut for rerunning the entire pipeline for today
rerun:
	@echo "Rerunning pipeline with force overwrite..."
	@$(PYTHON) -m src.main --force $(ARGS)

# Testing
test:
	@$(PYTHON) -m pytest tests/ -v

# Cleanup
clean:
	@echo "Cleaning temporary files..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleanup complete."