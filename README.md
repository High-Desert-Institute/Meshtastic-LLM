---
layout: project
title: "Meshtastic-LLM"
slug: meshtastic-llm
guilds:
    - pathfinders
link: "https://github.com/High-Desert-Institute/Meshtastic-LLM"
summary: >-
    A tool for conneting local LLMs to the Meshtastic network.
---

# Meshtastic-LLM

## Styleguide References
- `project-spec.md` – full technical specification plus CLI logging and CLI-first Python style requirements. granular task tracking with status legend per the styleguide, and organizational context, partnerships, and impact narrative.

Read these documents before contributing so CLI, logging, and documentation stay consistent.

## Project Overview
Meshtastic-LLM is a two-process stack that keeps a Meshtastic node synchronized with a local Ollama model. The Meshtastic bridge ingests telemetry and messages, persists everything to CSV, and delivers queued replies. The AI agent watches the same thread CSVs, applies persona triggers, calls Ollama through a single worker thread, and appends chunked replies ready for mesh delivery. Everything is file-based so the system stays resilient on low-resource, offline-first hardware.

## Components
- **Meshtastic bridge (`meshtastic-bridge.py`)** – Connects to the radio, records nodes/sightings/threads, and flushes queued outbound messages with retry/backoff logic.
- **AI agent (`ai-agent.py`)** – Loads persona TOML files, handles control commands immediately, queues LLM tasks, and writes newline-safe outbound chunks back to the thread CSVs.
- **Local LLM (Ollama)** – Serves persona-selected models; the agent validates connectivity and pulls models on demand.
- **Prompts archive** – Reserved for per-call CSV + Markdown audit logs (prompt logging hooks are stubbed until metrics land).

## Prerequisites
- Python 3.11 or newer (both scripts rely on `tomllib` and `zoneinfo` from the stdlib)
- Python dependencies from `requirements.txt` (`meshtastic`, `ollama`)
- A Meshtastic radio on USB serial (or a compatible TCP bridge if you adapt the config)
- An Ollama daemon reachable at the configured base URL (`config/default.toml` defaults to `http://docker-ai0:11434`)
- Optional: Docker installations of Ollama and OpenWebUI (the start scripts will create/manage them)

Install dependencies with:
```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Running the Stack
The convenience scripts install dependencies, ensure the Ollama/OpenWebUI containers are online, launch the AI agent, and then start the Meshtastic bridge.
```bash
# Linux/macOS
./start.sh

