# Meshtastic-LLM — Project Specification

## Comprehensive project specification

### Purpose
Provide a minimal, reliable, and offline-first bridge between a Meshtastic node and a local LLM (Ollama) to:
- Persist structured CSV records of mesh activity (nodes, messages, sightings)
- Run a compact “librarian” AI agent that responds to DMs and to channel messages prefixed with “librarian …”
- Queue AI replies for the Meshtastic bridge to send, keeping messages short and within mesh constraints

This runs on a Raspberry Pi 5 with local Docker instances of Ollama and OpenWebUI. Target models: qwen3-4b-q8 (thinking and instruct variants).

### High-level architecture
- meshtastic-bridge (Python)
  - Connects to a Meshtastic node via the Meshtastic Python library
  - Ingests inbound messages and telemetry
  - Maintains per-node CSV datasets (nodes, threads, sightings) under nodes/<node_uid>/
  - Monitors thread CSVs for messages with state=queued and sends them over mesh, marking as outbound when successfully sent
- ai-agent (Python)
  - Scans thread CSVs for inbound DM messages and channel messages starting with “librarian” that have no AI reply yet
  - Builds compact prompts with recent thread context, calls local Ollama, and writes queued replies back to the thread CSV
  - Records each model invocation to a prompts directory: appends a row to prompts.csv and writes a Markdown file with YAML front matter for Jekyll rendering
- Local LLM (Ollama)
  - Hosts qwen3-4b-q8 models (thinking/instruct)
  - OpenWebUI optional for interactive inspection and prompt tuning

All communication between the two Python processes is file-based via CSVs on local storage, with simple file locks to ensure integrity.

### Process boundaries and responsibilities
- meshtastic-bridge.py
  - Subscribe to Meshtastic events (messages, user metadata, telemetry)
  - Normalize and record data into CSVs
  - Enforce deduplication rules (messages, sightings)
  - Read thread CSVs for state=queued rows and send the message; update to outbound and record send metadata (e.g., message ID/ACK)
  - Resilience: survive restarts, avoid duplicate sends, flush buffered writes atomically
- ai-agent.py
  - Periodically scan threads for new inbound content requiring a reply
  - Determine triggers:
    - DM threads: reply to inbound messages
    - Channel threads: reply only to messages starting with the word “librarian” (case-insensitive, followed by space or punctuation)
  - Context assembly: gather the recent thread history up to a configured token/character budget
  - Prompting: use short, utility-focused prompting with strict length limits
  - Write responses as new rows with state=queued for pickup by the bridge

### Node identity and multi-node support
The system can manage multiple Meshtastic nodes connected to the same machine (e.g., multiple USB serial ports). Each attached node is assigned/derived a stable unique identifier node_uid (e.g., Meshtastic node ID, long ID, or a user-configured alias). All data for each node is scoped to a dedicated directory.

### Data storage layout (CSV-based)

- nodes/<node_uid>/nodes.csv — Registry of all mesh nodes seen by this attached device
- nodes/<node_uid>/sightings.csv — Periodic sightings observed by this attached device (deduplicated)
- nodes/<node_uid>/threads/channels/<channel_name>.csv — Per-channel message logs for this device
- nodes/<node_uid>/threads/dms/<node_id_or_name>.csv — Per-DM thread logs for this device
- prompts/prompts.csv — Registry of all LLM prompt runs with timing and token metrics
- prompts/<prompt_id>.md — One Markdown file per prompt run containing the prompt and response, with timing and token metrics stored as YAML front matter for Jekyll

Filenames
- Channel files are named from the channel’s display name, sanitized (lowercase, spaces to underscores, remove unsafe characters)
- DM files are named with the peer’s node ID where possible; fall back to a sanitized short/long name

Atomicity & locking
- Full rewrites (e.g., CSV header backfill or bulk replace) use a temp file + atomic rename pattern
- Append operations hold an advisory per-file lock and append a single row directly to the CSV to avoid rewriting the entire file
- A simple advisory lock file (e.g., .lock alongside the CSV) prevents concurrent writers from corrupting files. Locks are per-file within each nodes/<node_uid>/ tree to allow concurrent operation across multiple attached nodes.

