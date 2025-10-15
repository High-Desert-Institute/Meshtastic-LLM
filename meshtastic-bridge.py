#!/usr/bin/env python3
"""Meshtastic bridge process for Meshtastic-LLM."""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Python 3.11 or newer is required to run this script") from exc

try:
    from meshtastic import portnums_pb2 as portnums
    from meshtastic.serial_interface import SerialInterface
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("The meshtastic python package is required: pip install meshtastic") from exc

try:
    from pubsub import pub
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("The pubsub package is required (installed via meshtastic dependency)") from exc

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config" / "default.toml"
DEFAULT_ENV_PREFIX = "MESHTASTIC_LLM_"

NODES_HEADERS = ["node_id", "short_name", "long_name", "first_seen_at", "last_seen_at"]
SIGHTINGS_HEADERS = [
    "node_id",
    "latitude",
    "longitude",
    "rssi",
    "telemetry_json",
    "observed_at",
    "sighting_hash",
]
THREAD_HEADERS = [
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

MAX_SEND_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 5


@dataclasses.dataclass
class BridgeConfig:
    data_root: Path
    nodes_base: Path
    prompts_dir: Path
    logs_dir: Path
    bridge_poll_interval: float
    node_uid_strategy: str
    node_uid_override: Optional[str]
    timezone: str
    env_prefix: str
    serial_port: Optional[str]
    log_file: Optional[Path] = None
    test_mode: bool = False


@dataclasses.dataclass
class NodePaths:
    node_uid: str
    base_dir: Path
    nodes_csv: Path
    sightings_csv: Path
    channels_dir: Path
    dms_dir: Path


class FileLock:
    def __init__(self, target: Path, timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        self._lock_path = target.with_suffix(target.suffix + ".lock") if target.suffix else Path(str(target) + ".lock")
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        start = time.time()
        while True:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode("ascii"))
                self._fd = fd
                return
            except FileExistsError:
                if time.time() - start >= self._timeout:
                    raise TimeoutError(f"Timed out waiting for lock {self._lock_path}")
                time.sleep(self._poll_interval)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass

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
        if path.exists():
            return
        self._ensure_parent(path)
        with FileLock(path):
            if path.exists():
                return
            with tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(path.parent)) as tmp:
                writer = csv.writer(tmp)
                writer.writerow(list(headers))
                temp_path = Path(tmp.name)
            temp_path.replace(path)

    def read_rows(self, path: Path) -> List[Dict[str, str]]:
        if not path.exists():
            return []
        with FileLock(path):
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                return [dict(row) for row in reader]

    def write_rows(self, path: Path, headers: Iterable[str], rows: Iterable[Dict[str, Any]]) -> None:
        self._ensure_parent(path)
        with FileLock(path):
            with tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
                writer = csv.DictWriter(tmp, fieldnames=list(headers))
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in writer.fieldnames})
                temp_path = Path(tmp.name)
            temp_path.replace(path)

    def append_row(self, path: Path, headers: Iterable[str], row: Dict[str, Any]) -> None:
        headers_list = list(headers)
        existing = self.read_rows(path)
        existing.append({key: row.get(key, "") for key in headers_list})
        self.write_rows(path, headers_list, existing)


