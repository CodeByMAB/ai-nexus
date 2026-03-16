"""
Metrics collection module for AI Dashboard.
Gathers GPU, system, service, and API metrics.
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import psutil

KEYS_DB = "/opt/ai/keys/keys.sqlite"
MODE_CONF = "/etc/systemd/system/vllm.service.d/mode.conf"
VLLM_API = "http://127.0.0.1:11434"

SYSTEMD_SERVICES = [
    "vllm",
    "ai-gateway",
    "openai-shim",
    "payment-webhook",
]

DOCKER_CONTAINERS = [
    "openwebui",
    "graphiti",
    "graphiti-neo4j",
    "zep",
    "zep-postgres",
]


# ---------------------------------------------------------------------------
# GPU Metrics
# ---------------------------------------------------------------------------

def get_gpu_metrics() -> dict:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu,utilization.memory,"
                "memory.used,memory.total,power.draw,power.limit,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return _gpu_unavailable("nvidia-smi failed")

        line = result.stdout.strip()
        if not line:
            return _gpu_unavailable("no output")

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return _gpu_unavailable("unexpected output")

        def safe_float(v: str) -> Optional[float]:
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        temp = safe_float(parts[0])
        util_gpu = safe_float(parts[1])
        util_mem = safe_float(parts[2])
        mem_used = safe_float(parts[3])
        mem_total = safe_float(parts[4])
        power_draw = safe_float(parts[5])
        power_limit = safe_float(parts[6])
        gpu_name = parts[7] if len(parts) > 7 else "Unknown GPU"

        mem_pct = round(mem_used / mem_total * 100, 1) if mem_total and mem_used else 0

        return {
            "available": True,
            "name": gpu_name,
            "temperature": temp,
            "utilization_gpu": util_gpu,
            "utilization_memory": util_mem,
            "memory_used_mib": mem_used,
            "memory_total_mib": mem_total,
            "memory_used_gib": round(mem_used / 1024, 2) if mem_used else 0,
            "memory_total_gib": round(mem_total / 1024, 2) if mem_total else 0,
            "memory_percent": mem_pct,
            "power_draw_w": power_draw,
            "power_limit_w": power_limit,
            "power_percent": round(power_draw / power_limit * 100, 1) if power_limit and power_draw else 0,
        }
    except FileNotFoundError:
        return _gpu_unavailable("nvidia-smi not found")
    except subprocess.TimeoutExpired:
        return _gpu_unavailable("nvidia-smi timeout")
    except Exception as e:
        return _gpu_unavailable(str(e))


def _gpu_unavailable(reason: str) -> dict:
    return {"available": False, "error": reason}


# ---------------------------------------------------------------------------
# System Metrics
# ---------------------------------------------------------------------------

def get_system_metrics() -> dict:
    try:
        cpu_pct = psutil.cpu_percent(interval=0.2)
        cpu_count = psutil.cpu_count()
        cpu_freq = psutil.cpu_freq()

        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()

        disk_root = psutil.disk_usage("/")
        disk_models = None
        try:
            disk_models = psutil.disk_usage("/opt/models")
        except Exception:
            pass

        def disk_dict(d, label: str) -> dict:
            if d is None:
                return {"label": label, "available": False}
            return {
                "label": label,
                "available": True,
                "total_gib": round(d.total / (1024 ** 3), 2),
                "used_gib": round(d.used / (1024 ** 3), 2),
                "free_gib": round(d.free / (1024 ** 3), 2),
                "percent": d.percent,
            }

        return {
            "cpu": {
                "percent": cpu_pct,
                "count": cpu_count,
                "freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
            },
            "ram": {
                "total_gib": round(ram.total / (1024 ** 3), 2),
                "used_gib": round(ram.used / (1024 ** 3), 2),
                "available_gib": round(ram.available / (1024 ** 3), 2),
                "percent": ram.percent,
            },
            "swap": {
                "total_gib": round(swap.total / (1024 ** 3), 2),
                "used_gib": round(swap.used / (1024 ** 3), 2),
                "percent": swap.percent,
            },
            "disks": [
                disk_dict(disk_root, "/"),
                disk_dict(disk_models, "/opt/models"),
            ],
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Service Status
# ---------------------------------------------------------------------------

def _systemd_status(service: str) -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=3
        )
        state = result.stdout.strip()
        if state == "active":
            status = "active"
        elif state == "activating":
            status = "activating"
        elif state in ("inactive", "dead"):
            status = "inactive"
        elif state == "failed":
            status = "failed"
        else:
            status = state

        # Get uptime for active services
        uptime_str = None
        if status == "active":
            try:
                prop = subprocess.run(
                    ["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
                    capture_output=True, text=True, timeout=3
                )
                ts_line = prop.stdout.strip()
                if "=" in ts_line:
                    ts_val = ts_line.split("=", 1)[1]
                    if ts_val and ts_val != "n/a":
                        from datetime import datetime
                        # Format: "Mon YYYY-MM-DD HH:MM:SS UTC"
                        try:
                            started = datetime.strptime(ts_val, "%a %Y-%m-%d %H:%M:%S %Z")
                            now = datetime.utcnow()
                            delta = now - started
                            secs = int(delta.total_seconds())
                            if secs < 60:
                                uptime_str = f"{secs}s"
                            elif secs < 3600:
                                uptime_str = f"{secs // 60}m"
                            elif secs < 86400:
                                h = secs // 3600
                                m = (secs % 3600) // 60
                                uptime_str = f"{h}h {m}m"
                            else:
                                d = secs // 86400
                                h = (secs % 86400) // 3600
                                uptime_str = f"{d}d {h}h"
                        except Exception:
                            pass
            except Exception:
                pass

        return {"name": service, "type": "systemd", "status": status, "uptime": uptime_str}
    except subprocess.TimeoutExpired:
        return {"name": service, "type": "systemd", "status": "timeout"}
    except Exception as e:
        return {"name": service, "type": "systemd", "status": "error", "error": str(e)}


def _docker_status(container: str) -> dict:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format={{.State.Status}}", container],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"name": container, "type": "docker", "status": "not_found"}
        state = result.stdout.strip()
        status_map = {
            "running": "active",
            "exited": "inactive",
            "paused": "inactive",
            "restarting": "activating",
            "dead": "failed",
            "created": "inactive",
            "removing": "inactive",
        }
        return {
            "name": container,
            "type": "docker",
            "status": status_map.get(state, state),
            "docker_state": state,
        }
    except subprocess.TimeoutExpired:
        return {"name": container, "type": "docker", "status": "timeout"}
    except FileNotFoundError:
        return {"name": container, "type": "docker", "status": "docker_unavailable"}
    except Exception as e:
        return {"name": container, "type": "docker", "status": "error", "error": str(e)}


def get_service_status() -> dict:
    systemd = [_systemd_status(s) for s in SYSTEMD_SERVICES]
    docker = [_docker_status(c) for c in DOCKER_CONTAINERS]
    return {"systemd": systemd, "docker": docker}


# ---------------------------------------------------------------------------
# vLLM Mode
# ---------------------------------------------------------------------------

def get_vllm_mode() -> dict:
    """Extract current vLLM mode from systemd drop-in config."""
    try:
        mode_path = Path(MODE_CONF)
        if not mode_path.exists():
            return {"mode": "unknown", "model": None, "context_length": None}

        content = mode_path.read_text()

        # Extract served-model-name (which is the mode alias)
        mode_match = re.search(r"--served-model-name\s+(\S+)", content)
        mode = mode_match.group(1) if mode_match else "unknown"

        # Extract model path/name
        # Look for first positional arg after "vllm serve"
        model_match = re.search(r"vllm serve\s+(\S+)", content)
        model = model_match.group(1) if model_match else None
        if model:
            model = model.split("/")[-1]  # Just the model name

        # Extract max-model-len
        ctx_match = re.search(r"--max-model-len\s+(\d+)", content)
        context_length = int(ctx_match.group(1)) if ctx_match else None

        # Map served name to display mode
        mode_display_map = {
            "fast": "fast",
            "fast+image": "fast+image",
            "code": "code",
            "extreme": "extreme",
        }
        display_mode = mode_display_map.get(mode, mode)

        return {
            "mode": display_mode,
            "model": model,
            "context_length": context_length,
            "raw_served_name": mode,
        }
    except Exception as e:
        return {"mode": "unknown", "model": None, "context_length": None, "error": str(e)}


# ---------------------------------------------------------------------------
# API Metrics
# ---------------------------------------------------------------------------

def get_api_metrics() -> dict:
    try:
        if not os.path.exists(KEYS_DB):
            return {"available": False, "error": "keys.sqlite not found"}

        conn = sqlite3.connect(f"file:{KEYS_DB}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        today_str = date.today().isoformat()

        # Total requests today
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM token_usage_log WHERE timestamp >= ?",
                (today_str,)
            )
            row = cur.fetchone()
            requests_today = row["cnt"] if row else 0
        except Exception:
            requests_today = 0

        # Also check api_usage table if it exists
        try:
            today_ts = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
            cur.execute(
                "SELECT COUNT(*) as cnt FROM api_usage WHERE ts >= ?",
                (today_ts,)
            )
            row = cur.fetchone()
            requests_today += row["cnt"] if row else 0
        except Exception:
            pass

        # Active API keys
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1"
            )
            row = cur.fetchone()
            active_keys = row["cnt"] if row else 0
        except Exception:
            active_keys = 0

        # Token usage today
        try:
            cur.execute(
                "SELECT COALESCE(SUM(tokens_consumed), 0) as total FROM token_usage_log WHERE timestamp >= ?",
                (today_str,)
            )
            row = cur.fetchone()
            tokens_today = row["total"] if row else 0
        except Exception:
            tokens_today = 0

        # Total tokens this month
        try:
            month_str = date.today().strftime("%Y-%m")
            cur.execute(
                "SELECT COALESCE(SUM(tokens_consumed), 0) as total FROM token_usage_log WHERE timestamp >= ?",
                (month_str + "-01",)
            )
            row = cur.fetchone()
            tokens_month = row["total"] if row else 0
        except Exception:
            tokens_month = 0

        # Total all-time requests
        try:
            cur.execute("SELECT COUNT(*) as cnt FROM token_usage_log")
            row = cur.fetchone()
            total_requests = row["cnt"] if row else 0
        except Exception:
            total_requests = 0

        # Total API keys
        try:
            cur.execute("SELECT COUNT(*) as cnt FROM api_keys")
            row = cur.fetchone()
            total_keys = row["cnt"] if row else 0
        except Exception:
            total_keys = 0

        # Hourly request distribution for today (last 24h)
        hourly = []
        try:
            cur.execute(
                """
                SELECT strftime('%H', timestamp) as hour, COUNT(*) as cnt
                FROM token_usage_log
                WHERE timestamp >= ?
                GROUP BY hour
                ORDER BY hour
                """,
                (today_str,)
            )
            hourly = [{"hour": r["hour"], "count": r["cnt"]} for r in cur.fetchall()]
        except Exception:
            pass

        conn.close()

        return {
            "available": True,
            "requests_today": requests_today,
            "tokens_today": tokens_today,
            "tokens_month": tokens_month,
            "total_requests": total_requests,
            "active_keys": active_keys,
            "total_keys": total_keys,
            "hourly_today": hourly,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Combined metrics
# ---------------------------------------------------------------------------

def get_all_metrics() -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gpu": get_gpu_metrics(),
        "system": get_system_metrics(),
        "services": get_service_status(),
        "vllm_mode": get_vllm_mode(),
        "api": get_api_metrics(),
    }