Jekyll compatibility
- The prompts directory is designed to be renderable by Jekyll/GitHub Pages
- Each prompts/<prompt_id>.md file includes YAML front matter for easy listing and theming
- Filenames use the prompt ID to avoid collisions and enable stable linking

### CSV schemas
1) nodes.csv
- node_id (string)
- short_name (string)
- long_name (string)
- first_seen_at (ISO 8601 UTC)
- last_seen_at (ISO 8601 UTC)

2) sightings.csv
- node_id (string)
- latitude (float)
- longitude (float)
- rssi (int)
- telemetry_json (string; compact JSON of additional fields, e.g., battery, voltage, snr)
- observed_at (ISO 8601 UTC)
- sighting_hash (string; deterministic hash of the meaningful contents to detect duplicates)

Deduplication rule for sightings
- At most one row per node per calendar day, and only when the sighting_hash differs from the last stored for that node (i.e., something changed)

3) threads/*.csv (for both channels and DMs)
- thread_type (string; channel|dm)
- thread_key (string; channel name or DM node_id)
- message_id (string; mesh message ID if available; else a generated UUID)
- direction (string; inbound|queued|outbound)
- sender_id (string)
- reply_to_id (string; optional; empty if not a reply)
- timestamp (ISO 8601 UTC)
- content (string; plain text message)
- send_attempts (int; default 0)
- send_status (string; empty|sent|failed)
- meta_json (string; optional JSON for extras, e.g., ACKs, retries, chunking info)

Notes
- direction reflects lifecycle: inbound (received), queued (ready to send), outbound (sent)
- The bridge updates send_attempts and send_status on each try
- The AI only appends queued rows; it never edits outbound rows

4) prompts/prompts.csv (registry of model invocations)
- prompt_id (string; UUID v4 or similar)
- node_uid (string; the attached device that initiated the prompt)
- datetime (string; sortable local or UTC time in YYYY-MM-DD HH:MM:SS; store UTC recommended)
- model (string; Ollama model name)
- thread_type (string; channel|dm)
- thread_key (string; channel name or DM node_id)
- source_message_id (string; the inbound message that triggered the prompt)
- response_message_id (string; the first queued/outbound message_id created by the AI)
- prompt_tokens (int; if available)
- completion_tokens (int; if available)
- prompt_tps (float; tokens/sec for prompt eval if reported)
- eval_tps (float; tokens/sec for generation if reported)
- duration_ms (int; total wall time for the call if available)
- meta_json (string; raw or summarized fields returned by the LLM endpoint)

### Message and reply constraints
- Short replies only: A hard cap on characters per message (configurable; conservative default)
- Optional chunking: If necessary, split long replies into multiple queued rows, each within the character cap, and include sequence markers in content or meta_json
- No sensitive or personal data beyond what is present in the thread context
- Avoid overuse: per-thread cooldown to prevent flooding channels

### Triggers and context policy
- DM threads: always respond to latest inbound message if not yet answered by AI (using the default persona unless explicitly overridden with a trigger)
- Channel threads: only respond when content begins with a recognized persona trigger (e.g., “librarian …”, “elmer …”, case-insensitive); strip the trigger word from the prompt
- Context window assembly:
  - Include the most recent N messages (configurable) and the latest node metadata of the participants
  - Truncate to a character/token budget before calling the model
  - Include a minimal system prompt enforcing brevity, clarity, and actionable steps

### Configuration
Use a small config file (YAML or TOML) and/or environment variables. Suggested keys:
- DATA_DIR (default ./data)
- LOG_LEVEL (info|debug|warn|error)
- OLLAMA_BASE_URL (e.g., http://localhost:11434)
- OLLAMA_MODEL_INSTRUCT (e.g., qwen3-4b-q8-instruct)
- OLLAMA_MODEL_THINK (optional; for chain-of-thought planning kept internal, not emitted)
- MAX_MESSAGE_CHARS (e.g., 180–220; tune per mesh constraints)
- MAX_CONTEXT_CHARS (upper bound for prompt context)
- REPLY_COOLDOWN_SECONDS (per thread)
- CHANNEL_ALLOWLIST / BLOCKLIST (optional)
- DM_ALLOWLIST / BLOCKLIST (optional)
- BRIDGE_POLL_INTERVAL_MS and AI_POLL_INTERVAL_MS
- TIMEZONE (store in UTC; format display as needed)
- PROMPTS_DIR (default ./prompts)
- ENABLE_PROMPT_LOGS (bool; default true)
- NODES_BASE_DIR (default ./data/nodes)
- NODE_UID_STRATEGY (auto|config); auto derives from device/node ID; config allows explicit mapping
 - PERSONAS_DIR (default ./config/personas) — directory of persona configuration files (TOML)

### Personas (configurable agents)

Personas define named assistant profiles that the AI agent can invoke based on triggers in messages. Each persona is configured via a TOML file in `config/personas/` and includes:

- name: Unique identifier that also serves as a channel trigger word (e.g., "librarian").
- triggers: Optional list of trigger aliases (e.g., ["librarian", "lib"]).
- description: Human-readable summary of the persona.
- system_prompt: The base/system instructions for the assistant.
- model: Preferred Ollama model for this persona (falls back to global defaults).
- temperature: Generation temperature.
- max_message_chars, max_context_chars: Optional overrides for persona-specific limits.
- cooldown_seconds: Optional override for reply cooldown per thread.
- rag: Boolean indicating whether this persona may use retrieval-augmented generation tools.
- tools: Optional list of tool names or capabilities the persona may use (e.g., ["local_rag"]).
- meta: Free-form metadata reserved for future features.

Location and file format
- Directory: `config/personas/`
- File naming: `<name>.toml` (lowercase, sanitized)

Example persona file (TOML):

```toml
name = "librarian"
triggers = ["librarian", "lib"]
description = "A concise research librarian who can use local RAG tools to answer complex questions."
model = "qwen3-4b-q8-instruct"
temperature = 0.2
max_message_chars = 200
max_context_chars = 1400
cooldown_seconds = 30
rag = true
tools = ["local_rag"]

system_prompt = """
You are the Librarian: a concise, utility-first research assistant operating over a very low-bandwidth mesh.
Answer only what's asked, keep replies short and actionable, and prefer bullet points.
If you used retrieval, add a tiny 'sources:' section with 1–3 short citations.
If the answer is uncertain, state what would be needed and suggest next steps.
Never include secrets; avoid speculation.
"""
```

Selection rules
- Channel messages: A message that begins with a persona trigger (case-insensitive) selects that persona for a reply. The trigger word is stripped from the prompt that is sent to the model.
- DM threads: The AI agent uses a default persona (configurable; default "librarian"). A DM can opt into a different persona by starting with a trigger word.
- Allow/block lists and per-thread cooldowns still apply; persona settings may further restrict behavior.

Initial personas
- elmer: a helpful amateur radio mentor focused on Meshtastic and radio practice (no RAG).
- librarian: a concise research librarian that can use local RAG tools for harder questions.

### Operational behavior
- Startup
  - Bridge: connect to Meshtastic, ensure data dirs/files exist, backfill headers if absent
  - AI: ensure data dirs exist, warm up Ollama endpoint (optional prompt), then idle-loop
- Shutdown
  - Flush pending writes; release locks cleanly
- Crash safety
  - On restart, bridge scans for queued items and resumes sending; AI resumes scanning from last seen timestamp
  - The prompts registry is append-only; repeated AI attempts for the same inbound should either: (a) be deduped via source_message_id, or (b) create a new prompt_id with reason annotated in meta_json
 - Multi-node operation
   - The bridge runs one process per attached node, or a single process multiplexing ports; in either case, each instance writes exclusively under nodes/<node_uid>/
   - The AI agent can process across all nodes by scanning nodes/*/threads/ trees; per-thread cooldown is enforced independently per node

