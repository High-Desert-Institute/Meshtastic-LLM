# Meshtastic-LLM Roadmap

_Status legend_: `[ ]` not started · `[?]` in progress/testing · `[x]` complete

_Update whenever task status changes._

## `meshtastic-bridge.py`
- [x] Nodes registry upsert flow and name change handling
- [x] Sightings ingestion with hashing and once-per-day guard
- [x] Thread CSV writers plus inbound dedupe on message_id or composite key
- [x] Meshtastic event subscription that records nodes, threads, and sightings
- [x] Outbound queue monitor that marks rows outbound or backs off on failure
- [?] Crash-safe resume logic covering queued→outbound lifecycle (baseline implementation; needs soak testing)

## `ai-agent.py`
- [ ] Trigger detection for DMs and “librarian …” channel messages with cooldowns
- [ ] Context assembly respecting MAX_CONTEXT_CHARS and minimal system prompt
- [ ] Ollama HTTP client with retries, model selection, and timing capture
- [ ] Reply generation enforcing MAX_MESSAGE_CHARS and chunking queued rows
- [ ] Idempotent scanning loop that skips threads already answered

## Shared Infrastructure
- [x] Config file + env override loader; path bootstrap utilities
- [x] CSV helpers for atomic append, advisory locks, and schema validation, incl. filename sanitization
- [?] Observability: structured logging, essential counters, optional metrics endpoint (basic logging in place)
- [?] Error handling hardening for corrupt CSVs, partial writes, disk-full scenarios (core patterns implemented; more guards needed)
- [ ] End-to-end integration checks to prevent duplicate sends across restarts
- [ ] Test suite covering unit cases, Meshtastic mock, Ollama stub, and prompt golden files
- [x] Run supervision scripts to start/restart both services with logging (`start.sh`, `start.ps1`)
- [ ] Optional Dockerfiles or packaging for the two Python services

## Documentation & Rollout
- [?] README updates for setup, configuration, troubleshooting, and CSV schemas (initial rewrite done; needs troubleshooting section)
- [ ] Prompts/data workflow documentation for operators and Jekyll consumers
- [ ] Field dry-run with log review and tuning of message limits/cooldowns
- [ ] Final polish: channel/DM allow/block lists and safety/call refusal patterns
