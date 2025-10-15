# Prompts directory

This folder records every LLM invocation made by the librarian AI.

- `prompts.csv` — Append-only registry of prompt runs with timing and token metrics.
- `<prompt_id>.md` — One Markdown file per prompt, suitable for Jekyll/GitHub Pages.

Per-prompt Markdown structure:

---
layout: prompt
title: "Prompt <prompt_id>"
prompt_id: <uuid>
node_uid: <attached_node_uid>
datetime: YYYY-MM-DD HH:MM:SS
model: qwen3-4b-q8-instruct
thread_type: channel|dm
thread_key: <channel_name|node_id>
source_message_id: <id>
response_message_id: <id>
prompt_tokens: <int>
completion_tokens: <int>
prompt_tps: <float>
eval_tps: <float>
duration_ms: <int>
tags: [librarian, meshtastic, ollama]
---

## Prompt

<exact prompt text>

## Response

<model response text>

## Context (optional)

<notes about context assembly>