### Error handling and resiliency
- Network and radio errors: retry with backoff; do not duplicate outbound rows
- CSV corruption: attempt to read-only open and skip malformed lines while logging; never discard data silently
- Disk space low: emit clear logs and stop queuing; resume when space returns
- Duplicate messages: detect via message_id when the radio stack provides one; else compare (sender_id, timestamp, content) with a short grace window

### Logging and observability
- Structured logs (JSON) recommended; include thread_key and message_id in events
- Key events: node seen/updated, message received, message queued, send succeeded/failed, sighting stored/skipped, AI inference started/completed/skipped
- Optional lightweight metrics: counters for inbound/outbound, deduped messages, reply counts, failures
- Prompt audit trail: prompts.csv and per-prompt Markdown capture model, runtime, and tokenization metrics for diagnostics and demonstrations
- Every CLI invocation writes a new timestamped log file under logs/log.YYYY-MM-DD-HH-MM-SS-ffffff.txt capturing CLI arguments, config overrides, and notable events
- A `--status` CLI flag prints queued message counts, last-seen peers, and the most recent log file to support quick diagnostics

### Security and privacy
- Everything is local; no cloud calls expected
- Do not store secrets in CSVs
- Be mindful of personally identifiable information in message content; avoid copying context beyond what’s needed
- The prompts directory captures prompts and responses; ensure no secrets or sensitive data are included beyond what is necessary; allow opt-out via ENABLE_PROMPT_LOGS

