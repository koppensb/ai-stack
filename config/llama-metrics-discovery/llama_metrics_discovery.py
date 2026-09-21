#!/usr/bin/env python3
"""llama.cpp target discovery and ComfyUI/ROCm VRAM coordination."""

import glob
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("ai-control-plane")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


# Compose maps VRAM_* deployment names to these process-level names. Memory
# values use MiB; delays/timeouts use seconds; percentages use the 0..100 scale.
# Only this main router is queried, not the separate image-prompt router.
LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://llama-cpp:8000").rstrip("/")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
LLAMA_API_KEY_FILE = os.environ.get("LLAMA_API_KEY_FILE", "/run/secrets/llama_cpp_api_key")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9091"))
GPU_INDEX = int(os.environ.get("GPU_INDEX", "0"))
VRAM_SOURCE = os.environ.get("VRAM_SOURCE", "auto").strip().lower()
RESERVE_VRAM_MB = float(os.environ.get("RESERVE_VRAM_MB", "6000"))
VRAM_THRESHOLD_RAW = os.environ.get("VRAM_THRESHOLD_PCT", "off").strip().lower()
VRAM_THRESHOLD_PCT = None if VRAM_THRESHOLD_RAW in {"", "off", "none", "disabled"} else float(VRAM_THRESHOLD_RAW)
CHECK_INTERVAL = float(os.environ.get("CHECK_INTERVAL", "1"))
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "20"))
HYSTERESIS_MB = float(os.environ.get("HYSTERESIS_MB", "1024"))
HYSTERESIS_PCT = float(os.environ.get("HYSTERESIS_PCT", "5"))
COMFYUI_IDLE_UNLOAD_SECONDS = float(os.environ.get("COMFYUI_IDLE_UNLOAD_SECONDS", "5"))
AGGRESSIVE_FREE = env_bool("AGGRESSIVE_FREE", True)
AGGRESSIVE_DELAY_SECONDS = float(os.environ.get("AGGRESSIVE_DELAY_SECONDS", "8"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "2.5"))
COMMAND_TIMEOUT = float(os.environ.get("COMMAND_TIMEOUT", "3"))
ERROR_LOG_INTERVAL = float(os.environ.get("ERROR_LOG_INTERVAL", "60"))
STATS_INTERVAL = float(os.environ.get("STATS_INTERVAL", "300"))


@dataclass
class Memory:
    used_mb: float
    total_mb: float
    source: str

    @property
    def free_mb(self) -> float:
        return max(0.0, self.total_mb - self.used_mb)

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used_mb / self.total_mb if self.total_mb else 0.0


def api_key() -> str:
    try:
        with open(LLAMA_API_KEY_FILE, "r", encoding="utf-8") as key_file:
            return key_file.read().strip()
    except OSError:
        return ""


def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
              authenticated: bool = False) -> Tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if authenticated and api_key():
        headers["Authorization"] = "Bearer {}".format(api_key())
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode("utf-8", errors="replace")[:500]
        return error.code, parsed


