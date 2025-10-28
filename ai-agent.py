#!/usr/bin/env python3
"""AI agent process for Meshtastic-LLM."""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import logging
import os
import queue
import random
import re
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ollama import Client

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Python 3.11 or newer is required to run this script") from exc

try:
    from zoneinfo import ZoneInfo
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("The zoneinfo module is required and should ship with Python 3.9+") from exc

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config" / "default.toml"
DEFAULT_ENV_PREFIX = "MESHTASTIC_LLM_"
DEFAULT_PERSONA_NAME = "librarian"
CONTROL_COMMANDS = {"start", "stop", "status", "config", "help"}
THREAD_HEADERS = [
    "processed",
    "thread_type",
    "thread_key",
    "message_id",
    "direction",
    "sender_id",
    "reply_to_id",
    "timestamp",
    "content",
    "send_attempts",
    "send_status",
    "meta_json",
]


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dump_meta(meta: Dict[str, Any]) -> str:
    return json.dumps(meta, separators=(",", ":"), ensure_ascii=True)


def resolve_path(path_like: Path | str) -> Path:
    candidate = Path(path_like)
    if candidate.is_absolute():
        return candidate
    return (BASE_DIR / candidate).resolve()


@dataclass
class AgentConfig:
    data_root: Path
    nodes_base: Path
    prompts_dir: Path
    logs_dir: Path
    personas_dir: Path
    ai_poll_interval: float
    timezone: str
    env_prefix: str
    default_persona: str
    ignore_channel_indexes: List[int]
    max_message_chars: int
    max_context_chars: int
    reply_cooldown_seconds: int
    enable_prompt_logs: bool
    ollama_base_url: str
    ollama_model_instruct: str
    ollama_model_think: Optional[str]
    log_file: Optional[Path] = None


@dataclass
class PersonaRuntime:
    running: bool = False
    total_calls: int = 0
    today_calls: int = 0
    today_date: str = ""
    last_started: str = ""
    control_calls: int = 0
    queue_count: int = 0


class FileLock:
    def __init__(self, target: Path, timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        self._lock_path = target.with_suffix(target.suffix + ".lock") if target.suffix else Path(str(target) + ".lock")
        self._timeout = timeout
        self._poll = poll_interval
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        deadline = time.time() + self._timeout
        while True:
            try:
                self._fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return
            except FileExistsError:
                if time.time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock {self._lock_path}")
                time.sleep(self._poll)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            return

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class CSVStore:
    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger

    @staticmethod
    def _ensure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _escape_field(text: str) -> str:
        if not isinstance(text, str):
            return text
        if not text:
            return ""
        text = text.replace("\\", "\\\\")
        text = text.replace("\r", "\\r")
        text = text.replace("\n", "\\n")
        return text

    @staticmethod
    def _unescape_field(text: str) -> str:
        if not isinstance(text, str) or "\\" not in text:
            return text
        result: List[str] = []
        i = 0
        length = len(text)
        while i < length:
            char = text[i]
            if char == "\\" and i + 1 < length:
                nxt = text[i + 1]
                if nxt == "n":
                    result.append("\n")
                    i += 2
                    continue
                if nxt == "r":
                    result.append("\r")
                    i += 2
                    continue
                if nxt == "\\":
                    result.append("\\")
                    i += 2
                    continue
            result.append(char)
            i += 1
        return "".join(result)

    @classmethod
    def _decode_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in list(row.items()):
            if isinstance(value, str):
                row[key] = cls._unescape_field(value)
        return row

    def ensure_file(self, path: Path, headers: Iterable[str]) -> None:
        headers_list = list(headers)
        self._ensure_parent(path)
        with FileLock(path):
            if path.exists():
                with path.open("r", newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    existing_headers = reader.fieldnames or []
                    rows = [self._decode_row(dict(row)) for row in reader]
                if existing_headers == headers_list:
                    return
                self._rewrite_locked(path, headers_list, rows)
                return
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers_list)

    def read_rows(self, path: Path) -> List[Dict[str, str]]:
        if not path.exists():
            return []
        with FileLock(path):
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames or []
                rows = [self._decode_row(dict(row)) for row in reader]
        if "processed" in fieldnames:
            for row in rows:
                if not row.get("processed"):
                    row["processed"] = "0"
        return rows

    def write_rows(self, path: Path, headers: Iterable[str], rows: Iterable[Dict[str, Any]]) -> None:
        headers_list = list(headers)
        self._ensure_parent(path)
        with FileLock(path):
            fd, tmp_name = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=headers_list)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(self._normalize_row(row, headers_list))
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def append_row(self, path: Path, headers: Iterable[str], row: Dict[str, Any]) -> None:
        headers_list = list(headers)
        self.ensure_file(path, headers_list)
        with FileLock(path):
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers_list)
                writer.writerow(self._normalize_row(row, headers_list))

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _normalize_row(self, row: Dict[str, Any], headers: Sequence[str]) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        for key in headers:
            value = row.get(key) if isinstance(row, dict) else None
            if key == "processed":
                text = self._stringify(value).strip()
                normalized[key] = text if text else "0"
            else:
                normalized[key] = self._escape_field(self._stringify(value))
        return normalized

    def _rewrite_locked(self, path: Path, headers: Sequence[str], rows: Iterable[Dict[str, Any]]) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(headers))
                writer.writeheader()
                for row in rows:
                    writer.writerow(self._normalize_row(row, writer.fieldnames))
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def load_config(path: Path, env_prefix: str = DEFAULT_ENV_PREFIX) -> AgentConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    data_cfg = raw.get("data", {})
    ai_cfg = raw.get("ai", {})
    general_cfg = raw.get("general", {})
    env_cfg = raw.get("env", {})

    prefix = env_cfg.get("prefix", env_prefix) or DEFAULT_ENV_PREFIX
    overrides = _extract_env_overrides(prefix)

    def pick_path(key: str, default_value: str) -> Path:
        override_value = overrides.get(key)
        if override_value is not None:
            return resolve_path(override_value)
        return resolve_path(default_value)

    data_root = pick_path("data.root", data_cfg.get("root", "data"))
    nodes_base = pick_path("data.nodes_base", data_cfg.get("nodes_base", str(data_root / "nodes")))
    prompts_dir = pick_path("data.prompts", data_cfg.get("prompts", "prompts"))
    logs_dir = pick_path("data.logs", data_cfg.get("logs", "logs"))
    personas_dir = pick_path("general.personas_dir", general_cfg.get("personas_dir", "config/personas"))

    poll_ms = float(overrides.get("ai.ai_poll_interval_ms", ai_cfg.get("ai_poll_interval_ms", ai_cfg.get("poll_interval_ms", 1000))))
    timezone = str(overrides.get("general.timezone", general_cfg.get("timezone", "UTC")))
    default_persona = str(overrides.get("ai.default_persona", ai_cfg.get("default_persona", DEFAULT_PERSONA_NAME)))
    ignore_default = ai_cfg.get("ignore_channel_indexes", [0])
    override_ignore = overrides.get("ai.ignore_channel_indexes")
    ignore_channel_indexes = _parse_int_list(override_ignore) if override_ignore is not None else _ensure_int_list(ignore_default)
    max_message_chars = int(overrides.get("ai.max_message_chars", ai_cfg.get("max_message_chars", 200)))
    max_context_chars = int(overrides.get("ai.max_context_chars", ai_cfg.get("max_context_chars", 2000)))
    reply_cooldown_seconds = int(overrides.get("ai.reply_cooldown_seconds", ai_cfg.get("reply_cooldown_seconds", 120)))
    enable_prompt_logs = bool(str(overrides.get("ai.enable_prompt_logs", ai_cfg.get("enable_prompt_logs", True))).lower() not in {"0", "false", "no"})

    ollama_cfg = raw.get("ollama", {})
    ollama_base_url = overrides.get("ollama.base_url", ollama_cfg.get("base_url", "http://localhost:11434"))
    ollama_model_instruct = overrides.get("ollama.model_instruct", ollama_cfg.get("model_instruct", "qwen3-4b-q8-instruct"))
    ollama_model_think = overrides.get("ollama.model_think", ollama_cfg.get("model_think")) or None

    return AgentConfig(
        data_root=data_root,
        nodes_base=nodes_base,
        prompts_dir=prompts_dir,
        logs_dir=logs_dir,
        personas_dir=personas_dir,
        ai_poll_interval=poll_ms / 1000.0,
        timezone=timezone,
        env_prefix=prefix,
        default_persona=default_persona,
        ignore_channel_indexes=ignore_channel_indexes,
        max_message_chars=max_message_chars,
        max_context_chars=max_context_chars,
        reply_cooldown_seconds=reply_cooldown_seconds,
        enable_prompt_logs=enable_prompt_logs,
        ollama_base_url=ollama_base_url,
        ollama_model_instruct=ollama_model_instruct,
        ollama_model_think=ollama_model_think,
    )