### Testing approach
- Unit tests for CSV helpers, filename sanitization, dedupe logic (sightings), trigger detection (DM vs librarian)
- Integration tests with a Meshtastic mock and an Ollama stub server
- Golden-file tests for prompt assembly and reply splitting
- A `--test` CLI flag runs the bridge against a stubbed Meshtastic interface using temporary directories so automated agents can exercise the workflow without modifying real data

### Deliverables and acceptance criteria
- Two runnable Python scripts with minimal config and no external DB dependencies
- Deterministic CSV schemas and portable data directory
- Reliable deduplication of sightings (daily per node, only when changed)
- AI consistently replies to DMs and to “librarian …” messages only
- Replies respect character limits, get queued, and are then sent by the bridge
- Prompts directory exists with a stable CSV schema and Markdown files containing YAML front matter; content is Jekyll-renderable
- Supports multiple attached nodes concurrently with isolated per-node data directories and no cross-node interference

### Per-prompt Markdown file format (prompts/<prompt_id>.md)
Each file includes YAML front matter followed by sections for the prompt and response. Suggested structure:

Front matter keys
- layout: prompt (optional; for Jekyll theme)
- title: Prompt <prompt_id> (optional)
- prompt_id: <uuid>
- datetime: YYYY-MM-DD HH:MM:SS (UTC)
- model: qwen3-4b-q8-instruct
- thread_type: channel|dm
- thread_key: <channel_name|node_id>
- source_message_id: <id>
- response_message_id: <id>
- prompt_tokens: <int>
- completion_tokens: <int>
- prompt_tps: <float>
- eval_tps: <float>
- duration_ms: <int>
- tags: [librarian, meshtastic, ollama] (optional)

Body outline
- ## Prompt
  - The exact prompt sent to the model (including minimal system prompt if applicable)
- ## Response
  - The model’s response text that was (or will be) split and queued to the thread
- ## Context (optional)
  - Brief summary of how the prompt was assembled (e.g., number of recent messages included)


## Meshtastic-LLM Social Context

### Organization & Mission
High Desert Institute focuses on resilient communications and practical AI for remote, bandwidth-constrained communities. Meshtastic-LLM extends that mission by pairing LoRa mesh coverage with an offline, privacy-preserving knowledge assistant.

### Partnerships & Collaborations
- **Local field teams:** Provide real-world usage feedback and environmental telemetry.
- **Community workshops:** Demonstrate mesh networking and offline AI deployments.
- **Open-source contributors:** Maintain integrations with Meshtastic, Ollama, and OpenWebUI.

### Use Cases & User Stories
- Rapid field checklists and Q&A over mesh during expeditions.
- Community resilience drills where residents rely on offline knowledge access.
- Classroom demonstrations showcasing constrained-device AI workflows.

### Community Impact & Benefits
- Enhances situational awareness via structured CSV telemetry.
- Empowers communities to run AI locally without internet dependencies.
- Encourages ethical, privacy-first tooling aligned with institute values.

### Accessibility & Inclusion
- Text-only interactions suit low-bandwidth and multilingual contexts.
- File-based workflows simplify auditing and allow CSV translation pipelines.
- Offline deployment respects communities with limited or intermittent connectivity.

### Ethical Considerations
- All storage remains local; prompts can be audited via append-only logs.
- Safety filters and refusal patterns are tracked for future implementation to avoid misuse.
- Opt-in documentation ensures participants understand data retention practices.

### Future Opportunities
- Deploy packaged bundles or Docker images for easier field rollout.
- Expand AI agent behavior with domain-specific playbooks and multi-turn memory.
- Integrate additional telemetry analytics for coverage and movement studies.


