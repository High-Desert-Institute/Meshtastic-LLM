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
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    log_file: Optional[Path] = None


@dataclass
class PersonaRuntime:
    running: bool = False
    total_calls: int = 0
    today_calls: int = 0
    today_date: str = ""
    last_started: str = ""
    control_calls: int = 0


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

    def ensure_file(self, path: Path, headers: Iterable[str]) -> None:
        headers_list = list(headers)
        self._ensure_parent(path)
        with FileLock(path):
            if path.exists():
                with path.open("r", newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    existing_headers = reader.fieldnames or []
                    rows = [dict(row) for row in reader]
                if existing_headers == headers_list:
                    return
                normalized = [self._normalize_row(row, headers_list) for row in rows]
                self._rewrite_locked(path, headers_list, normalized)
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
                rows = [dict(row) for row in reader]
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
                normalized[key] = self._stringify(value)
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
    )


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
        self.temperature = doc.get("temperature")
        self.max_message_chars = int(doc.get("max_message_chars", 0) or 0)
        self.max_context_chars = int(doc.get("max_context_chars", 0) or 0)
        self.cooldown_seconds = int(doc.get("cooldown_seconds", 0) or 0)
        self.rag = bool(doc.get("rag", False))
        self.tools = list(doc.get("tools", []))
        self.system_prompt = doc.get("system_prompt", "")
        self.runtime = runtime
        self._head_text = head_text
        self._comment_line = (comment_line or "# Runtime fields (updated atomically by the agent; do not edit manually)").rstrip("\n")

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
        state = "running" if self.runtime.running else "stopped"
        prefix = local_now.strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"{prefix} {tz_name} — {self.name} {state}; total={self.runtime.total_calls}; "
            f"today={self.runtime.today_calls}; last_started={last_display}"
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
        self._logger.setLevel(logging.INFO)
        self._configure_logging()
        self._csv = CSVStore(self._logger)
        self.personas = PersonaRegistry(config.personas_dir, self._logger, config.default_persona)
        self._logger.info("AI agent configured with nodes base %s", self.config.nodes_base)

    def _configure_logging(self) -> None:
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("log.%Y-%m-%d-%H-%M-%S-%f.txt")
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
            self._logger.info("AI agent stopped")

    def stop(self) -> None:
        self._stop_event.set()

    def scan_once(self) -> None:
        nodes_base = self.config.nodes_base
        if not nodes_base.exists():
            return
        for node_dir in sorted(p for p in nodes_base.iterdir() if p.is_dir()):
            threads_dir = node_dir / "threads"
            self._process_thread_dir(threads_dir / "channels", "channel")
            self._process_thread_dir(threads_dir / "dms", "dm")

    def _process_thread_dir(self, base: Path, thread_type: str) -> None:
        if not base.exists():
            return
        for csv_path in sorted(p for p in base.glob("*.csv") if p.is_file()):
            try:
                self._process_thread_file(csv_path, thread_type)
            except Exception as exc:
                self._logger.exception("Failed to process thread file %s: %s", csv_path, exc)

    def _process_thread_file(self, path: Path, fallback_thread_type: str) -> None:
        self._csv.ensure_file(path, THREAD_HEADERS)
        rows = self._csv.read_rows(path)
        if not rows:
            return
        for idx, row in enumerate(rows):
            direction = (row.get("direction") or "").lower()
            if direction != "inbound":
                continue
            processed_flag = (row.get("processed") or "0").strip()
            if processed_flag and processed_flag != "0":
                continue
            message_id = self._message_id(path, row, idx)
            if self._already_replied(rows, message_id):
                continue
            match = self._detect_persona(row, fallback_thread_type)
            if not match:
                continue
            if match.command:
                self._handle_control_command(path, row, message_id, match)
                continue
            # Future: LLM handling will go here.

    def _message_id(self, path: Path, row: Dict[str, str], idx: int) -> str:
        message_id = row.get("message_id") or ""
        if message_id:
            return message_id
        payload = "|".join([path.stem, row.get("thread_key", ""), row.get("timestamp", ""), row.get("content", "")])
        return uuid.uuid5(uuid.NAMESPACE_URL, payload).hex

    @staticmethod
    def _already_replied(rows: Sequence[Dict[str, str]], message_id: str) -> bool:
        for candidate in rows:
            if (candidate.get("reply_to_id") or "") == message_id:
                if (candidate.get("direction") or "").lower() in {"queued", "outbound"}:
                    return True
        return False

    def _detect_persona(self, row: Dict[str, str], fallback_thread_type: str) -> Optional[PersonaMatch]:
        content_raw = row.get("content") or ""
        content = content_raw.strip()
        if not content:
            return None
        tokens = content.split()
        if not tokens:
            return None
        first = tokens[0]
        persona = self.personas.find_by_trigger(first)
        if persona is None:
            if (row.get("thread_type") or fallback_thread_type) == "dm":
                # Default persona reserved for non-control replies; not handled yet.
                return None
            return None
        remainder = content[len(first) :].lstrip()
        if not remainder:
            return PersonaMatch(persona=persona, trigger=first, command=None, remainder="")
        second_token = remainder.split(None, 1)[0]
        rest = remainder[len(second_token) :].lstrip()
        command = second_token.lower()
        if command in CONTROL_COMMANDS:
            return PersonaMatch(persona=persona, trigger=first, command=command, remainder=rest)
        return PersonaMatch(persona=persona, trigger=first, command=None, remainder=remainder)

    def _handle_control_command(
        self,
        thread_path: Path,
        source_row: Dict[str, str],
        source_message_id: str,
        match: PersonaMatch,
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        persona = match.persona
        persona.refresh_today(now)
        persona.increment_control()
        command = match.command or ""
        replies: List[Tuple[str, Dict[str, Any]]] = []
        if command == "start":
            if persona.runtime.running:
                message = f"{persona.name} already running."
            else:
                persona.mark_started(now)
                message = f"{persona.name} running." if persona.runtime.last_started else f"{persona.name} started."
            replies.append((message, {"reply_type": "control", "control_command": command}))
        elif command == "stop":
            if not persona.runtime.running:
                message = f"{persona.name} already stopped."
            else:
                persona.mark_stopped()
                message = f"{persona.name} stopped."
            replies.append((message, {"reply_type": "control", "control_command": command}))
        elif command == "status":
            summary = persona.status_summary(now)
            replies.append((summary, {"reply_type": "control", "control_command": command}))
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
            return
        persona.write_runtime()
        for reply in replies:
            self._enqueue_reply(thread_path, source_row, source_message_id, persona, match, reply)

    def _enqueue_reply(
        self,
        thread_path: Path,
        source_row: Dict[str, str],
        source_message_id: str,
        persona: Persona,
        match: PersonaMatch,
        reply: Tuple[str, Dict[str, Any]],
    ) -> None:
        content, meta = reply
        thread_type = (source_row.get("thread_type") or "").lower() or ("channel" if "channels" in thread_path.parts else "dm")
        thread_key = source_row.get("thread_key") or thread_path.stem
        row = {
            "processed": "0",
            "thread_type": thread_type,
            "thread_key": thread_key,
            "message_id": uuid.uuid4().hex,
            "direction": "queued",
            "sender_id": persona.name,
            "reply_to_id": source_message_id,
            "timestamp": iso_now(),
            "content": content,
            "send_attempts": "0",
            "send_status": "",
            "meta_json": dump_meta(
                {
                    "persona": persona.name,
                    "trigger": match.trigger,
                    "source_message_id": source_message_id,
                    **meta,
                }
            ),
        }
        self._csv.append_row(thread_path, THREAD_HEADERS, row)
        self._logger.info(
            "Queued control reply command=%s persona=%s thread=%s", meta.get("control_command"), persona.name, thread_key
        )

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