class VramProbe:
    """Prefer ComfyUI's global device view, then use local ROCm fallbacks."""

    SOURCES = ("comfyui", "amd-smi", "rocm-smi", "sysfs")

    def __init__(self) -> None:
        if VRAM_SOURCE not in {"auto", *self.SOURCES}:
            raise ValueError("VRAM_SOURCE must be auto, comfyui, amd-smi, rocm-smi or sysfs")
        self.preferred: Optional[str] = None

    # Prefer the last successful source to avoid retrying unavailable tools on
    # every tick; only auto mode may fall through to another probe.
    def read(self) -> Memory:
        names = list(self.SOURCES) if VRAM_SOURCE == "auto" else [VRAM_SOURCE]
        if self.preferred in names:
            names.remove(self.preferred)
            names.insert(0, self.preferred)
        errors = []
        for name in names:
            try:
                memory = getattr(self, "_" + name.replace("-", "_"))()
                if memory.total_mb <= 0 or memory.used_mb < 0:
                    raise ValueError("invalid memory values")
                self.preferred = name
                return memory
            except Exception as error:
                errors.append("{}: {}".format(name, error))
        raise RuntimeError("; ".join(errors))

    def _comfyui(self) -> Memory:
        status, payload = http_json("GET", COMFYUI_URL + "/system_stats")
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("HTTP {}".format(status))
        devices = payload.get("devices", [])
        if not isinstance(devices, list) or not devices:
            raise ValueError("no devices in /system_stats")
        device = next((item for item in devices if isinstance(item, dict) and item.get("index") == GPU_INDEX), None)
        if device is None and GPU_INDEX < len(devices):
            device = devices[GPU_INDEX]
        if not isinstance(device, dict):
            raise ValueError("GPU {} not found in /system_stats".format(GPU_INDEX))
        total_bytes = float(device["vram_total"])
        # ComfyUI includes unused PyTorch reservations in vram_free.
        # Those bytes are not yet available to another process.
        free_bytes = max(0.0, float(device["vram_free"]) - float(device["torch_vram_free"]))
        divisor = 1024.0 * 1024.0
        return Memory((total_bytes - free_bytes) / divisor, total_bytes / divisor, "comfyui")

    @staticmethod
    def _walk(value: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = "{} {}".format(path, key).strip().lower()
                yield child_path, child
                yield from VramProbe._walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from VramProbe._walk(child, "{} {}".format(path, index))

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
            return float(match.group(0)) if match else None
        return None

    @classmethod
    def _extract_memory(cls, data: Any, source: str) -> Memory:
        total: Optional[Tuple[float, str]] = None
        used: Optional[Tuple[float, str]] = None
        for path, value in cls._walk(data):
            number = cls._number(value)
            normalized = path.replace("_", " ").replace("-", " ")
            if number is None or "vram" not in normalized:
                continue
            if "total" in normalized and total is None:
                total = (number, normalized)
            if ("used" in normalized or "usage" in normalized) and "percent" not in normalized and used is None:
                used = (number, normalized)
        if not total or not used:
            raise ValueError("VRAM total/used fields not found")

        # SMI releases use different field labels. Explicit unit labels win;
        # unlabeled large values are treated as bytes (a compatibility heuristic).
        def to_mb(item: Tuple[float, str]) -> float:
            number, key = item
            if "gib" in key or " gb" in key:
                return number * 1024.0
            if "kib" in key or " kb" in key:
                return number / 1024.0
            if "mib" in key or " mb" in key:
                return number
            return number / (1024.0 * 1024.0) if number > 1024 * 1024 else number

        return Memory(to_mb(used), to_mb(total), source)

    @staticmethod
    def _run_json(command: List[str]) -> Any:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=COMMAND_TIMEOUT, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "exit {}".format(result.returncode))
        return json.loads(result.stdout)

    def _amd_smi(self) -> Memory:
        return self._extract_memory(
            self._run_json(["amd-smi", "metric", "--gpu", str(GPU_INDEX), "--json"]), "amd-smi"
        )

    def _rocm_smi(self) -> Memory:
        data = self._run_json(["rocm-smi", "--device", str(GPU_INDEX), "--showmeminfo", "vram", "--json"])
        card = data.get("card{}".format(GPU_INDEX), data) if isinstance(data, dict) else data
        return self._extract_memory(card, "rocm-smi")

    def _sysfs(self) -> Memory:
        # This fallback uses sorted visible DRM devices; that ordering may differ
        # from HIP/SMI indices when the host exposes multiple GPUs.
        devices = []
        for total_path in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/mem_info_vram_total")):
            used_path = os.path.join(os.path.dirname(total_path), "mem_info_vram_used")
            if os.path.exists(used_path):
                devices.append((total_path, used_path))
        if GPU_INDEX >= len(devices):
            raise ValueError("GPU {} not found ({} sysfs GPU(s))".format(GPU_INDEX, len(devices)))
        total_path, used_path = devices[GPU_INDEX]
        with open(total_path, "r", encoding="ascii") as handle:
            total = int(handle.read().strip()) / (1024.0 * 1024.0)
        with open(used_path, "r", encoding="ascii") as handle:
            used = int(handle.read().strip()) / (1024.0 * 1024.0)
        return Memory(used, total, "sysfs")