def load_config(path: Path, env_prefix: str = DEFAULT_ENV_PREFIX) -> BridgeConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    data_cfg = raw.get("data", {})
    meshtastic_cfg = raw.get("meshtastic", {})
    general_cfg = raw.get("general", {})
    env_cfg = raw.get("env", {})

    prefix = env_cfg.get("prefix", env_prefix) or DEFAULT_ENV_PREFIX
    overrides = _extract_env_overrides(prefix=prefix)

    def _path_from(value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
        return candidate

    data_root = _path_from(overrides.get("data.root", data_cfg.get("root", "data")))
    nodes_base = _path_from(overrides.get("data.nodes_base", data_cfg.get("nodes_base", str(data_root / "nodes"))))
    prompts_dir = _path_from(overrides.get("data.prompts", data_cfg.get("prompts", "prompts")))
    logs_dir = _path_from(overrides.get("data.logs", data_cfg.get("logs", "logs")))

    poll_ms = float(overrides.get("meshtastic.bridge_poll_interval_ms", meshtastic_cfg.get("bridge_poll_interval_ms", 500)))
    node_uid_strategy = str(overrides.get("general.node_uid_strategy", general_cfg.get("node_uid_strategy", "auto")))
    node_uid_override = overrides.get("general.node_uid", general_cfg.get("node_uid"))
    timezone = str(overrides.get("general.timezone", general_cfg.get("timezone", "UTC")))
    serial_port = overrides.get("meshtastic.serial_port", meshtastic_cfg.get("serial_port"))

    return BridgeConfig(
        data_root=data_root,
        nodes_base=nodes_base,
        prompts_dir=prompts_dir,
        logs_dir=logs_dir,
        bridge_poll_interval=poll_ms / 1000.0,
        node_uid_strategy=node_uid_strategy,
        node_uid_override=node_uid_override,
        timezone=timezone,
        env_prefix=prefix,
        serial_port=serial_port,
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


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sanitize_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "unnamed"


def load_meta(row: Dict[str, str]) -> Dict[str, Any]:
    raw = row.get("meta_json") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def dump_meta(meta: Dict[str, Any]) -> str:
    return json.dumps(meta, separators=(",", ":"), ensure_ascii=True)


class MeshtasticBridge:
    def __init__(
        self,
        config: BridgeConfig,
        interface_factory: Optional[Callable[[], SerialInterface]] = None,
        cli_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.config = config
        self._stop_event = threading.Event()
        self._logger = logging.getLogger("meshtastic_bridge")
        self._logger.setLevel(logging.INFO)
        self._interface_factory = interface_factory
        self._cli_args = cli_args or {}
        self._log_path: Optional[Path] = None
        self._configure_logging()
        self._csv = CSVStore(self._logger)
        self._interface = None
        self._node_paths = None
        self._subscriptions = []

    def _configure_logging(self) -> None:
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("log.%Y-%m-%d-%H-%M-%S-%f.txt")
        log_path = self.config.logs_dir / timestamp
        self._log_path = log_path
        self.config.log_file = log_path
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)
        self._logger.handlers.clear()
        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)
        serializable_cli_args = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in self._cli_args.items()
        }
        run_meta = {
            "log_file": str(log_path),
            "data_root": str(self.config.data_root),
            "nodes_base": str(self.config.nodes_base),
            "serial_port": self.config.serial_port,
            "test_mode": self.config.test_mode,
            "cli_args": serializable_cli_args,
        }
        self._logger.info("bridge_run_metadata=%s", json.dumps(run_meta, sort_keys=True))

    def start(self) -> None:
        self._logger.info("Starting meshtastic bridge")
        self._connect_interface()
        self._ensure_node_paths()
        self._register_listeners()

    def stop(self) -> None:
        self._logger.info("Stopping meshtastic bridge")
        self._stop_event.set()
        self._remove_listeners()
        if self._interface:
            try:
                self._interface.close()
            except Exception as exc:  # pragma: no cover
                self._logger.warning("Error closing interface: %s", exc)
        self._interface = None

    def run(self) -> None:
        self.start()
        try:
            while not self._stop_event.is_set():
                self._flush_outbound_queue()
                time.sleep(self.config.bridge_poll_interval)
        except KeyboardInterrupt:
            self._logger.info("Keyboard interrupt received; shutting down")
        finally:
            self.stop()

    def _connect_interface(self) -> None:
        if self._interface_factory is not None:
            self._interface = self._interface_factory()
            self._logger.info(
                "Connected using injected Meshtastic interface %s", type(self._interface).__name__
            )
            return
        kwargs = {}
        if self.config.serial_port:
            kwargs["devPath"] = self.config.serial_port
        self._interface = SerialInterface(**kwargs)
        self._logger.info("Connected to Meshtastic device")

    def _resolve_node_uid(self) -> str:
        if self.config.node_uid_override:
            return sanitize_name(self.config.node_uid_override)
        strategy = self.config.node_uid_strategy.lower()
        if strategy == "config":
            raise RuntimeError("node_uid_strategy=config requires general.node_uid to be set")
        assert self._interface is not None
        try:
            info = self._interface.getMyNodeInfo()
            if not info:
                wait_for_config = getattr(self._interface, "waitForConfig", None)
                if callable(wait_for_config):
                    self._logger.info("Waiting for node identity from Meshtastic interface")
                    try:
                        wait_for_config()
                    except Exception as waited_exc:  # pragma: no cover
                        self._logger.warning("waitForConfig failed: %s", waited_exc)
                    info = self._interface.getMyNodeInfo()
            if info:
                for key in ("userId", "nodeId", "longName", "shortName"):
                    value = info.get(key)
                    if value:
                        return sanitize_name(str(value))
                user_info = info.get("user")
                if isinstance(user_info, dict):
                    for key in ("id", "longName", "shortName", "userId"):
                        value = user_info.get(key)
                        if value:
                            return sanitize_name(str(value))
                self._logger.warning(
                    "Meshtastic node info missing expected identity keys: %s",
                    sorted(info.keys()),
                )
        except Exception as exc:
            self._logger.warning("Could not resolve node uid from interface: %s", exc)
        return "node"

    def _ensure_node_paths(self) -> None:
        node_uid = self._resolve_node_uid()
        base_dir = self.config.nodes_base / node_uid
        paths = NodePaths(
            node_uid=node_uid,
            base_dir=base_dir,
            nodes_csv=base_dir / "nodes.csv",
            sightings_csv=base_dir / "sightings.csv",
            channels_dir=base_dir / "threads" / "channels",
            dms_dir=base_dir / "threads" / "dms",
        )
        paths.channels_dir.mkdir(parents=True, exist_ok=True)
        paths.dms_dir.mkdir(parents=True, exist_ok=True)
        self._csv.ensure_file(paths.nodes_csv, NODES_HEADERS)
        self._csv.ensure_file(paths.sightings_csv, SIGHTINGS_HEADERS)
        self._node_paths = paths
        self._logger.info("Initialized node directories at %s", paths.base_dir)

    def _register_listeners(self) -> None:
        self._subscribe("meshtastic.receive", self._handle_receive_event)
        self._subscribe(
            "meshtastic.connection.established", self._handle_connection_established
        )
        self._subscribe("meshtastic.connection.lost", self._handle_connection_lost)

    def _remove_listeners(self) -> None:
        while self._subscriptions:
            topic, handler = self._subscriptions.pop()
            try:
                pub.unsubscribe(handler, topic)
            except Exception as exc:  # pragma: no cover
                self._logger.debug("Failed to unsubscribe %s: %s", topic, exc)

    def _subscribe(self, topic: str, handler: Callable[..., Any]) -> None:
        pub.subscribe(handler, topic)
        self._subscriptions.append((topic, handler))

    def _handle_receive_event(
        self,
        packet: Dict[str, Any],
        interface: Any,
        topic: Any = None,
    ) -> None:
        # Topic argument is provided by pubsub; we ignore it aside from optional tracing.
        _ = topic
        self._on_packet(packet)

    def _handle_connection_established(self, interface: Any, topic: Any = None) -> None:
        _ = topic
        self._logger.info("Meshtastic connection established: %s", interface)

    def _handle_connection_lost(self, interface: Any, topic: Any = None) -> None:
        _ = topic
        self._logger.warning("Meshtastic connection lost: %s", interface)

    def _packet_timestamp(self, packet: Dict[str, Any]) -> str:
        value = packet.get("rxTime") or packet.get("timestamp")
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat()
        return iso_now()

    def _on_packet(self, packet: Dict[str, Any]) -> None:
        try:
            decoded = packet.get("decoded", {})
            portnum_raw = decoded.get("portnum")
            portnum_name = ""
            portnum_value: Optional[int] = None
            if isinstance(portnum_raw, str):
                portnum_name = portnum_raw.upper()
                try:
                    portnum_value = portnums.PortNum.Value(portnum_name)
                except ValueError:
                    portnum_value = None
            elif isinstance(portnum_raw, int):
                portnum_value = portnum_raw
                try:
                    portnum_name = portnums.PortNum.Name(portnum_raw)
                except ValueError:
                    portnum_name = str(portnum_raw)
            if portnum_name == "TELEMETRY_APP":
                self._handle_telemetry(packet)
            elif portnum_name in {"TEXT_MESSAGE_APP", "PRIVATE_APP", "REPLY_APP"}:
                self._handle_text_message(packet)
            else:
                # Treat other packets as potential text payloads if they include text
                if "payload" in decoded:
                    self._handle_text_message(packet)
        except Exception as exc:
            self._logger.exception("Failed to handle packet: %s", exc)

    def _handle_telemetry(self, packet: Dict[str, Any]) -> None:
        if not self._node_paths:
            return
        decoded = packet.get("decoded", {})
        payload = decoded.get("payload")
        if not isinstance(payload, dict):
            return
        node_id = str(packet.get("fromId") or payload.get("id") or payload.get("owner"))
        if not node_id:
            return
        latitude = payload.get("latitude") or payload.get("lat")
        longitude = payload.get("longitude") or payload.get("lon")
        rssi = packet.get("rxRssi") or packet.get("rssi")
        telemetry_json = dump_meta(payload)
        observed_at = self._packet_timestamp(packet)
        meaningful = {
            "latitude": latitude,
            "longitude": longitude,
            "rssi": rssi,
            "telemetry_json": telemetry_json,
        }
        hash_source = json.dumps(meaningful, sort_keys=True, default=str).encode("utf-8")
        sighting_hash = uuid.uuid5(uuid.NAMESPACE_OID, hash_source.decode("utf-8", errors="ignore"))
        rows = self._csv.read_rows(self._node_paths.sightings_csv)
        today = observed_at.split("T")[0]
        for existing in reversed(rows):
            if existing.get("node_id") != node_id:
                continue
            if existing.get("sighting_hash") == str(sighting_hash):
                if (existing.get("observed_at") or "").split("T")[0] == today:
                    return
        self._csv.append_row(
            self._node_paths.sightings_csv,
            SIGHTINGS_HEADERS,
            {
                "node_id": node_id,
                "latitude": latitude or "",
                "longitude": longitude or "",
                "rssi": rssi or "",
                "telemetry_json": telemetry_json,
                "observed_at": observed_at,
                "sighting_hash": str(sighting_hash),
            },
        )
        self._logger.info("Stored sighting for node %s", node_id)
        self._upsert_node(packet, payload)

    def _handle_text_message(self, packet: Dict[str, Any]) -> None:
        if not self._node_paths:
            return
        decoded = packet.get("decoded", {})
        text = self._extract_text(decoded)
        if text is None:
            return
        sender_id = str(packet.get("fromId") or decoded.get("from"))
        reply_to_id = decoded.get("replyId") or ""
        timestamp = self._packet_timestamp(packet)
        message_id = str(packet.get("id") or packet.get("packetId") or uuid.uuid4())
        thread_type, thread_key, channel_meta = self._derive_thread(packet)
        meta: Dict[str, Any] = {
            "channel_index": channel_meta.get("index"),
            "channel_name": channel_meta.get("name"),
            "raw_portnum": decoded.get("portnum"),
        }
        row = {
            "thread_type": thread_type,
            "thread_key": thread_key,
            "message_id": message_id,
            "direction": "inbound",
            "sender_id": sender_id or "",
            "reply_to_id": reply_to_id or "",
            "timestamp": timestamp,
            "content": text,
            "send_attempts": "0",
            "send_status": "",
            "meta_json": dump_meta(meta),
        }
        csv_path = self._thread_csv_path(thread_type, thread_key)
        rows = self._csv.read_rows(csv_path)
        for existing in rows:
            if existing.get("message_id") == message_id:
                return
            if (
                existing.get("direction") == "inbound"
                and existing.get("sender_id") == row["sender_id"]
                and existing.get("timestamp") == row["timestamp"]
                and existing.get("content") == row["content"]
            ):
                return
        self._csv.append_row(csv_path, THREAD_HEADERS, row)
        self._logger.info("Recorded inbound message %s (%s:%s)", message_id, thread_type, thread_key)
        self._upsert_node(packet, decoded.get("payload") if isinstance(decoded.get("payload"), dict) else None)

    def _thread_csv_path(self, thread_type: str, thread_key: str) -> Path:
        assert self._node_paths is not None
        if thread_type == "dm":
            directory = self._node_paths.dms_dir
        else:
            directory = self._node_paths.channels_dir
        safe_key = sanitize_name(thread_key)
        path = directory / f"{safe_key}.csv"
        self._csv.ensure_file(path, THREAD_HEADERS)
        return path

    def _derive_thread(self, packet: Dict[str, Any]) -> tuple[str, str, Dict[str, Any]]:
        channel_info = packet.get("channel") or {}
        decoded = packet.get("decoded", {})
        to_id = packet.get("toId") or decoded.get("dest")
        sender_id = packet.get("fromId") or decoded.get("from")
        channel_name = channel_info.get("name")
        if to_id and to_id not in ("^all", "^broadcast", "ffffffff", "4294967295"):
            thread_type = "dm"
            thread_key = str(sender_id or to_id)
        elif channel_name:
            thread_type = "channel"
            thread_key = str(channel_name)
        else:
            thread_type = "channel"
            thread_key = f"channel_{channel_info.get('index', 0)}"
        return thread_type, thread_key, {"index": channel_info.get("index"), "name": channel_name}

    def _extract_text(self, decoded: Dict[str, Any]) -> Optional[str]:
        payload = decoded.get("payload")
        if isinstance(payload, dict):
            for key in ("text", "message", "msg"):
                if payload.get(key):
                    return str(payload[key])
        if isinstance(payload, str):
            return payload
        for key in ("text", "message", "msg"):
            if key in decoded and decoded[key]:
                return str(decoded[key])
        return None

    def _upsert_node(self, packet: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> None:
        if not self._node_paths:
            return
        node_id = str(packet.get("fromId") or packet.get("from"))
        if not node_id:
            return
        short_name = None
        long_name = None
        if payload:
            short_name = payload.get("shortName") or payload.get("short_name")
            long_name = payload.get("longName") or payload.get("long_name")
        user_info = packet.get("user") or packet.get("fromUser") or {}
        short_name = short_name or user_info.get("shortName") or user_info.get("short_name")
        long_name = long_name or user_info.get("longName") or user_info.get("long_name")
        rows = self._csv.read_rows(self._node_paths.nodes_csv)
        updated = False
        for row in rows:
            if row.get("node_id") == node_id:
                row["last_seen_at"] = iso_now()
                if short_name and (row.get("short_name") or "") != str(short_name):
                    row["short_name"] = str(short_name)
                if long_name and (row.get("long_name") or "") != str(long_name):
                    row["long_name"] = str(long_name)
                updated = True
                break
        if not updated:
            rows.append(
                {
                    "node_id": node_id,
                    "short_name": str(short_name or ""),
                    "long_name": str(long_name or ""),
                    "first_seen_at": iso_now(),
                    "last_seen_at": iso_now(),
                }
            )
        self._csv.write_rows(self._node_paths.nodes_csv, NODES_HEADERS, rows)

    def _flush_outbound_queue(self) -> None:
        if not self._node_paths or not self._interface:
            return
        for thread_file in list(self._node_paths.channels_dir.glob("*.csv")) + list(self._node_paths.dms_dir.glob("*.csv")):
            rows = self._csv.read_rows(thread_file)
            changed = False
            for row in rows:
                if row.get("direction") != "queued":
                    continue
                attempts = int(row.get("send_attempts") or 0)
                if attempts >= MAX_SEND_ATTEMPTS:
                    continue
                meta = load_meta(row)
                if not self._can_attempt_send(meta):
                    continue
                success = self._send_row(row)
                attempts += 1
                row["send_attempts"] = str(attempts)
                if success:
                    row["direction"] = "outbound"
                    row["send_status"] = "sent"
                    meta["sent_at"] = iso_now()
                    meta.pop("next_attempt_ts", None)
                    changed = True
                else:
                    row["send_status"] = "failed"
                    backoff = min(300, BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))
                    meta["next_attempt_ts"] = time.time() + backoff
                    changed = True
                row["meta_json"] = dump_meta(meta)
            if changed:
                self._csv.write_rows(thread_file, THREAD_HEADERS, rows)

    def _can_attempt_send(self, meta: Dict[str, Any]) -> bool:
        next_attempt = meta.get("next_attempt_ts")
        if not next_attempt:
            return True
        try:
            return float(next_attempt) <= time.time()
        except (TypeError, ValueError):
            return True

    def _send_row(self, row: Dict[str, str]) -> bool:
        assert self._interface is not None
        try:
            if row.get("thread_type") == "dm":
                dest = row.get("thread_key")
                if not dest:
                    raise ValueError("Missing thread_key for DM send")
                self._interface.sendText(row.get("content", ""), dest)
            else:
                meta = load_meta(row)
                channel_index = meta.get("channel_index")
                if channel_index is None:
                    channel_index = 0
                self._interface.sendText(row.get("content", ""), channelIndex=int(channel_index))
            self._logger.info(
                "Sent queued message %s (%s:%s)",
                row.get("message_id"),
                row.get("thread_type"),
                row.get("thread_key"),
            )
            return True
        except Exception as exc:
            self._logger.warning(
                "Failed to send queued message %s: %s",
                row.get("message_id"),
                exc,
            )
            return False