def _parse_int_list(raw: str) -> List[int]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        # Support JSON-like input, e.g. "[0,2]"
        val = json.loads(text)
        if isinstance(val, list):
            return [int(x) for x in val]
    except Exception:
        pass
    # Fallback: comma-separated list, e.g. "0,2, 4"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    result: List[int] = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            continue
    return result


def _ensure_int_list(val: Any) -> List[int]:
    if isinstance(val, list):
        out: List[int] = []
        for x in val:
            try:
                out.append(int(x))
            except Exception:
                continue
        return out
    try:
        return [int(val)]
    except Exception:
        return []


def _extract_env_overrides(prefix: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        stripped = key[len(prefix) :]
        lowered = stripped.lower().replace("__", ".")
        results[lowered] = value
    return results


@dataclass
class PersonaMatch:
    persona: "Persona"
    trigger: str
    command: Optional[str]
    remainder: str


@dataclass
class PersonaSnapshot:
    name: str
    system_prompt: str
    model: Optional[str]
    temperature: Optional[float]
    max_message_chars: int
    max_context_chars: int
    cooldown_seconds: int
    allow_channels: Optional[List[int]]
    block_channels: Optional[List[int]]


@dataclass
class LLMTask:
    thread_path: Path
    thread_type: str
    thread_key: str
    message_id: str
    sender_id: str
    prompt_text: str
    trigger: str
    persona_snapshot: PersonaSnapshot
    source_meta: Dict[str, Any]
    timestamp: str
    source_row: Dict[str, str]


class Persona:
    def __init__(
        self,
        path: Path,
        doc: Dict[str, Any],
        runtime: PersonaRuntime,
        head_text: str,
        comment_line: str,
    ) -> None:
        self.path = path
        self.name = doc.get("name", path.stem)
        triggers = doc.get("triggers") or [self.name]
        self.triggers = [str(t) for t in triggers]
        self._triggers_lower = [t.lower() for t in self.triggers]
        self.timezone = doc.get("timezone", "America/Los_Angeles")
        self.description = doc.get("description", "")
        self.model = doc.get("model")
        temp_raw = doc.get("temperature")
        try:
            self.temperature = float(temp_raw) if temp_raw is not None else None
        except (TypeError, ValueError):
            self.temperature = None
        self.max_message_chars = int(doc.get("max_message_chars", 0) or 0)
        self.max_context_chars = int(doc.get("max_context_chars", 0) or 0)
        self.cooldown_seconds = int(doc.get("cooldown_seconds", 0) or 0)
        self.rag = bool(doc.get("rag", False))
        self.tools = list(doc.get("tools", []))
        self.system_prompt = doc.get("system_prompt", "")
        self.allow_channels = _ensure_int_list(doc.get("allow_channels")) if doc.get("allow_channels") is not None else None
        self.block_channels = _ensure_int_list(doc.get("block_channels")) if doc.get("block_channels") is not None else None
        self.runtime = runtime
        self._head_text = head_text
        self._comment_line = (comment_line or "# Runtime fields (updated atomically by the agent; do not edit manually)").rstrip("\n")

    def to_snapshot(self) -> PersonaSnapshot:
        return PersonaSnapshot(
            name=self.name,
            system_prompt=(self.system_prompt or "").strip(),
            model=self.model,
            temperature=self.temperature,
            max_message_chars=self.max_message_chars,
            max_context_chars=self.max_context_chars,
            cooldown_seconds=self.cooldown_seconds,
            allow_channels=list(self.allow_channels) if self.allow_channels is not None else None,
            block_channels=list(self.block_channels) if self.block_channels is not None else None,
        )

    @property
    def triggers_lower(self) -> List[str]:
        return self._triggers_lower

    def get_zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    def refresh_today(self, now: dt.datetime) -> None:
        local_now = now.astimezone(self.get_zone())
        today = local_now.date().isoformat()
        if self.runtime.today_date != today:
            self.runtime.today_date = today
            self.runtime.today_calls = 0

    def mark_started(self, now: dt.datetime) -> None:
        local_now = now.astimezone(self.get_zone())
        self.runtime.running = True
        self.runtime.last_started = local_now.isoformat()

    def mark_stopped(self) -> None:
        self.runtime.running = False

    def increment_control(self) -> None:
        self.runtime.control_calls += 1

    def to_runtime_lines(self) -> List[str]:
        return [
            f"running = {'true' if self.runtime.running else 'false'}",
            f"total_calls = {int(self.runtime.total_calls)}",
            f"today_calls = {int(self.runtime.today_calls)}",
            f"today_date = \"{self.runtime.today_date}\"",
            f"last_started = \"{self.runtime.last_started}\"",
            f"control_calls = {int(self.runtime.control_calls)}",
            f"queue_count = {int(self.runtime.queue_count)}",
        ]

    def write_runtime(self) -> None:
        head = self._head_text or ""
        if head and not head.endswith("\n"):
            head = head + "\n"
        block_lines = [self._comment_line] + self.to_runtime_lines()
        new_text = head + "\n".join(block_lines) + "\n"
        with FileLock(self.path):
            fd, tmp_name = tempfile.mkstemp(prefix=self.path.stem, suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(new_text)
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        self._head_text = head

    def read_config_text(self) -> str:
        with FileLock(self.path):
            return self.path.read_text(encoding="utf-8")

    def status_summary(self, now: dt.datetime) -> str:
        local_now = now.astimezone(self.get_zone())
        tz_name = local_now.tzname() or self.timezone
        last_display = "never"
        if self.runtime.last_started:
            try:
                parsed = dt.datetime.fromisoformat(self.runtime.last_started)
                last_display = parsed.astimezone(self.get_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")
            except ValueError:
                last_display = self.runtime.last_started
        local_now = now.astimezone(self.get_zone())
        tz_name = local_now.tzname() or self.timezone
        last_display = "never"
        if self.runtime.last_started:
            try:
                parsed = dt.datetime.fromisoformat(self.runtime.last_started)
                last_display = parsed.astimezone(self.get_zone()).strftime("%Y-%m-%d %H:%M:%S %Z")
            except ValueError:
                last_display = self.runtime.last_started
        state = "running" if self.runtime.running else "stopped"
        prefix = local_now.strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"{prefix} {tz_name} | {self.name} is {state}. "
            f"Calls: {self.runtime.total_calls} total, {self.runtime.today_calls} today. "
            f"Last start: {last_display}."
        )


class PersonaRegistry:
    def __init__(self, personas_dir: Path, logger: logging.Logger, default_name: str) -> None:
        self._dir = personas_dir
        self._logger = logger
        self._default_name = default_name.lower()
        self._personas: Dict[str, Persona] = {}
        self.reload()

    @staticmethod
    def _split_head(text: str) -> Tuple[str, str]:
        comment_default = "# Runtime fields (updated atomically by the agent; do not edit manually)"
        lines = text.splitlines(keepends=True)
        head_parts: List[str] = []
        for line in lines:
            if line.strip().lower().startswith("# runtime fields"):
                comment = line.rstrip("\n") or comment_default
                return ("".join(head_parts), comment)
            head_parts.append(line)
        return ("".join(lines), comment_default)

    def reload(self) -> None:
        self._personas.clear()
        if not self._dir.exists():
            self._logger.warning("Personas directory missing: %s", self._dir)
            return
        for path in sorted(self._dir.glob("*.toml")):
            try:
                text = path.read_text(encoding="utf-8")
                doc = tomllib.loads(text)
                head_text, comment_line = self._split_head(text)
                runtime = PersonaRuntime(
                    running=bool(doc.get("running", False)),
                    total_calls=int(doc.get("total_calls", 0) or 0),
                    today_calls=int(doc.get("today_calls", 0) or 0),
                    today_date=str(doc.get("today_date", "")),
                    last_started=str(doc.get("last_started", "")),
                    control_calls=int(doc.get("control_calls", 0) or 0),
                    queue_count=int(doc.get("queue_count", 0) or 0),
                )
                persona = Persona(path=path, doc=doc, runtime=runtime, head_text=head_text, comment_line=comment_line)
                key = persona.name.lower()
                if key in self._personas:
                    self._logger.warning("Duplicate persona name detected: %s", persona.name)
                self._personas[key] = persona
            except Exception as exc:
                self._logger.error("Failed to load persona %s: %s", path, exc)

    def all(self) -> List[Persona]:
        return list(self._personas.values())

    def get_default(self) -> Optional[Persona]:
        return self._personas.get(self._default_name)

    def get_by_name(self, name: str) -> Optional[Persona]:
        return self._personas.get(name.lower())

    def find_by_trigger(self, token: str) -> Optional[Persona]:
        lowered = token.lower()
        for persona in self._personas.values():
            if lowered in persona.triggers_lower:
                return persona
        return None


class AIAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._stop_event = threading.Event()
        self._logger = logging.getLogger("ai_agent")
        self._logger.setLevel(logging.DEBUG)
        self._configure_logging()
        self._csv = CSVStore(self._logger)
        self._persona_lock = threading.Lock()
        self.personas = PersonaRegistry(config.personas_dir, self._logger, config.default_persona)
        self._logger.info("AI agent configured with nodes base %s", self.config.nodes_base)
        self._queue_counts: Dict[str, int] = defaultdict(int)
        self._queue_lock = threading.Lock()
        self._inflight: set[Tuple[str, str]] = set()
        self._inflight_lock = threading.Lock()
        self._thread_last_reply: Dict[Tuple[str, str, str], float] = {}
        self._thread_reply_lock = threading.Lock()
        self._llm_queue: "queue.Queue[Optional[LLMTask]]" = queue.Queue()
        self._ollama_client: Optional[Client] = None
        self._ollama_status_lock = threading.Lock()
        self._ollama_status: Dict[str, Any] = {"connected": None, "models": {}}
        self._worker_stop_sent = threading.Event()
        self._llm_thread = threading.Thread(target=self._llm_worker, name="ollama-worker", daemon=True)
        self._llm_thread.start()

    def _configure_logging(self) -> None:
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("log.%Y-%m-%d-%H-%M-%S-%f.agent.txt")
        log_path = self.config.logs_dir / timestamp
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._logger.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self._logger.addHandler(console)
        self.config.log_file = log_path
        self._logger.info("Logging to %s", log_path)

    def run(self, once: bool = False) -> None:
        self._logger.info("AI agent starting; once=%s", once)
        try:
            while not self._stop_event.is_set():
                self.scan_once()
                if once:
                    break
                time.sleep(self.config.ai_poll_interval)
        except KeyboardInterrupt:
            self._logger.info("Interrupted; shutting down")
        finally:
            self.stop()
            self._logger.info("AI agent stopped")

    def stop(self) -> None:
        already_set = self._stop_event.is_set()
        self._stop_event.set()
        if not self._worker_stop_sent.is_set():
            self._llm_queue.put(None)
            self._worker_stop_sent.set()
        if not already_set and self._llm_thread.is_alive():
            self._llm_thread.join(timeout=10.0)
        elif self._worker_stop_sent.is_set() and self._llm_thread.is_alive():
            self._llm_thread.join(timeout=10.0)

    def scan_once(self) -> None:
        self._logger.debug("Starting scan loop")
        with self._persona_lock:
            self.personas.reload()
            personas_snapshot = list(self.personas.all())
            self._logger.debug("Loaded %d personas", len(personas_snapshot))
            self._sync_queue_counts_locked(personas_snapshot)
        nodes_base = self.config.nodes_base
        if not nodes_base.exists():
            self._logger.debug("Nodes base %s does not exist; skipping", nodes_base)
            return
        for node_dir in sorted(p for p in nodes_base.iterdir() if p.is_dir()):
            self._logger.debug("Scanning node directory %s", node_dir)
            threads_dir = node_dir / "threads"
            self._process_thread_dir(threads_dir / "channels", "channel")
            self._process_thread_dir(threads_dir / "dms", "dm")
        self._logger.debug("Finished scan loop")

    def _process_thread_dir(self, base: Path, thread_type: str) -> None:
        if not base.exists():
            self._logger.debug("Thread directory %s missing; skipping", base)
            return
        self._logger.debug("Processing thread directory %s", base)
        for csv_path in sorted(p for p in base.glob("*.csv") if p.is_file()):
            self._logger.debug("Processing thread file %s", csv_path)
            try:
                self._process_thread_file(csv_path, thread_type)
            except Exception as exc:
                self._logger.exception("Failed to process thread file %s: %s", csv_path, exc)

    def _process_thread_file(self, path: Path, fallback_thread_type: str) -> None:
        self._csv.ensure_file(path, THREAD_HEADERS)
        rows = self._csv.read_rows(path)
        if not rows:
            self._logger.debug("Thread file %s empty; nothing to process", path)
            return
        modified = False
        queued_reply_rows: List[Dict[str, str]] = []
        for idx, row in enumerate(rows):
            direction = (row.get("direction") or "").lower()
            if direction != "inbound":
                self._logger.debug(
                    "Skipping row %s direction=%s in %s",
                    idx,
                    direction or "",
                    path,
                )
                continue
            thread_type = (row.get("thread_type") or fallback_thread_type)
            thread_key = row.get("thread_key") or path.stem
            try:
                source_meta = json.loads(row.get("meta_json") or "{}")
            except json.JSONDecodeError:
                source_meta = {}
            channel_index = None
            if thread_type == "channel":
                channel_index = self._safe_int(source_meta.get("channel_index"))
                if channel_index is not None and channel_index in (self.config.ignore_channel_indexes or []):
                    self._logger.info(
                        "Ignoring inbound row %s on channel index %s due to configuration",
                        idx,
                        channel_index,
                    )
                    continue
            processed_flag = (row.get("processed") or "0").strip()
            if processed_flag and processed_flag != "0":
                self._logger.debug(
                    "Row %s already processed (flag=%s) in %s",
                    idx,
                    processed_flag,
                    path,
                )
                continue
            message_id = self._message_id(path, row, idx)
            if self._already_replied(rows, message_id):
                self._logger.debug(
                    "Row %s message_id=%s already has a queued/outbound reply",
                    idx,
                    message_id,
                )
                continue
            match = self._detect_persona(row, fallback_thread_type)
            if not match:
                self._logger.debug(
                    "Row %s message_id=%s has no persona match; content preview=%r",
                    idx,
                    message_id,
                    (row.get("content") or "")[:120],
                )
                continue
            persona_name = match.persona.name
            if match.command:
                reply_rows = self._handle_control_command(path, row, message_id, match)
                if reply_rows:
                    queued_reply_rows.extend(reply_rows)
                    self._logger.info(
                        "Queued %d control replies for message_id=%s persona=%s",
                        len(reply_rows),
                        message_id,
                        persona_name,
                    )
                row["processed"] = "1"
                modified = True
                continue
            prompt_text = match.remainder.strip()
            if not prompt_text:
                prompt_text = (row.get("content") or "").strip()
            if not prompt_text:
                self._logger.debug(
                    "Skipping row %s message_id=%s due to empty prompt after trigger removal",
                    idx,
                    message_id,
                )
                continue
            if not self._persona_allows_message(match.persona, thread_type, thread_key, channel_index):
                self._logger.debug(
                    "Persona %s not eligible to reply on thread %s (channel_index=%s)",
                    persona_name,
                    thread_key,
                    channel_index,
                )
                continue
            if self._enqueue_llm_task(
                path,
                thread_type,
                thread_key,
                message_id,
                row,
                match,
                source_meta,
                prompt_text,
            ):
                self._logger.info(
                    "Queued LLM reply for message_id=%s persona=%s thread=%s",
                    message_id,
                    persona_name,
                    thread_key,
                )
            else:
                self._logger.debug(
                    "Skipping LLM queue for message_id=%s persona=%s (already pending)",
                    message_id,
                    persona_name,
                )
        if queued_reply_rows:
            rows.extend(queued_reply_rows)
            modified = True
            self._logger.debug("Appended %d queued replies to %s", len(queued_reply_rows), path)
        if modified:
            self._csv.write_rows(path, THREAD_HEADERS, rows)
            self._logger.info("Wrote updates to thread file %s", path)
        else:
            self._logger.debug("No modifications required for %s", path)

    def _persona_allows_message(
        self,
        persona: Persona,
        thread_type: str,
        thread_key: str,
        channel_index: Optional[int],
    ) -> bool:
        if not persona.runtime.running:
            return False
        if thread_type == "channel":
            if persona.allow_channels is not None and channel_index is not None and channel_index not in persona.allow_channels:
                return False
            if persona.block_channels is not None and channel_index is not None and channel_index in persona.block_channels:
                return False
        cooldown = persona.cooldown_seconds or self.config.reply_cooldown_seconds
        if cooldown > 0:
            key = (persona.name, thread_type, thread_key)
            with self._thread_reply_lock:
                last_ts = self._thread_last_reply.get(key)
            if last_ts and (time.time() - last_ts) < cooldown:
                return False
        return True

    def _enqueue_llm_task(
        self,
        thread_path: Path,
        thread_type: str,
        thread_key: str,
        message_id: str,
        row: Dict[str, str],
        match: PersonaMatch,
        source_meta: Dict[str, Any],
        prompt_text: str,
    ) -> bool:
        task_key = self._task_key(thread_path, message_id)
        with self._inflight_lock:
            if task_key in self._inflight:
                return False
            self._inflight.add(task_key)
        snapshot = match.persona.to_snapshot()
        task = LLMTask(
            thread_path=thread_path,
            thread_type=thread_type,
            thread_key=thread_key,
            message_id=message_id,
            sender_id=row.get("sender_id") or "",
            prompt_text=prompt_text,
            trigger=match.trigger,
            persona_snapshot=snapshot,
            source_meta=source_meta,
            timestamp=row.get("timestamp") or iso_now(),
            source_row=dict(row),
        )
        self._llm_queue.put(task)
        self._increment_queue_count(snapshot.name)
        return True

    def _task_key(self, thread_path: Path, message_id: str) -> Tuple[str, str]:
        return (str(thread_path.resolve()), message_id)

    def _record_thread_reply(self, persona_name: str, thread_type: str, thread_key: str) -> None:
        with self._thread_reply_lock:
            self._thread_last_reply[(persona_name, thread_type, thread_key)] = time.time()

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()

    @staticmethod
    def _chunk_response(text: str, limit: int) -> List[str]:
        limit = max(1, limit)
        remaining = text.strip()
        chunks: List[str] = []
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_pos = remaining.rfind(" ", 0, limit + 1)
            if split_pos <= 0:
                split_pos = limit
            chunk = remaining[:split_pos].rstrip()
            if not chunk:
                chunk = remaining[:limit].rstrip()
            chunks.append(chunk)
            remaining = remaining[len(chunk) :].lstrip()
        return chunks

    def _sync_queue_counts_locked(self, personas: Sequence[Persona]) -> None:
        names = {persona.name for persona in personas}
        with self._queue_lock:
            for persona in personas:
                self._queue_counts.setdefault(persona.name, 0)
            for name in list(self._queue_counts.keys()):
                if name not in names:
                    del self._queue_counts[name]
            snapshot_counts = {name: self._queue_counts.get(name, 0) for name in names}
        for persona in personas:
            desired = snapshot_counts.get(persona.name, 0)
            if persona.runtime.queue_count != desired:
                persona.runtime.queue_count = desired
                persona.write_runtime()

    def _set_persona_queue_count(self, persona_name: str, value: int) -> None:
        with self._persona_lock:
            persona = self.personas.get_by_name(persona_name)
            if persona is None:
                return
            if persona.runtime.queue_count != value:
                persona.runtime.queue_count = value
                persona.write_runtime()

    def _increment_queue_count(self, persona_name: str) -> None:
        with self._queue_lock:
            new_value = self._queue_counts.get(persona_name, 0) + 1
            self._queue_counts[persona_name] = new_value
        self._set_persona_queue_count(persona_name, new_value)

    def _decrement_queue_count(self, persona_name: str) -> None:
        with self._queue_lock:
            current = self._queue_counts.get(persona_name, 0)
            new_value = current - 1 if current > 0 else 0
            self._queue_counts[persona_name] = new_value
        self._set_persona_queue_count(persona_name, new_value)

    def _set_ollama_connected(self, value: Optional[bool]) -> None:
        with self._ollama_status_lock:
            self._ollama_status["connected"] = value

    def _set_model_status(self, model_name: str, status: str) -> None:
        if not model_name:
            return
        with self._ollama_status_lock:
            models_obj = self._ollama_status.get("models")
            if not isinstance(models_obj, dict):
                models_obj = {}
                self._ollama_status["models"] = models_obj
            models_obj[model_name] = status

    def _get_cached_ollama_status(self, required_models: Set[str]) -> Tuple[Optional[bool], Dict[str, str]]:
        with self._ollama_status_lock:
            connected = self._ollama_status.get("connected")
            models_obj = self._ollama_status.get("models")
            models_map = models_obj if isinstance(models_obj, dict) else {}
            snapshot = {model: models_map.get(model, "unknown") for model in required_models}
        return connected, snapshot

    def _required_models_for_persona(self, persona: Persona) -> Set[str]:
        models: Set[str] = set()
        default_model = self.config.ollama_model_instruct
        if default_model:
            models.add(default_model)
        if persona.model:
            models.add(persona.model)
        if self.config.ollama_model_think:
            models.add(self.config.ollama_model_think)
        return {model for model in models if model}

    def _probe_ollama(self, required_models: Set[str]) -> Tuple[bool, Dict[str, str]]:
        if not required_models:
            return True, {}
        try:
            client = Client(host=self.config.ollama_base_url)
            response = client.list()
            available: Set[str] = set()
            for entry in response.get("models", []):
                name = entry.get("name") or entry.get("model") or ""
                if not name:
                    continue
                available.add(name)
                base = name.split(":", 1)[0]
                if base:
                    available.add(base)
            result: Dict[str, str] = {}
            for model in required_models:
                result[model] = "available" if model in available else "missing"
            return True, result
        except Exception as exc:  # pragma: no cover - best effort probe
            self._logger.debug("Ollama probe failed: %s", exc)
            return False, {model: "unknown" for model in required_models}

    def _ollama_status_snapshot(self, persona: Persona) -> Tuple[Set[str], Optional[bool], Dict[str, str]]:
        required = self._required_models_for_persona(persona)
        connected, statuses = self._get_cached_ollama_status(required)
        needs_probe = connected is None or any(statuses.get(model, "unknown") in {"unknown", "missing", "error"} for model in required)
        if needs_probe:
            probe_connected, probe_statuses = self._probe_ollama(required)
            self._set_ollama_connected(probe_connected)
            for model, status in probe_statuses.items():
                current = statuses.get(model)
                if current == "downloading" and status in {"missing", "unknown"}:
                    continue
                self._set_model_status(model, status)
            connected, statuses = self._get_cached_ollama_status(required)
        return required, connected, statuses

    def _ollama_status_line(self, persona: Persona) -> str:
        required, connected, statuses = self._ollama_status_snapshot(persona)
        if not required:
            return "Ollama: not configured"
        if not connected:
            return "Ollama: offline | models unavailable"
        downloading = sorted(model for model, status in statuses.items() if status == "downloading")
        missing = sorted(model for model, status in statuses.items() if status in {"missing", "unknown", "error"})
        if not downloading and not missing:
            return "Ollama: connected | all required models ready"
        parts: List[str] = []
        if downloading:
            parts.append("downloading " + ", ".join(downloading))
        if missing:
            parts.append("missing " + ", ".join(missing))
        detail = "; ".join(parts) if parts else "status unknown"
        return f"Ollama: connected | {detail}"

    def _llm_worker(self) -> None:
        client: Optional[Client] = None
        connected = False
        validated_models: set[str] = set()
        while True:
            try:
                task = self._llm_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set() and self._worker_stop_sent.is_set():
                    break
                continue
            if task is None:
                self._llm_queue.task_done()
                break
            success = False
            try:
                if client is None:
                    client = Client(host=self.config.ollama_base_url)
                if not connected:
                    connected = self._validate_ollama_connection(client)
                if not connected:
                    raise RuntimeError("Unable to connect to Ollama server")
                model_name = task.persona_snapshot.model or self.config.ollama_model_instruct
                if model_name and model_name not in validated_models:
                    if self._ensure_model_available(client, model_name):
                        validated_models.add(model_name)
                    else:
                        raise RuntimeError(f"Model '{model_name}' not available")
                success = self._process_llm_task(task, client)
            except Exception as exc:  # pragma: no cover - best effort logging
                self._logger.exception(
                    "LLM task failed for persona=%s message=%s: %s",
                    task.persona_snapshot.name,
                    task.message_id,
                    exc,
                )
                self._set_ollama_connected(None)
            finally:
                self._finish_task(task, success)
                self._llm_queue.task_done()
        self._logger.debug("LLM worker exiting")

    def _finish_task(self, task: LLMTask, success: bool) -> None:
        key = self._task_key(task.thread_path, task.message_id)
        with self._inflight_lock:
            self._inflight.discard(key)
        self._decrement_queue_count(task.persona_snapshot.name)
        if success:
            self._record_thread_reply(task.persona_snapshot.name, task.thread_type, task.thread_key)

    def _validate_ollama_connection(self, client: Client) -> bool:
        try:
            self._logger.info("Connecting to Ollama at %s", self.config.ollama_base_url)
            client.list()
            self._logger.info("Connected to Ollama")
            self._set_ollama_connected(True)
            return True
        except Exception as exc:
            self._logger.error("Failed to connect to Ollama: %s", exc)
            self._set_ollama_connected(False)
            return False

    def _ensure_model_available(self, client: Client, model_name: str) -> bool:
        try:
            self._logger.info("Validating Ollama model '%s'", model_name)
            list_response = client.list()
            existing: set[str] = set()
            for model in list_response.get("models", []):
                name = model.get("name") or model.get("model") or ""
                if name:
                    existing.add(name)
                    base = name.split(":", 1)[0]
                    existing.add(base)
            if model_name in existing:
                self._logger.info("Ollama model '%s' already present", model_name)
                self._set_model_status(model_name, "available")
                return True
            self._logger.info("Pulling Ollama model '%s'", model_name)
            self._set_model_status(model_name, "downloading")
            last_status = None
            for chunk in client.pull(model_name, stream=True):
                status = chunk.get("status")
                if status and status != last_status:
                    self._logger.info("ollama pull %s: %s", model_name, status)
                    last_status = status
                detail = chunk.get("detail")
                if detail:
                    self._logger.debug("ollama pull %s detail: %s", model_name, detail)
            self._logger.info("Completed pull for model '%s'", model_name)
            self._set_model_status(model_name, "available")
            return True
        except Exception as exc:
            self._logger.error("Failed to ensure model '%s': %s", model_name, exc)
            self._set_model_status(model_name, "error")
            return False

    def _process_llm_task(self, task: LLMTask, client: Client) -> bool:
        with self._persona_lock:
            persona = self.personas.get_by_name(task.persona_snapshot.name)
            if persona is None:
                self._logger.warning(
                    "Persona %s missing while processing message %s",
                    task.persona_snapshot.name,
                    task.message_id,
                )
                return False
            persona_running = persona.runtime.running
        if not persona_running:
            self._logger.info(
                "Persona %s is stopped; postponing message %s",
                task.persona_snapshot.name,
                task.message_id,
            )
            return False
        model = task.persona_snapshot.model or self.config.ollama_model_instruct
        if not model:
            self._logger.error("No Ollama model configured for persona %s", task.persona_snapshot.name)
            return False
        options: Dict[str, Any] = {}
        if task.persona_snapshot.temperature is not None:
            options["temperature"] = task.persona_snapshot.temperature
        start_ts = time.time()
        result = client.generate(
            model=model,
            prompt=task.prompt_text,
            system=task.persona_snapshot.system_prompt or None,
            options=options or None,
            stream=False,
        )
        response_text = (result.get("response") or "").strip()
        if not response_text:
            self._logger.warning(
                "Empty response from Ollama for persona %s message %s",
                task.persona_snapshot.name,
                task.message_id,
            )
            return False
        response_text = self._strip_think_blocks(response_text)
        if not response_text:
            self._logger.warning(
                "Response from Ollama for persona %s message %s contained only <think> content",
                task.persona_snapshot.name,
                task.message_id,
            )
            return False
        duration_ms = int((time.time() - start_ts) * 1000)
        limit = task.persona_snapshot.max_message_chars or self.config.max_message_chars or 200
        if limit <= 0:
            limit = 200
        chunks = self._chunk_response(response_text, limit)
        if not chunks:
            self._logger.warning(
                "Response from Ollama for persona %s message %s produced no chunks",
                task.persona_snapshot.name,
                task.message_id,
            )
            return False
        reply_meta: Dict[str, Any] = {
            "reply_type": "llm",
            "model": model,
            "trigger": task.trigger,
            "source_message_id": task.message_id,
            "duration_ms": duration_ms,
        }
        if task.persona_snapshot.temperature is not None:
            reply_meta["temperature"] = task.persona_snapshot.temperature
        rows = self._csv.read_rows(task.thread_path)
        found = False
        for row in rows:
            if row.get("message_id") == task.message_id:
                row["processed"] = "1"
                found = True
                break
        if not found:
            self._logger.warning(
                "Source message %s not found in %s during reply write",
                task.message_id,
                task.thread_path,
            )
        with self._persona_lock:
            now = dt.datetime.now(dt.timezone.utc)
            persona = self.personas.get_by_name(task.persona_snapshot.name)
            if persona is None:
                self._logger.warning(
                    "Persona %s disappeared before reply write",
                    task.persona_snapshot.name,
                )
                return False
            persona.refresh_today(now)
            persona.runtime.total_calls += 1
            persona.runtime.today_calls += 1
        match = PersonaMatch(persona=persona, trigger=task.trigger, command=None, remainder=task.prompt_text)
        total_chunks = len(chunks)
        for idx, chunk_text in enumerate(chunks, start=1):
            chunk_meta = {**reply_meta, "chunk_index": idx, "chunk_total": total_chunks}
            rows.append(
                self._build_reply_row(
                    task.thread_path,
                    task.source_row,
                    task.message_id,
                    persona,
                    match,
                    (chunk_text, chunk_meta),
                    task.source_meta,
                )
            )
        self._csv.write_rows(task.thread_path, THREAD_HEADERS, rows)
        with self._persona_lock:
            persona.write_runtime()
        self._logger.info(
            "Prepared LLM reply for persona=%s thread=%s message=%s",
            task.persona_snapshot.name,
            task.thread_key,
            task.message_id,
        )
        return True

    def _message_id(self, path: Path, row: Dict[str, str], idx: int) -> str:
        message_id = row.get("message_id") or ""
        if message_id:
            return message_id
        payload = "|".join([path.stem, row.get("thread_key", ""), row.get("timestamp", ""), row.get("content", "")])
        ts_part = self._timestamp_from_row(row).strftime("%Y%m%d%H%M%S")
        return f"gen_{path.stem}_{idx}_{ts_part}"

    @staticmethod
    def _generate_reply_id() -> str:
        """Generate a random 32-bit unsigned integer as a string for a new reply."""
        return str(random.randint(0, 2**32 - 1))

    @staticmethod
    def _timestamp_from_row(row: Dict[str, str]) -> dt.datetime:
        try:
            ts = row.get("timestamp") or ""
            if len(ts) >= 19:
                return dt.datetime.fromisoformat(ts[:19])
        except Exception:
            pass
        return dt.datetime.now(dt.timezone.utc)

    @staticmethod
    def _already_replied(rows: List[Dict[str, str]], source_message_id: str) -> bool:
        """Check if a reply for the given source message ID already exists."""
        for r in rows:
            if r.get("reply_to_id") == source_message_id:
                return True
        return False

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _detect_persona(self, row: Dict[str, str], fallback_thread_type: str) -> Optional[PersonaMatch]:
        """Detect which persona, if any, should respond to a message."""
        content = (row.get("content") or "").strip()
        if not content:
            return None
        tokens = content.split(None, 2)
        if not tokens:
            return None
        first = tokens[0]
        with self._persona_lock:
            persona = self.personas.find_by_trigger(first)
        if persona is None:
            if (row.get("thread_type") or fallback_thread_type) == "dm":
                # Default persona reserved for non-control replies; not handled yet.
                return None
            return None
        if len(tokens) == 1:
            return PersonaMatch(persona=persona, trigger=first, command=None, remainder="")
        second = tokens[1]
        command = second.lower()
        if command in CONTROL_COMMANDS:
            remainder = tokens[2] if len(tokens) > 2 else ""
            return PersonaMatch(persona=persona, trigger=first, command=command, remainder=remainder)
        remainder = " ".join(tokens[1:])
        return PersonaMatch(persona=persona, trigger=first, command=None, remainder=remainder)

    def _handle_control_command(
        self,
        thread_path: Path,
        source_row: Dict[str, str],
        source_message_id: str,
        match: PersonaMatch,
    ) -> List[Dict[str, str]]:
        now = dt.datetime.now(dt.timezone.utc)
        persona = match.persona
        persona.refresh_today(now)
        persona.increment_control()
        command = match.command or ""
        replies: List[Tuple[str, Dict[str, Any]]] = []
        if command == "start":
            persona.mark_started(now)
            message = f"{persona.name} is now running."
            replies.append((message, {"reply_type": "control", "control_command": command}))
        elif command == "stop":
            persona.mark_stopped()
            message = f"{persona.name} is now stopped."
            replies.append((message, {"reply_type": "control", "control_command": command}))
        elif command == "status":
            summary = persona.status_summary(now)
            ollama_line = self._ollama_status_line(persona)
            message = summary if not ollama_line else f"{summary}\n{ollama_line}"
            replies.append((message, {"reply_type": "control", "control_command": command}))
        elif command == "config":
            config_text = persona.read_config_text().strip()
            max_chars = persona.max_message_chars or 200
            chunks = self._split_config_chunks(persona.name, config_text, max_chars)
            for idx, chunk in enumerate(chunks, start=1):
                replies.append(
                    (
                        chunk,
                        {
                            "reply_type": "control",
                            "control_command": command,
                            "chunk_index": idx,
                            "chunk_count": len(chunks),
                        },
                    )
                )
        elif command == "help":
            replies.append(
                (
                    f"{persona.name} help: https://github.com/High-Desert-Institute/Meshtastic-LLM",
                    {"reply_type": "control", "control_command": command},
                )
            )
        else:
            self._logger.debug("Unhandled control command '%s' for persona %s", command, persona.name)
            return []
        persona.write_runtime()
        reply_rows: List[Dict[str, str]] = []
        try:
            source_meta = json.loads(source_row.get("meta_json") or "{}")
        except json.JSONDecodeError:
            source_meta = {}
        for reply in replies:
            reply_rows.append(
                self._build_reply_row(thread_path, source_row, source_message_id, persona, match, reply, source_meta)
            )
        return reply_rows

    def _build_reply_row(
        self,
        thread_path: Path,
        source_row: Dict[str, str],
        source_message_id: str,
        persona: Persona,
        match: PersonaMatch,
        reply: Tuple[str, Dict[str, Any]],
        source_meta: Dict[str, Any],
    ) -> Dict[str, str]:
        content, meta = reply
        thread_type = (source_row.get("thread_type") or "").lower() or ("channel" if "channels" in thread_path.parts else "dm")
        thread_key = source_row.get("thread_key") or thread_path.stem
        
        final_meta = source_meta.copy()
        final_meta.update({
            "persona": persona.name,
            "trigger": match.trigger,
            "source_message_id": source_message_id,
            **meta,
        })

        chunk_total = int(final_meta.get("chunk_total", final_meta.get("chunk_count", 1)) or 1)
        chunk_index = int(final_meta.get("chunk_index", 1) or 1)
        base_prefix = f"-"
        prefix = base_prefix if chunk_total <= 1 else f"{base_prefix} ({chunk_index}/{chunk_total})"
        content = f"{prefix} {content}" if content else prefix
        row: Dict[str, str] = {
            "processed": "0",
            "thread_type": thread_type,
            "thread_key": thread_key,
            "message_id": self._generate_reply_id(),
            "direction": "queued",
            "sender_id": persona.name,
            "reply_to_id": source_message_id,
            "timestamp": iso_now(),
            "content": content,
            "send_attempts": "0",
            "send_status": "",
            "meta_json": dump_meta(final_meta),
        }
        self._logger.info(
            "Prepared control reply command=%s persona=%s thread=%s",
            meta.get("control_command"),
            persona.name,
            thread_key,
        )
        return row

    @staticmethod
    def _split_config_chunks(persona_name: str, text: str, limit: int) -> List[str]:
        limit = max(limit, 80)
        header_template = f"{persona_name} config (000/000):\n"
        body_limit = max(limit - len(header_template), 40)
        bodies = _chunk_config_body(text, body_limit)
        total = max(len(bodies), 1)
        chunks: List[str] = []
        for idx, body in enumerate(bodies or [""], start=1):
            header = f"{persona_name} config ({idx}/{total}):\n"
            chunks.append(header + body)
        return chunks or [f"{persona_name} config (1/1):"]


def _chunk_config_body(text: str, limit: int) -> List[str]:
    if limit <= 0:
        return [text]
    if not text:
        return [""]
    pieces: List[str] = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) <= limit:
            current += line
        else:
            if current:
                pieces.append(current.rstrip("\n"))
                current = ""
            while len(line) > limit:
                pieces.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        pieces.append(current.rstrip("\n"))
    return pieces or [""]


def print_status(config: AgentConfig, registry: PersonaRegistry) -> None:
    print(f"Data root: {config.data_root}")
    print(f"Nodes base: {config.nodes_base}")
    print(f"Logs dir: {config.logs_dir}")
    print(f"Personas dir: {config.personas_dir}")
    default_persona = registry.get_default()
    if default_persona is None:
        print("Default persona not found.")
    else:
        print(f"Default persona: {default_persona.name}")
    print("Personas:")
    now = dt.datetime.now(dt.timezone.utc)
    for persona in registry.all():
        summary = persona.status_summary(now)
        print(f"- {summary}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI agent for Meshtastic-LLM")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to TOML config file")
    parser.add_argument("--status", action="store_true", help="Print persona status and exit")
    parser.add_argument("--once", action="store_true", help="Run a single scan iteration and exit")
    parser.add_argument("--log-dir", type=Path, help="Override log directory location")
    parser.add_argument("--personas-dir", type=Path, help="Override personas directory location")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.log_dir:
        config.logs_dir = resolve_path(args.log_dir)
    if args.personas_dir:
        config.personas_dir = resolve_path(args.personas_dir)

    if args.status:
        logger = logging.getLogger("ai_agent_status")
        logger.setLevel(logging.WARNING)
        registry = PersonaRegistry(config.personas_dir, logger, config.default_persona)
        print_status(config, registry)
        return

    agent = AIAgent(config)
    agent.run(once=args.once)


if __name__ == "__main__":
    main()