# One background thread performs mutations; HTTP handlers serve snapshots.
# Locks protect shared model/health snapshots. Counters are diagnostic snapshots,
# not a transactionally consistent record of an entire control-loop iteration.
class ControlPlane:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.probe = VramProbe()
        self.lock = threading.Lock()
        self.models: List[Dict[str, Any]] = []
        # Wall-clock timestamps are exported; monotonic clocks govern durations
        # so time synchronization cannot bypass cooldowns or delay cleanup.
        self.last_success = 0.0
        self.last_loop = 0.0
        self.last_error = ""
        self.last_error_log = 0.0
        self.last_stats_log = time.monotonic()
        self.last_action = 0.0
        self.last_soft = 0.0
        # Pressure hysteresis rearms cleanup after recovery; separate timestamps
        # track soft cleanup, full release, and each llama unload attempt.
        self.armed = True
        self.comfy_idle_since: Optional[float] = None
        self.comfy_idle_released = False
        self.comfy_was_busy = False
        self.last_full_release = float("-inf")
        self.llama_unload_attempts: Dict[str, float] = {}
        self.stats = {"checks": 0, "llama_active_checks": 0, "pressure_checks": 0,
                      "soft_frees": 0, "aggressive_frees": 0, "llama_unloads": 0, "api_errors": 0, "probe_errors": 0}

    @staticmethod
    def model_status(model: Dict[str, Any]) -> str:
        status = model.get("status", "")
        return str(status.get("value", "") if isinstance(status, dict) else status).lower()

    def refresh_models(self) -> Tuple[List[Dict[str, Any]], bool]:
        status, payload = http_json("GET", LLAMA_BASE_URL + "/models", authenticated=True)
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError("llama.cpp /models returned HTTP {}".format(status))
        if not isinstance(payload.get("data"), list):
            raise RuntimeError("llama.cpp /models returned an invalid model list")
        models = [item for item in payload["data"] if isinstance(item, dict)]
        loading = any(self.model_status(model) in {"loading", "pending"} for model in models)
        with self.lock:
            self.models, self.last_success, self.last_error = models, time.time(), ""
        return models, loading

    @staticmethod
    def health_loading() -> bool:
        status, payload = http_json("GET", LLAMA_BASE_URL + "/health")
        if status not in {200, 503}:
            raise RuntimeError("llama.cpp /health returned HTTP {}".format(status))
        detail = json.dumps(payload).lower() if payload is not None else ""
        return status == 503 and ("load" in detail or not detail)

    @staticmethod
    def slot_active(slot: Any) -> bool:
        return isinstance(slot, dict) and (
            slot.get("is_processing") is True
            or str(slot.get("state", "")).lower() in {"processing", "prompt", "generating", "busy"}
        )

    def llama_demand(self, models: List[Dict[str, Any]], loading: bool) -> bool:
        if loading:
            return True
        for model in models:
            if self.model_status(model) != "loaded" or not model.get("id"):
                continue
            if not self.llama_model_idle(str(model["id"])):
                return True
        return False

    def llama_model_idle(self, model_id: str) -> bool:
        query = urlencode({"model": model_id, "autoload": "false"})
        status, slots = http_json("GET", LLAMA_BASE_URL + "/slots?" + query, authenticated=True)
        if isinstance(slots, dict):
            slots = slots.get("slots")
        # Never infer inactivity from missing, empty or malformed slot data.
        if (status != 200 or not isinstance(slots, list) or not slots
                or any(not isinstance(slot, dict)
                       or not isinstance(slot.get("is_processing"), bool) for slot in slots)):
            raise RuntimeError("llama.cpp /slots unavailable or invalid (HTTP {})".format(status))
        return all(not slot["is_processing"] and not self.slot_active(slot) for slot in slots)

    def release_llama_for_comfyui(self, models: List[Dict[str, Any]]) -> None:
        """Release at most one idle model, then remeasure VRAM on the next tick."""
        now = time.monotonic()
        for model in models:
            if self.model_status(model) != "loaded" or not model.get("id"):
                continue
            model_id = str(model["id"])
            if now - self.llama_unload_attempts.get(model_id, float("-inf")) < COOLDOWN_SECONDS:
                continue
            try:
                # Recheck activity immediately before unloading. Polling cannot
                # exclude a new inference request arriving after this check.
                if not self.llama_model_idle(model_id):
                    continue
                if self.comfyui_idle():
                    return
                self.llama_unload_attempts[model_id] = now
                status, payload = http_json("POST", LLAMA_BASE_URL + "/models/unload",
                                            {"model": model_id}, authenticated=True)
                if not 200 <= status < 300 or not isinstance(payload, dict) or payload.get("success") is not True:
                    raise RuntimeError("llama.cpp /models/unload failed (HTTP {})".format(status))
                self.stats["llama_unloads"] += 1
                LOG.warning("Requested llama.cpp model %s unload for ComfyUI VRAM demand", model_id)
            except Exception as error:
                self.llama_unload_attempts[model_id] = now
                self.stats["api_errors"] += 1
                self.set_error(error)
            return

    @staticmethod
    # Either enabled pressure criterion may trigger a release attempt.
    def pressured(memory: Memory) -> bool:
        return (
            (RESERVE_VRAM_MB > 0 and memory.free_mb < RESERVE_VRAM_MB)
            or (VRAM_THRESHOLD_PCT is not None and memory.used_pct >= VRAM_THRESHOLD_PCT)
        )

    @staticmethod
    # Both enabled criteria must clear their margins before rearming.
    def recovered(memory: Memory) -> bool:
        return (
            (RESERVE_VRAM_MB <= 0 or memory.free_mb >= RESERVE_VRAM_MB + HYSTERESIS_MB)
            and (VRAM_THRESHOLD_PCT is None or memory.used_pct <= VRAM_THRESHOLD_PCT - HYSTERESIS_PCT)
        )

    # /free sets worker flags asynchronously: HTTP success confirms acceptance,
    # not that another process can already allocate the released VRAM.
    def free_comfyui(self, aggressive: bool) -> bool:
        try:
            status, _ = http_json("POST", COMFYUI_URL + "/free",
                                  {"unload_models": aggressive, "free_memory": True})
            if not 200 <= status < 300:
                raise RuntimeError("HTTP {}".format(status))
            self.stats["aggressive_frees" if aggressive else "soft_frees"] += 1
            LOG.warning("Requested %s ComfyUI VRAM release", "aggressive" if aggressive else "soft")
            return True
        except Exception as error:
            self.stats["api_errors"] += 1
            self.last_action = time.monotonic()
            self.set_error(RuntimeError("ComfyUI /free failed: {}".format(error)))
            return False

    def comfyui_idle(self) -> bool:
        status, queue = http_json("GET", COMFYUI_URL + "/queue")
        if (status != 200 or not isinstance(queue, dict)
                or not isinstance(queue.get("queue_running"), list)
                or not isinstance(queue.get("queue_pending"), list)):
            raise RuntimeError("ComfyUI /queue unavailable or invalid (HTTP {})".format(status))
        return not queue["queue_running"] and not queue["queue_pending"]

    # Tri-state result: True=idle, False=busy, None=unknown after an API error.
    # Unknown state must not be treated as permission to interrupt image work.
    def release_idle_comfyui(self, loading: bool = False) -> Optional[bool]:
        """Unload before demand arrives; a successful POST only acknowledges flags."""
        try:
            idle = self.comfyui_idle()
        except Exception as error:
            self.comfy_idle_since = None
            self.comfy_idle_released = False
            self.stats["api_errors"] += 1
            self.set_error(error)
            return None
        now = time.monotonic()
        if not idle:
            self.comfy_idle_since = None
            self.comfy_idle_released = False
            self.comfy_was_busy = True
            return False
        if self.comfy_was_busy:
            # A new image job invalidates a prior successful release. Do not
            # carry its cooldown into this handoff; failures still back off.
            self.last_full_release = float("-inf")
            self.comfy_was_busy = False
            if COMFYUI_IDLE_UNLOAD_SECONDS > 0:
                self.comfy_idle_since = now - COMFYUI_IDLE_UNLOAD_SECONDS
        if self.comfy_idle_since is None:
            self.comfy_idle_since = now
        idle_due = (COMFYUI_IDLE_UNLOAD_SECONDS > 0 and not self.comfy_idle_released
                    and now - self.comfy_idle_since >= COMFYUI_IDLE_UNLOAD_SECONDS)
        if (idle_due or (loading and AGGRESSIVE_FREE)) and now - self.last_full_release >= COOLDOWN_SECONDS:
            # Track failures too, so an unavailable /free endpoint is not hammered.
            self.last_full_release = now
            if self.free_comfyui(True):
                self.last_action, self.last_soft = now, 0.0
                self.comfy_idle_released = True
        return True

    def set_error(self, error: Exception) -> None:
        with self.lock:
            self.last_error = str(error)
        now = time.monotonic()
        if now - self.last_error_log >= ERROR_LOG_INTERVAL:
            LOG.error("Control loop error: %s", error)
            self.last_error_log = now

    def tick(self) -> None:
        self.stats["checks"] += 1
        with self.lock:
            self.last_loop = time.time()
        # Idle cleanup must work even when llama.cpp or the GPU probe is down.
        comfy_idle = self.release_idle_comfyui()
        try:
            health_loading = self.health_loading()
            models, model_loading = self.refresh_models()
            loading = health_loading or model_loading
            demand = self.llama_demand(models, loading)
        except (OSError, ValueError, RuntimeError, URLError) as error:
            self.stats["api_errors"] += 1
            self.set_error(error)
            return
        if loading and comfy_idle:
            comfy_idle = self.release_idle_comfyui(loading=True)
        if demand:
            self.stats["llama_active_checks"] += 1
        try:
            memory = self.probe.read()
        except Exception as error:
            self.stats["probe_errors"] += 1
            self.set_error(error)
            return

        pressure = self.pressured(memory)
        if pressure:
            self.stats["pressure_checks"] += 1
        elif self.recovered(memory):
            self.armed, self.last_soft = True, 0.0
        LOG.debug("GPU %d %.0f/%.0f MiB (%.1f%%), free %.0f MiB via %s; demand=%s armed=%s",
                  GPU_INDEX, memory.used_mb, memory.total_mb, memory.used_pct,
                  memory.free_mb, memory.source, demand, self.armed)

        now = time.monotonic()
        if now - self.last_stats_log >= STATS_INTERVAL:
            LOG.info("Statistics: %s", json.dumps(self.stats, sort_keys=True))
            self.last_stats_log = now
        # Give busy ComfyUI room by unloading one verified-idle main-router model.
        # Otherwise, help active llama.cpp only while the image queue is empty.
        if comfy_idle is False and pressure and not loading:
            self.release_llama_for_comfyui(models)
            return
        if not (demand and pressure and comfy_idle):
            return
        # Escalation has its own shorter delay; ordinary repeated cleanup obeys
        # the longer cooldown. Neither path is an atomic scheduling/load barrier.
        can_escalate = AGGRESSIVE_FREE and self.last_soft > 0 and now - self.last_soft >= AGGRESSIVE_DELAY_SECONDS
        if can_escalate and now - self.last_action >= AGGRESSIVE_DELAY_SECONDS:
            if self.free_comfyui(True):
                self.last_action, self.last_soft, self.armed = now, 0.0, False
                self.last_full_release = now
                self.comfy_idle_released = True
        elif now - self.last_action >= COOLDOWN_SECONDS and (self.armed or self.last_soft == 0):
            if self.free_comfyui(False):
                self.last_soft, self.last_action, self.armed = now, now, False

    def run(self) -> None:
        LOG.info("VRAM manager started: GPU=%d source=%s reserve=%.0f MiB threshold=%s",
                 GPU_INDEX, VRAM_SOURCE, RESERVE_VRAM_MB,
                 "off" if VRAM_THRESHOLD_PCT is None else "{:.1f}%".format(VRAM_THRESHOLD_PCT))
        while not self.stop.is_set():
            started = time.monotonic()
            self.tick()
            self.stop.wait(max(0.0, CHECK_INTERVAL - (time.monotonic() - started)))
        LOG.info("Stopped. Statistics: %s", json.dumps(self.stats, sort_keys=True))

    # Prometheus relabels llama_model_id into ?model=... on its /metrics request.
    # No loaded model means an empty discovery list, not a router outage.
    def target_payload(self) -> List[Dict[str, Any]]:
        with self.lock:
            models = list(self.models)
        return [{"targets": ["llama-cpp:8000"], "labels": {"llama_model_id": str(model["id"])}}
                for model in models if model.get("id") and self.model_status(model) == "loaded"]

    def metrics_payload(self) -> str:
        # This endpoint never calls downstream services or loads a model.
        with self.lock:
            last_loop, last_success = self.last_loop, self.last_success
        now = time.time()
        values = {
            "ai_control_plane_loop_healthy": int(bool(last_loop) and now - last_loop <= max(10.0, CHECK_INTERVAL * 5)),
            "ai_control_plane_llama_reachable": int(bool(last_success) and now - last_success <= max(30.0, CHECK_INTERVAL * 5)),
            "ai_control_plane_llama_last_success_timestamp_seconds": last_success,
        }
        lines = []
        for name, value in values.items():
            lines.extend(["# TYPE {} gauge".format(name), "{} {}".format(name, value)])
        for name, value in self.stats.items():
            metric = "ai_control_plane_{}_total".format(name)
            lines.extend(["# TYPE {} counter".format(metric), "{} {}".format(metric, value)])
        return "\n".join(lines) + "\n"

    # Health measures whether the loop still runs. Downstream failures are
    # separate metrics, so Docker health does not fail on every transient outage.
    def health_payload(self) -> Tuple[int, Dict[str, Any]]:
        with self.lock:
            last_loop, last_success, last_error = self.last_loop, self.last_success, self.last_error
        stale = not last_loop or time.time() - last_loop > max(10.0, CHECK_INTERVAL * 5)
        status = 503 if stale else 200
        return status, {"status": "stale" if stale else "ok", "last_loop": last_loop,
                        "last_llama_success": last_success, "last_error": last_error,
                        "statistics": dict(self.stats)}