## CLI Logging and Interpretability Style Guide

This style guide outlines best practices for command‑line interface (CLI) development tools. Its goal is to make every action that your tool performs visible from the CLI so that both humans and large language models (LLMs) can understand and debug the workflow. The guidance in this document is language‑agnostic and can be applied to any project that exposes a command‑line surface, even if the project also ships a graphical user interface.

### 1. Philosophy

* **CLI first.** The CLI should be the primary interface for your tool. Even if a GUI exists, every operation must be executable from the command line so that automated agents and scripts can drive the tool without a GUI.
* **Transparency.** Everything that happens in your tool should be visible in the CLI. Hiding behaviour behind GUI elements or implicit side effects makes it impossible for developers and LLMs to reason about what the tool is doing.
* **Reproducibility.** A user (or agent) following the documented CLI commands should be able to reproduce any run of your tool. Avoid hidden state or reliance on external environment configuration when possible.
* **Cross-platform reliability.** All code must run correctly on both Windows and Linux without requiring manual tweaks or OS-specific forks.

### 2. Verbose logging

To support LLMs and developers in understanding your tool’s behaviour, every CLI‑based project must create a root‑level log directory (for example, `logs/`). Each run of the application must write to a brand‑new log file inside that directory named with the timestamp of when the run started using a sortable pattern such as `log.YYYY-MM-DD-HH-mm-ss-ffffff.txt`. Every per-run log file must contain:

* **User inputs and actions.** Log the exact command arguments or interactive input received.
* **Outputs and results.** Log what the tool prints to stdout/stderr and any side effects it performs (e.g. files written, network calls made, database queries executed). Include timestamps to aid debugging.
* **Contextual metadata.** Provide information about the component generating the log (module name, function, or class) and the phase of the operation.

Logs should be written in a plain‑text, append‑only format. The goal is to give agents complete visibility into what happened during execution, so do not reuse log files across runs—always start a fresh, timestamped file when the process begins. If multiple processes are spawned (for example, launching Tor or IPFS as sub‑processes), their stdout/stderr should also be captured and appended to that run’s log file.

### 3. Test mode and simulation

LLMs often need to verify that your tool behaves correctly without modifying real data. Provide a dedicated **test mode** (for example, via a `--test` or `-test` flag) that:

* Simulates a broad range of typical user actions.
* Produces the same verbose logs as a normal run.
* Does **not** modify any persistent user data (such as real databases or on‑disk files). Use temporary directories or in‑memory data structures in test mode.

When running in test mode, the tool should exercise enough code paths to make automated testing effective. This allows LLMs to verify functionality without incurring the overhead of running integration tests during every normal startup.

### 4. README.md and style guide references

Every repository that contains CLI‑based tools must include a comprehensive **`README.md`** file. The styleguide references must be listed at the top of the README.md so that agents will always see them first. At a minimum, the README.md should:

* List relevant styleguide files at the top with brief descriptions
* Describe the high‑level purpose of the project and its architecture.
* Explain where logs live (for example, the root‑level `logs/` directory and the timestamped files it contains) and how to run the project in normal and test modes.

Agents and developers should read `README.md` first to understand how to apply the various style guides in the repository.

### 5. Project roadmap and specification requirements

#### Roadmap requirements

The roadmap must include:

* **Status legend** at the top explaining checkbox meanings:
  - `[ ]` - Not started
  - `[?]` - In progress / Testing / Development  
  - `[x]` - Completed and tested
* **All major and minor tasks** broken down by phases
* **Current status** clearly marked with appropriate checkboxes
* **Update instructions** requiring roadmap updates whenever tasks change status

#### Specification requirements

The specification must include:

* **Complete technical requirements** for all features
* **Architecture details** and component descriptions
* **Configuration schemas** and data formats
* **Success criteria** and acceptance tests
* **Security and privacy** requirements

#### Social context requirements

The social context document must include:

* **Organization details** and mission statement
* **Partnerships and collaborations** with other organizations
* **Use cases and user stories** showing how the project helps people
* **Community impact** and social benefits
* **Future opportunities** and potential for related projects
* **Stakeholder information** and target audiences
* **Cultural and social considerations** relevant to the project
* **Accessibility and inclusion** aspects
* **Ethical considerations** and responsible development practices

