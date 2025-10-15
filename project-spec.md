# Meshtastic-LLM — Project Specification

## 1) Comprehensive project specification

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
- Writes use a temp file + atomic rename pattern
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
- DM threads: always respond to latest inbound message if not yet answered by AI
- Channel threads: only respond when content begins with “librarian” (case-insensitive); strip the trigger word from the prompt
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

### Security and privacy
- Everything is local; no cloud calls expected
- Do not store secrets in CSVs
- Be mindful of personally identifiable information in message content; avoid copying context beyond what’s needed
- The prompts directory captures prompts and responses; ensure no secrets or sensitive data are included beyond what is necessary; allow opt-out via ENABLE_PROMPT_LOGS

### Testing approach
- Unit tests for CSV helpers, filename sanitization, dedupe logic (sightings), trigger detection (DM vs librarian)
- Integration tests with a Meshtastic mock and an Ollama stub server
- Golden-file tests for prompt assembly and reply splitting

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


## 2) Social Context — fit with High Desert Institute
Meshtastic-LLM extends resilient, low-cost communications in remote or bandwidth-constrained environments with a compact, local “librarian” assistant. This aligns with common institute goals such as:
- Field operations support: Provide quick answers, checklists, or summaries in places with limited connectivity
- Community resilience: Offer a local knowledge helper over mesh without relying on the internet
- Education and outreach: Demonstrate practical AI on constrained networks, supporting workshops and training
- Privacy-first tooling: Keep data on-device, aligning with ethical use of AI and community trust
- Research enablement: Capture structured telemetry and message logs that can inform studies of mobility, coverage, and usage patterns

This project complements existing mesh networking efforts by adding a pragmatic knowledge interface. The librarian pattern encourages concise, high-signal interactions suited to LoRa constraints while remaining accessible to non-expert users.


## 3) Project roadmap (checklist)

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
  - [?] Crash-safe resume logic covering queued→outbound lifecycle (baseline implementation; needs soak testing)
- [ ] **`ai-agent.py` features**
  - [ ] Trigger detection for DMs and “librarian …” channel messages with cooldowns
  - [ ] Context assembly respecting MAX_CONTEXT_CHARS and minimal system prompt
  - [ ] Ollama HTTP client with retries, model selection, and timing capture
  - [ ] Reply generation enforcing MAX_MESSAGE_CHARS and chunking queued rows
  - [ ] Idempotent scanning loop that skips threads already answered
- [ ] **Shared infrastructure**
  - [x] Config file + env override loader; path bootstrap utilities
  - [x] CSV helpers for atomic append, advisory locks, and schema validation, incl. filename sanitization
  - [?] Observability: structured logging, essential counters, optional metrics endpoint (basic logging in place)
  - [?] Error handling hardening for corrupt CSVs, partial writes, disk-full scenarios (core patterns implemented; more guards needed)
  - [ ] End-to-end integration checks to prevent duplicate sends across restarts
  - [ ] Test suite covering unit cases, Meshtastic mock, Ollama stub, and prompt golden files
  - [x] Run supervision scripts (e.g., `run.sh`) to start/restart both services with logging
  - [ ] Optional Dockerfiles or packaging for the two Python services
- [ ] **Documentation and rollout**
  - [ ] README updates for setup, configuration, troubleshooting, and CSV schemas
  - [ ] Prompts/data workflow documentation for operators and Jekyll consumers
  - [ ] Field dry-run with log review and tuning of message limits/cooldowns
  - [ ] Final polish: channel/DM allow/block lists and safety/call refusal patterns