# Windows PowerShell
./start.ps1
```
`start.sh` must be executed as `root` because it wires Docker networking for the containers. `start.ps1` runs without elevation but assumes Docker Desktop is available if you want containers.

### Bridge quick test (no hardware required)
```bash
python meshtastic-bridge.py --test
```
This mode now reuses a single in-memory Meshtastic stub so the bridge exercises telemetry ingestion, DM logging, and outbound queue flushing without touching real devices. The command prints the temporary data path and log file when it exits.

### Running the bridge without the helper scripts
```bash
python meshtastic-bridge.py --config config/default.toml
```

Key flags:
- `--status` prints a summary of nodes, queued messages, and the latest log file.
- `--test` runs the bridge with a synthetic Meshtastic stub, writing output to a temporary data tree without touching real hardware.
- `--log-dir` overrides where per-run log files are written.
- `--serial-port` pins the Meshtastic radio path if auto-detect sees too many COM ports (recommended on Windows).

The bridge will enumerate available serial ports once at startup, then at most once per minute on subsequent retries. Only the initial scan emits warnings for missing devices; later attempts log at debug level so normal operation stays quiet.

When multiple ports remain after filtering, the CLI starts one bridge thread per port and keeps each instance isolated by checking the interface object on every PubSub callback.

Configuration values live in `config/default.toml` and can be overridden by environment variables prefixed with `MESHTASTIC_LLM_` (double underscores map to dots; see `load_config()` for the exact mapping). Data directories default to `data/` and `logs/` relative to the repo root.

### AI agent CLI usage
The agent runs automatically from the start scripts, but you can launch it directly for diagnostics or cron-style runs:
```bash
python ai-agent.py --config config/default.toml
```
Useful flags:
- `--status` prints persona runtime snapshots (queue depth, today/total call counts, last start time).
- `--once` performs a single scan of every thread CSV and exits (handy for tests or manual queues).
- `--log-dir` and `--personas-dir` override the default locations when experimenting.

The agent maintains a single LLM worker thread. Every matched message becomes a task on that queue, the worker ensures the requested model is present in Ollama (pulling once if needed), strips `<think>` sections from responses, and writes chunked replies back to the thread CSV while updating persona runtime counters.

## AI Agent personas and control commands
- Persona files live in `config/personas/*.toml` and include triggers, model overrides, message limits, temperature, and timezone.
- Messages that start with a trigger word (e.g. `librarian …`, `elmer …`) route to that persona. Control commands (`start`, `stop`, `status`, `config`, `help`) execute immediately without an LLM call so you can manage personas even if Ollama is offline.
- `status` replies now append an Ollama health line that reports whether the agent can reach the server and if all required models are available, downloading, or missing.
- Runtime fields (`running`, `total_calls`, `today_calls`, `queue_count`, etc.) are stored back into the persona file with atomic writes, enabling REST-style observability from the filesystem.
- LLM replies honour persona or global `max_message_chars`. Messages are prefixed with the persona name (and chunk progress when split), and long answers are divided into numbered chunks with metadata so the bridge can send them sequentially. All newline characters are escaped in CSV so round-tripping between the agent and bridge stays lossless.

DM fallbacks and richer context assembly are still on the roadmap; today the agent responds to channel triggers only.

### Persona quick reference
| Persona | Triggers | Model override | Temperature | Notable behavior |
|---------|----------|----------------|-------------|------------------|
| librarian | `librarian`, `lib` | `qwen3:0.6b-q4_K_M` | 0.2 | Concise research aide that can call local RAG tools; defaults to max 200 chars and 30 s cooldown. |
| elmer | `elmer`, `ham` | `qwen3:0.6b-q4_K_M` | 0.3 | Friendly ham radio mentor; keeps guidance practical and short. |

Every persona can override `max_message_chars`, `max_context_chars`, `cooldown_seconds`, and other Ollama options. When these fields are omitted, the agent falls back to the global defaults in `config/default.toml`.

### Adding or updating personas
1. Copy an existing file in `config/personas/` and rename it (e.g. `fieldtech.toml`).
2. Update the metadata (`name`, `triggers`, `description`, `model`, `system_prompt`, etc.).
3. Leave the runtime block at the bottom alone; the agent rewrites those fields atomically during runs.
4. Reload the agent (or wait for the next scan) to pick up the changes. The agent re-reads persona files on every polling cycle, so edits take effect without a restart.

Control commands operate per persona across all nodes. For example, `librarian stop` pauses LLM responses everywhere until you issue `librarian start` again.

## Data layout
Thread, node, and sighting CSVs live under `data/nodes/<node_uid>/…` (exact layout in `project-spec.md`). Prompts will be archived under `prompts/` once token metrics are wired up. Both scripts use advisory lock files (`*.lock`) to defend against concurrent writers.

## Logging and Observability
Each component writes a timestamped log under `logs/`:
- Bridge: `logs/log.YYYY-MM-DD-HH-MM-SS-ffffff.bridge.<port>.txt`
- AI agent: `logs/log.YYYY-MM-DD-HH-MM-SS-ffffff.agent.txt`

Inspect logs with:
```bash
ls logs/
tail -f logs/log.*.txt
```

## Test Mode
- `python meshtastic-bridge.py --test` exercises the bridge end-to-end using an in-memory Meshtastic stub and a temporary data tree.
- `python ai-agent.py --once --config …` lets you point the agent at fixture CSVs for deterministic runs without keeping the worker thread alive.

## Diagnostics and Prompts
The `prompts/` directory is reserved for per-call audit trails (CSV registry + Markdown transcripts with YAML front matter). Prompt logging hooks exist in the agent configuration and will start emitting files once token metrics land.

## Current State & Next Steps
- Bridge and agent now run together from the start scripts, persisting newline-safe CSV rows and keeping per-run logs.
- Persona control commands, runtime counters, and queue depth tracking are live; each LLM call flows through a single worker thread with Ollama model validation/pulls.
- Responses are stripped of `<think>` sections, chunked to mesh-safe sizes, and annotated with metadata for the bridge to send sequentially.
- Remaining roadmap items (DM defaults, richer context windows, prompt logging, broader test coverage) are tracked in the checklist inside `project-spec.md`.

## Troubleshooting
- **No radio detected:** supply `--serial-port COMx` (Windows) or `/dev/ttyUSBx` (Linux) to bypass auto-detect, and confirm no other tool has the port open.
- **Repeated send failures:** inspect the per-thread CSV under `data/nodes/<uid>/threads/...` for `send_status=failed` rows and check the log for reported exceptions.
- **CSV lock timeouts:** ensure only one bridge instance is pointed at a given data directory when running outside multi-port mode.
- **Ollama connection errors:** confirm the Ollama daemon is listening on `OLLAMA_BASE_URL`, pull the configured models manually (`ollama pull <model>`), or adjust the URL in `config/default.toml` / `MESHTASTIC_LLM_OLLAMA__BASE_URL`.