All three documents must be kept current whenever project details change, features are added, or development status updates.

### 6. Logging best practices

The following guidelines apply across languages:

* **Consistent format.** Use a structured logging format (e.g. timestamps, log level, module name, message). Consistency makes parsing and analysis easier for tools.
* **Appropriate log levels.** Use `INFO` for normal operations, `WARN` for recoverable issues, and `ERROR` for serious problems. Do not hide exceptions—log stack traces at the `ERROR` level.
* **Context in messages.** Include enough detail in each log entry to understand what was happening. For example, log input parameters, intermediate results, and the outcome of operations.
* **Cross‑language adherence.** When your project consists of components in multiple languages, ensure that each component writes to the same per-run log file in the same format.

### 7. Integration with large language models (LLMs)

LLMs cannot see your GUI or internal state; they rely entirely on textual output. To make your tool LLM‑friendly:

* **Expose state via CLI.** Provide commands or flags that output the current configuration, status, or internal metrics of your tool. Avoid requiring an API or GUI for this.
* **Descriptive errors.** Write error messages that explain what went wrong and how to fix it. Avoid cryptic messages or silent failures.
* **Deterministic output.** When possible, avoid non‑deterministic ordering of logs (e.g. due to concurrency) that could confuse automated analysis. If concurrency is necessary, clearly label log lines with thread or process identifiers.

### 8. Example CLI workflow

Document a typical usage scenario for your tool. For example:

```bash
# run the tool normally and capture logs
./mytool --input data.txt --output results.txt

# inspect the newest log file
ls logs/
cat "$(ls -t logs/log.* | head -n 1)"

# run in test mode
./mytool --test

# run a command to dump current status
./mytool --status
```

### 9. Language-Specific Supplementary Styleguides

By following this style guide, your CLI‑based development tools will remain transparent, debuggable, and compatible with automated agents as your project evolves.

## CLI-First Python Development Style Guide

This document describes a simple, command-line–first approach to writing, running, and distributing Python software. The focus is on making projects transparent, reproducible, and easy to integrate with automated agents. 

---

### 1. Philosophy of CLI-First Python Development

Most modern Python developers rely on IDEs, virtualenv managers, and packaging systems. While useful, these tools can:

* Introduce hidden state and complexity (virtual environments, IDE configs).
* Make automation harder when agents must guess about implicit behavior.
* Encourage reliance on external services (PyPI) without reproducibility guarantees.

A CLI-first style instead:

* Uses the **Python interpreter directly** (`python`, `pip`, `venv`).
* Relies on **text editors** and **scripts** (Makefiles, batch files, shell scripts).
* Keeps projects **transparent and reproducible**.
* Emphasizes **self-contained applications** that work on any machine.
* Requires **Windows and Linux parity** so the same codebase builds and runs without changes on both operating systems.

---

### 2. Basic Project Structure

A typical CLI-first Python project:

```
project/
  src/
    myapp/
      __main__.py    # entry point
      core.py        # core logic
  resources/
    etc/             # configuration files
  tests/
    test_core.py
  build/             # distribution artifacts (ignored by VCS)
```

* `src/myapp/__main__.py` → CLI entry point (`python -m myapp`)
* `resources/etc/` → static configs
* `tests/` → unit/integration tests
* `build/` → generated wheels, packages

---

### 3. Running and Packaging

Run directly from source:

```bash
# run as a module
python -m myapp --help
```

Create a **virtual environment** for reproducibility:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Build distribution artifacts:

```bash
python -m build   # produces .whl and .tar.gz in dist/
```

Install locally:

```bash
pip install dist/myapp-*.whl
```

---

### 4. Self-Contained Apps

Instead of relying on system Python, ship your own environment:

* **PyInstaller** or **shiv** → produce a single executable.
* **Docker** → containerize Python + your code.

Example with PyInstaller:

```bash
pyinstaller --onefile src/myapp/__main__.py --name myapp
./dist/myapp --help
```

---

### 5. Including External Tools (Tor, IPFS, etc.)

Bundle third-party tools alongside Python:

1. Place binaries into `resources/bin/`.
2. Extract them at runtime into a temporary directory.
3. Launch with `subprocess.Popen`.
4. Communicate via sockets, APIs, or subprocess pipes.