CONTROL = ControlPlane()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(*CONTROL.health_payload())
        elif self.path == "/targets.json":
            self.send_json(200, CONTROL.target_payload())
        elif self.path == "/metrics":
            body = CONTROL.metrics_payload().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/stats":
            self.send_json(200, CONTROL.stats)
        else:
            self.send_json(404, {"error": "not found"})

    def log_message(self, message_format: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), message_format % args)


def validate_configuration() -> None:
    if COMFYUI_IDLE_UNLOAD_SECONDS < 0 or AGGRESSIVE_DELAY_SECONDS < 0:
        raise ValueError("Idle unload and aggressive delay must not be negative")
    if GPU_INDEX < 0 or RESERVE_VRAM_MB < 0 or HYSTERESIS_MB < 0 or HYSTERESIS_PCT < 0:
        raise ValueError("GPU, reserve and hysteresis values must not be negative")
    if VRAM_THRESHOLD_PCT is not None and not 0 <= VRAM_THRESHOLD_PCT <= 100:
        raise ValueError("VRAM_THRESHOLD_PCT must be 0..100 or off")
    for name, value in (("CHECK_INTERVAL", CHECK_INTERVAL), ("COOLDOWN_SECONDS", COOLDOWN_SECONDS),
                        ("HTTP_TIMEOUT", HTTP_TIMEOUT), ("COMMAND_TIMEOUT", COMMAND_TIMEOUT),
                        ("STATS_INTERVAL", STATS_INTERVAL)):
        if value <= 0:
            raise ValueError("{} must be greater than zero".format(name))


def main() -> int:
    validate_configuration()
    logging.basicConfig(level=logging.DEBUG if env_bool("DEBUG", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.timeout = 0.5
    # Keep probes and network timeouts off the HTTP-serving thread so monitoring
    # can still expose stale-loop health if downstream calls become slow.
    worker = threading.Thread(target=CONTROL.run, name="vram-manager", daemon=True)

    def stop(_signum: int, _frame: Any) -> None:
        CONTROL.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.start()
    while not CONTROL.stop.is_set():
        server.handle_request()
    server.server_close()
    worker.join(timeout=max(5.0, HTTP_TIMEOUT + COMMAND_TIMEOUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