class StubSerialInterface:
    """Lightweight Meshtastic stub used for --test runs."""

    def __init__(self) -> None:
        self._listener = None
        self.sent_messages = []
        self._node_info = {"userId": "TESTNODE", "longName": "Test Node", "shortName": "Test"}

    # Meshtastic SerialInterface compatibility
    def getMyNodeInfo(self) -> Dict[str, Any]:
        return self._node_info

    def addPacketListener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        self._listener = listener

    def removePacketListener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        if self._listener == listener:
            self._listener = None

    def close(self) -> None:
        self._listener = None

    def sendText(self, content: str, dest: Optional[str] = None, channelIndex: Optional[int] = None) -> None:
        self.sent_messages.append({"content": content, "dest": dest, "channelIndex": channelIndex})

    # Test helper
    def feed(self, packet: Dict[str, Any]) -> None:
        if self._listener:
            self._listener(packet)
        pub.sendMessage("meshtastic.receive", packet=packet, interface=self)


def run_test_mode(bridge: MeshtasticBridge, interface: StubSerialInterface) -> None:
    """Execute a quick smoke test without touching real hardware."""

    bridge._logger.info("Entering test mode; using temporary directories at %s", bridge.config.data_root)
    bridge.start()
    try:
        pub.sendMessage("meshtastic.connection.established", interface=interface)
        now = time.time()
        telemetry_packet = {
            "fromId": "^meshPeer",
            "rxTime": now,
            "decoded": {
                "portnum": portnums.PORTNUM_TELEMETRY_APP,
                "payload": {
                    "latitude": 35.0,
                    "longitude": -117.0,
                    "battery": 88,
                    "shortName": "Peer",
                    "longName": "Peer Node",
                },
            },
            "rxRssi": -75,
        }
        interface.feed(telemetry_packet)

        message_packet = {
            "fromId": "^meshPeer",
            "rxTime": now + 1,
            "decoded": {
                "portnum": portnums.PORTNUM_TEXT_MESSAGE_APP,
                "payload": {"text": "test message"},
            },
            "channel": {"index": 0, "name": "General"},
        }
        interface.feed(message_packet)

        # Queue a dummy outbound message to exercise send flow
        if bridge._node_paths:
            queued_row = {
                "thread_type": "dm",
                "thread_key": "^meshPeer",
                "message_id": str(uuid.uuid4()),
                "direction": "queued",
                "sender_id": bridge._node_paths.node_uid,
                "reply_to_id": "",
                "timestamp": iso_now(),
                "content": "ack: test",
                "send_attempts": "0",
                "send_status": "",
                "meta_json": dump_meta({}),
            }
            thread_csv = bridge._thread_csv_path("dm", "^meshPeer")
            bridge._csv.append_row(thread_csv, THREAD_HEADERS, queued_row)
        bridge._flush_outbound_queue()
    finally:
        bridge.stop()

    print("Test mode completed.")
    print(f"Temporary data directory: {bridge.config.data_root}")
    if bridge.config.log_file:
        print(f"Log file: {bridge.config.log_file}")
    if interface.sent_messages:
        print(f"Queued sends recorded: {len(interface.sent_messages)}")
    else:
        print("No outbound sends captured during test run.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Meshtastic bridge for Meshtastic-LLM")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to TOML config file")
    parser.add_argument("--status", action="store_true", help="Print current data/log status and exit")
    parser.add_argument("--test", action="store_true", help="Run in synthetic test mode without hardware")
    parser.add_argument("--log-dir", type=Path, help="Override log directory location")
    parser.add_argument("--serial-port", type=str, help="Override Meshtastic serial port device path")
    return parser.parse_args()


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def print_status(config: BridgeConfig) -> None:
    """Emit a quick summary of data directories, queued messages, and logs."""

    print(f"Data root: {config.data_root}")
    print(f"Nodes base: {config.nodes_base}")
    print(f"Logs dir: {config.logs_dir}")
    log_files = sorted(config.logs_dir.glob("log.*.txt")) if config.logs_dir.exists() else []
    if log_files:
        latest_log = log_files[-1]
        print(f"Most recent log: {latest_log} ({latest_log.stat().st_size} bytes)")
    else:
        print("No log files found yet.")

    if not config.nodes_base.exists():
        print("No node directories discovered.")
        return

    for node_dir in sorted(p for p in config.nodes_base.iterdir() if p.is_dir()):
        nodes_csv = node_dir / "nodes.csv"
        sightings_csv = node_dir / "sightings.csv"
        channels_dir = node_dir / "threads" / "channels"
        dms_dir = node_dir / "threads" / "dms"
        nodes_rows = _read_csv_rows(nodes_csv)
        print(f"\nNode UID: {node_dir.name}")
        print(f"  Known peers: {len(nodes_rows)}")
        if nodes_rows:
            latest_peer = max(nodes_rows, key=lambda row: row.get("last_seen_at", ""))
            print(f"  Last peer seen: {latest_peer.get('node_id')} at {latest_peer.get('last_seen_at')}")
        sightings_rows = _read_csv_rows(sightings_csv)
        print(f"  Sightings recorded: {len(sightings_rows)}")

        def summarize_threads(folder: Path) -> Dict[str, int]:
            counts = {"files": 0, "queued": 0, "outbound": 0, "inbound": 0}
            if not folder.exists():
                return counts
            for csv_path in folder.glob("*.csv"):
                counts["files"] += 1
                for row in _read_csv_rows(csv_path):
                    direction = (row.get("direction") or "").lower()
                    if direction in counts:
                        counts[direction] += 1
            return counts

        channels_summary = summarize_threads(channels_dir)
        dms_summary = summarize_threads(dms_dir)
        print(
            "  Channels: {files} files | {inbound} inbound | {queued} queued | {outbound} outbound".format(
                **channels_summary
            )
        )
        print(
            "  DMs: {files} files | {inbound} inbound | {queued} queued | {outbound} outbound".format(
                **dms_summary
            )
        )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.log_dir:
        config.logs_dir = (args.log_dir if args.log_dir.is_absolute() else (BASE_DIR / args.log_dir)).resolve()
    if args.serial_port:
        config.serial_port = args.serial_port

    if args.status:
        print_status(config)
        return

    if args.test:
        with tempfile.TemporaryDirectory(prefix="meshtastic_llm_test_") as tmp_dir:
            tmp_root = Path(tmp_dir)
            config.test_mode = True
            config.data_root = tmp_root / "data"
            config.nodes_base = config.data_root / "nodes"
            config.prompts_dir = tmp_root / "prompts"
            config.logs_dir = tmp_root / "logs"
            config.prompts_dir.mkdir(parents=True, exist_ok=True)
            config.logs_dir.mkdir(parents=True, exist_ok=True)
            stub_interface = StubSerialInterface()
            bridge = MeshtasticBridge(
                config,
                interface_factory=lambda: stub_interface,
                cli_args=vars(args),
            )
            run_test_mode(bridge, stub_interface)
        return

    bridge = MeshtasticBridge(config, cli_args=vars(args))
    bridge.run()


if __name__ == "__main__":
    main()
