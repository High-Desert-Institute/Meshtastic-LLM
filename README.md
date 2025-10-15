# Meshtastic-LLM

## Styleguide References
- `project-spec.md` – full technical specification plus CLI logging and CLI-first Python style requirements.
- `project-roadmap.md` – granular task tracking with status legend per the styleguide.
- `social-context.md` – organizational context, partnerships, and impact narrative.

Read these documents before contributing so CLI, logging, and documentation stay consistent.

## Project Overview
Meshtastic-LLM provides an offline-first bridge between a Meshtastic node and a local Ollama model. The bridge persists mesh activity to CSV, exposes data for the forthcoming AI agent, and keeps all communications file-based for resilience on low-resource hardware.

## Running the Bridge
```bash
# Linux/macOS
./start.sh

# Windows PowerShell
./start.ps1
```
Both scripts install dependencies from `requirements.txt`, ensure Ollama/OpenWebUI containers are running, and then invoke `meshtastic-bridge.py` with `config/default.toml`.

### Direct CLI usage
```bash
python meshtastic-bridge.py --config config/default.toml
```

Key flags:
- `--status` prints a summary of nodes, queued messages, and the latest log file.
- `--test` runs the bridge with a synthetic Meshtastic stub, writing output to a temporary data tree without touching real hardware.
- `--log-dir` overrides where per-run log files are written.

## Logging and Interpretability
Each run generates a fresh timestamped log (`logs/log.YYYY-MM-DD-HH-MM-SS-ffffff.txt`) containing CLI arguments, configuration overrides, structured events, and any serial send attempts. Inspect logs with:
```bash
ls logs/
tail -f logs/log.*.txt
```

## Test Mode
Test mode (`--test`) uses an in-memory Meshtastic stub to simulate telemetry and text packets while storing all CSV output in a temporary directory. The command emits the location of the generated data and log file when it finishes, satisfying the styleguide requirement for non-destructive verification.

## Diagnostics and Prompts
The `prompts/` directory remains append-only, containing `prompts.csv` plus per-prompt Markdown files (with YAML front matter) so the repository can be rendered by Jekyll/GitHub Pages for auditability.