```python
import subprocess

subprocess.Popen([
    "bin/tor", "-f", "etc/tor/torrc"
])
```

---

### 6. Why Not Poetry or Conda?

Tools like Poetry/Conda are powerful, but not always necessary.

**You don’t need them if:**

* You want maximum transparency.
* You’re shipping self-contained executables.
* Dependencies are few or vendored.

**You may want them if:**

* You manage many dependencies.
* You need lockfile-style reproducibility.
* You publish widely on PyPI.

---

### 7. Benefits of This Style

* **Simplicity:** Just Python + your scripts.
* **Transparency:** No hidden configs or lock-in.
* **Portability:** One wheel or binary runs everywhere.
* **Control:** Choose what to vendor and bundle.
* **Reproducibility:** Your Makefile/script is the build pipeline.

---

### 8. Example Workflow

1. Write code in any text editor.
2. Run `make` (or script) to:

   * Run tests (`pytest`).
   * Package wheel.
   * Optionally build executable.
3. Test locally with `python -m myapp`.
4. Ship wheel, source tarball, or binary.

---

👉 By following this style, your Python projects stay minimal, reproducible, and fully CLI-driven. 


## Project roadmap (checklist)

Legend:
- [x] = done
- [?] = in progress or testing
- [ ] = not started yet

### Roadmap

- [x] **`meshtastic-bridge.py` core**
  - [x] Nodes registry upsert flow and name change handling
  - [x] Sightings ingestion with hashing and once-per-day guard
  - [x] Thread CSV writers plus inbound dedupe on message_id or composite key
  - [x] Meshtastic event subscription that records nodes, threads, and sightings
  - [x] Outbound queue monitor that marks rows outbound or backs off on failure
  - [x] PubSub listener scoping so each bridge only handles its own interface
  - [x] Serial port scan throttling and noise reduction between retries
  - [?] Crash-safe resume logic covering queued→outbound lifecycle (baseline implementation; needs soak testing)
  - [?] Adversarial code review
- [ ] **personas**
  - [x] Define persona config schema and directory: `config/personas/*.toml`
  - [x] Add two initial personas: `librarian` (RAG-enabled) and `elmer` (ham radio mentor)
  - [ ] Persona loader in AI agent with validation and helpful errors
  - [ ] Trigger detection updated to match any configured persona trigger in channels; default persona for DMs
  - [ ] Per-persona overrides for model, temperature, context limits, and cooldown
  - [ ] Optional runtime reload of persona files (on file change)
  - [ ] Unit tests for schema parsing, trigger matching, and selection logic
- [ ] **`ai-agent.py` features**
  - [ ] Trigger detection for DMs and persona-triggered channel messages (e.g., “librarian …”, “elmer …”) with cooldowns
  - [ ] Context assembly respecting MAX_CONTEXT_CHARS and minimal system prompt
  - [ ] Ollama HTTP client with retries, model selection, and timing capture
  - [ ] Reply generation enforcing MAX_MESSAGE_CHARS and chunking queued rows
  - [ ] Idempotent scanning loop that skips threads already answered
- [ ] **Shared infrastructure**
  - [x] Config file + env override loader; path bootstrap utilities
  - [x] CSV helpers for atomic append, advisory locks, and schema validation, incl. filename sanitization
  - [?] Observability: structured logging, essential counters, optional metrics endpoint (basic logging in place)
  - [?] Error handling hardening for corrupt CSVs, partial writes, disk-full scenarios (core patterns implemented; more guards needed)
  - [ ] Serial port filtering via `meshtastic.util` helpers to avoid probing unrelated devices
  - [ ] End-to-end integration checks to prevent duplicate sends across restarts
  - [ ] Test suite covering unit cases, Meshtastic mock, Ollama stub, and prompt golden files
  - [x] Run supervision scripts (e.g., `run.sh`) to start/restart both services with logging
  - [ ] Optional Dockerfiles or packaging for the two Python services
- [ ] **Documentation and rollout**
  - [?] README updates for setup, configuration, troubleshooting, and CSV schemas (initial pass completed; expand troubleshooting)
  - [ ] Prompts/data workflow documentation for operators and Jekyll consumers
  - [ ] Field dry-run with log review and tuning of message limits/cooldowns
  - [ ] Final polish: channel/DM allow/block lists and safety/call refusal patterns
