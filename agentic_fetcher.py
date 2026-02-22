#!/usr/bin/env python3
"""
agentic_fetcher.py — RSS Newspaper Agentic Fetcher
===================================================

A Claude Agent SDK agent that:
  1. Parses all data/feed*.opml files for RSS/Atom subscriptions
  2. Fetches each feed and filters to articles from the last 3 days
  3. Deduplicates against already-saved article JSON files
  4. Enriches missing metadata (hero image, canonical URL, content) via page scraping
  5. Saves fully-populated Article JSON files under data/articles/<feed-slug>/

File naming convention:
    data/articles/<feed-slug>/<yyyy-mm-dd_HHmmss>-<feed-slug>-<article-title-slug>.json

Re-runs are idempotent: existing articles (matched by GUID) are never duplicated.

Environment (loaded from .env):
    ANTHROPIC_BASE_URL  - LLM endpoint (e.g. http://localhost:1234 for LM Studio)
    ANTHROPIC_AUTH_TOKEN - API auth token
    CLAUDE_CODE_MAX_OUTPUT_TOKENS - Cap assistant output tokens per response
    AGENT_MAX_TURNS     - Max agentic turns per run (blank/0 = no explicit limit)
    USE_VLLM            - Set to 1 to launch local vLLM automatically
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import shlex
import socket
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    create_sdk_mcp_server,
    tool,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Environment & Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
ARTICLES_DIR = DATA_DIR / "articles"
ARCHIVE_DIR = DATA_DIR / "archive"
RETIRED_FEEDS_OPML = ARCHIVE_DIR / "retired-feeds.opml"
CUTOFF_DAYS = 3

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RSSNewspaper/1.0; "
        "+https://github.com/rssnewspaper)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Timeout for HTTP requests (seconds)
HTTP_TIMEOUT = 20
PERMANENT_REDIRECT_CODES = {301, 308}

SAFE_HTML_TAGS = {
    "p",
    "br",
    "hr",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "code",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "a",
    "img",
}

STRIP_ENTIRELY_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "option",
    "meta",
    "link",
    "base",
    "svg",
    "math",
}

DEFAULT_VLLM_MODEL = "mistralai/Mistral-7B-Instruct-v0.3"
DEFAULT_VLLM_SERVED_MODEL_NAME = "mistral-7b-instruct-v0_3"


def _env_flag(name: str) -> bool:
    """Parse an environment variable as a boolean flag."""

    value = os.environ.get(name, "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _optional_env_positive_int(name: str) -> int | None:
    """Parse an environment variable as a positive integer, or None if unset/0."""

    raw_value = os.environ.get(name, "").strip()
    if raw_value == "" or raw_value == "0":
        return None

    try:
        parsed_value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a positive integer, 0, or blank. Got: {raw_value!r}"
        ) from exc

    if parsed_value < 0:
        raise RuntimeError(
            f"{name} must be a positive integer, 0, or blank. Got: {raw_value!r}"
        )
    if parsed_value == 0:
        return None
    return parsed_value


def _tail_text_file(path: Path, max_lines: int = 120) -> str:
    """Return the tail of a UTF-8 text file for diagnostics."""

    if not path.exists():
        return "(no log output available)"

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(failed to read vLLM log file)"

    if not lines:
        return "(log file is empty)"
    return "\n".join(lines[-max_lines:])


def _collect_gpu_diagnostics() -> str:
    """Collect best-effort GPU memory diagnostics via nvidia-smi."""

    try:
        gpu_mem = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return "(GPU diagnostics unavailable: nvidia-smi not accessible)"

    details: list[str] = []
    if gpu_mem.stdout.strip():
        details.append("GPU memory snapshot (index, name, total MiB, used MiB, free MiB):")
        details.extend(f"  {line}" for line in gpu_mem.stdout.strip().splitlines())

    try:
        procs = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        procs = None

    process_lines = []
    if procs is not None and procs.stdout.strip():
        for raw in procs.stdout.strip().splitlines():
            parts = [part.strip() for part in raw.split(",")]
            if len(parts) < 3:
                process_lines.append(f"  {raw}")
                continue
            pid, process_name, used_mem = parts[0], parts[1], parts[2]
            cmdline = ""
            try:
                cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace"
                )
                cmdline = cmdline_raw.strip()
            except Exception:
                cmdline = ""
            if cmdline:
                process_lines.append(
                    f"  pid={pid} mem={used_mem}MiB name={process_name} cmd={cmdline}"
                )
            else:
                process_lines.append(
                    f"  pid={pid} mem={used_mem}MiB name={process_name}"
                )

    if process_lines:
        details.append("GPU compute processes:")
        details.extend(process_lines)

    if not details:
        return "(no GPU diagnostics available)"
    return "\n".join(details)


def _extract_estimated_max_model_len(log_text: str) -> int | None:
    """Extract vLLM's estimated max model length from KV-cache error logs."""

    match = re.search(r"estimated maximum model length is\s+(\d+)", log_text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _is_tcp_port_in_use(host: str, port: int) -> bool:
    """Return True when a TCP port is already bound/listening."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _choose_vllm_api_port(host: str, preferred_port: int, scan_limit: int) -> int:
    """Pick a free API port, starting from preferred_port and scanning upward."""

    for candidate in range(preferred_port, preferred_port + scan_limit + 1):
        if not _is_tcp_port_in_use(host, candidate):
            return candidate

    raise RuntimeError(
        f"Could not find a free TCP port for vLLM on {host} in range "
        f"{preferred_port}-{preferred_port + scan_limit}."
    )


def _choose_vllm_master_port(
    host: str,
    preferred_port: int,
    scan_limit: int,
    reserved_port: int,
) -> int:
    """Pick a free vLLM distributed master port distinct from the API port."""

    for candidate in range(preferred_port, preferred_port + scan_limit + 1):
        if candidate == reserved_port:
            continue
        if not _is_tcp_port_in_use(host, candidate):
            return candidate

    raise RuntimeError(
        f"Could not find a free vLLM master port on {host} in range "
        f"{preferred_port}-{preferred_port + scan_limit} (excluding API port {reserved_port})."
    )


def _validate_hf_model_access(model_id: str, hf_token: str) -> None:
    """Fail fast if a Hugging Face model ID is inaccessible or invalid."""

    if Path(model_id).expanduser().exists():
        return

    if model_id.startswith("/") or model_id.startswith("./"):
        return

    headers: dict[str, str] = {}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    try:
        response = requests.get(
            f"https://huggingface.co/api/models/{model_id}",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Unable to validate Hugging Face model '{model_id}'. "
            "Check network connectivity or set VLLM_MODEL to a local path."
        ) from exc

    if response.status_code == 200:
        return

    if response.status_code in {401, 403}:
        raise RuntimeError(
            f"Model '{model_id}' requires Hugging Face authentication or access approval. "
            "Set VLLM_HF_TOKEN (or HF_TOKEN), or run `hf auth login`, then retry."
        )

    if response.status_code == 404:
        raise RuntimeError(
            f"Model '{model_id}' was not found on Hugging Face. "
            "Set VLLM_MODEL to a valid public model id or a local model path."
        )

    raise RuntimeError(
        f"Model validation failed for '{model_id}' (HTTP {response.status_code})."
    )


def _wait_for_vllm_ready(
    base_url: str,
    timeout_seconds: int,
    process: subprocess.Popen[str],
    log_path: Path,
) -> None:
    """Wait until the local vLLM server responds on a health endpoint."""

    deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
    probe_paths = ("/health", "/v1/models", "/version")

    while deadline is None or time.monotonic() < deadline:
        if process.poll() is not None:
            log_tail = _tail_text_file(log_path)
            memory_hint = ""
            if "Free memory on device cuda" in log_tail:
                memory_hint = (
                    "\n\nDetected GPU memory pressure while starting vLLM. "
                    "Free GPU memory, reduce VLLM_GPU_MEMORY_UTILIZATION, "
                    "or use USE_VLLM=0 to run via another endpoint.\n\n"
                    f"{_collect_gpu_diagnostics()}"
                )
            kv_cache_hint = ""
            if "estimated maximum model length is" in log_tail:
                estimated_max_len = _extract_estimated_max_model_len(log_tail)
                suggested_len = str(estimated_max_len) if estimated_max_len else "(unknown)"
                kv_cache_hint = (
                    "\n\nDetected KV-cache capacity limit for current settings. "
                    "Lower VLLM_MAX_MODEL_LEN (for example to "
                    f"{suggested_len}) or increase VLLM_GPU_MEMORY_UTILIZATION."
                )
            raise RuntimeError(
                "vLLM exited during startup.\n\n"
                "Last vLLM log lines:\n"
                f"{log_tail}"
                f"{memory_hint}"
                f"{kv_cache_hint}"
            )

        for path in probe_paths:
            try:
                response = requests.get(
                    urljoin(base_url + "/", path.lstrip("/")), timeout=3
                )
                if response.status_code < 500:
                    return
            except requests.RequestException:
                continue
        time.sleep(1)

    assert deadline is not None
    raise RuntimeError(
        f"Timed out waiting for vLLM server at {base_url} after {timeout_seconds}s\n\n"
        "Last vLLM log lines:\n"
        f"{_tail_text_file(log_path)}"
    )


def _stop_process_gracefully(process: subprocess.Popen[str], grace_seconds: int = 15) -> None:
    """Terminate a subprocess and force-kill on timeout without raising cleanup errors."""

    if process.poll() is not None:
        return

    try:
        process_group_id = os.getpgid(process.pid)
    except OSError:
        process_group_id = None

    if process_group_id is not None:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except OSError:
            pass
    else:
        process.terminate()

    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if process_group_id is not None:
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except OSError:
                pass
        else:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def configure_llm_runtime() -> tuple[subprocess.Popen[str] | None, str]:
    """Configure endpoint/auth, optionally launching a local vLLM server."""

    if not _env_flag("USE_VLLM"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            if token:
                os.environ["ANTHROPIC_API_KEY"] = token
        return None, os.environ.get("ANTHROPIC_BASE_URL", "(default)")

    vllm_model = os.environ.get("VLLM_MODEL", DEFAULT_VLLM_MODEL)
    served_model_name = os.environ.get(
        "VLLM_SERVED_MODEL_NAME", DEFAULT_VLLM_SERVED_MODEL_NAME
    )
    host = os.environ.get("VLLM_HOST", "127.0.0.1")
    requested_port = int(os.environ.get("VLLM_PORT", "8000"))
    port_scan_limit = int(os.environ.get("VLLM_PORT_SCAN_LIMIT", "20"))
    port = _choose_vllm_api_port(host, requested_port, port_scan_limit)
    requested_master_port = int(
        os.environ.get("VLLM_MASTER_PORT", str(max(1024, requested_port + 100)))
    )
    master_port_scan_limit = int(os.environ.get("VLLM_MASTER_PORT_SCAN_LIMIT", "100"))
    master_port = _choose_vllm_master_port(
        host=host,
        preferred_port=requested_master_port,
        scan_limit=master_port_scan_limit,
        reserved_port=port,
    )
    max_model_len = int(os.environ.get("VLLM_MAX_MODEL_LEN", "20000"))
    if max_model_len <= 0:
        raise RuntimeError("VLLM_MAX_MODEL_LEN must be a positive integer.")
    auto_adjust_max_model_len = _env_flag("VLLM_AUTO_ADJUST_MAX_MODEL_LEN")
    gpu_memory_utilization = float(
        os.environ.get("VLLM_GPU_MEMORY_UTILIZATION", "0.9")
    )
    fit_profile = _env_flag("VLLM_FIT_PROFILE")
    max_num_seqs_raw = os.environ.get("VLLM_MAX_NUM_SEQS", "").strip()
    max_num_batched_tokens_raw = os.environ.get(
        "VLLM_MAX_NUM_BATCHED_TOKENS", ""
    ).strip()
    enforce_eager = _env_flag("VLLM_ENFORCE_EAGER")
    kv_cache_dtype = os.environ.get("VLLM_KV_CACHE_DTYPE", "").strip()
    cpu_offload_gb_raw = os.environ.get("VLLM_CPU_OFFLOAD_GB", "").strip()
    swap_space_gb_raw = os.environ.get("VLLM_SWAP_SPACE_GB", "").strip()
    load_format = os.environ.get("VLLM_LOAD_FORMAT", "").strip()
    quantization = os.environ.get("VLLM_QUANTIZATION", "").strip()
    dtype = os.environ.get("VLLM_DTYPE", "").strip()
    extra_args_raw = os.environ.get("VLLM_EXTRA_ARGS", "").strip()

    if fit_profile:
        if not max_num_seqs_raw:
            max_num_seqs_raw = "1"
        if not max_num_batched_tokens_raw:
            max_num_batched_tokens_raw = "512"
        if not _env_flag("VLLM_ENFORCE_EAGER"):
            enforce_eager = True

    if max_num_seqs_raw:
        max_num_seqs = int(max_num_seqs_raw)
        if max_num_seqs <= 0:
            raise RuntimeError("VLLM_MAX_NUM_SEQS must be a positive integer.")
    else:
        max_num_seqs = None

    if max_num_batched_tokens_raw:
        max_num_batched_tokens = int(max_num_batched_tokens_raw)
        if max_num_batched_tokens <= 0:
            raise RuntimeError(
                "VLLM_MAX_NUM_BATCHED_TOKENS must be a positive integer."
            )
    else:
        max_num_batched_tokens = None

    if cpu_offload_gb_raw:
        cpu_offload_gb = float(cpu_offload_gb_raw)
        if cpu_offload_gb < 0:
            raise RuntimeError("VLLM_CPU_OFFLOAD_GB must be >= 0.")
    else:
        cpu_offload_gb = None

    if swap_space_gb_raw:
        swap_space_gb = float(swap_space_gb_raw)
        if swap_space_gb < 0:
            raise RuntimeError("VLLM_SWAP_SPACE_GB must be >= 0.")
    else:
        swap_space_gb = None

    extra_args: list[str] = []
    if extra_args_raw:
        try:
            extra_args = shlex.split(extra_args_raw)
        except ValueError as exc:
            raise RuntimeError(
                "VLLM_EXTRA_ARGS could not be parsed. Ensure it is a valid shell-style "
                "argument string."
            ) from exc

    api_key = os.environ.get("VLLM_API_KEY", "dummy")
    tool_call_parser = os.environ.get("VLLM_TOOL_CALL_PARSER", "mistral")
    wait_timeout_seconds = int(os.environ.get("VLLM_STARTUP_TIMEOUT", "1800"))
    hf_token = os.environ.get("VLLM_HF_TOKEN", "").strip() or os.environ.get(
        "HF_TOKEN", ""
    ).strip()

    if "/" in served_model_name:
        raise RuntimeError(
            "VLLM_SERVED_MODEL_NAME cannot contain '/'. Use an alias such as "
            f"'{DEFAULT_VLLM_SERVED_MODEL_NAME}'."
        )

    _validate_hf_model_access(vllm_model, hf_token)

    endpoint = f"http://{host}:{port}"
    effective_max_model_len = max_model_len
    process: subprocess.Popen[str] | None = None
    log_path: Path | None = None
    launch_error: Exception | None = None

    for attempt in range(2):
        serve_cmd = [
            "vllm",
            "serve",
            vllm_model,
            "--host",
            host,
            "--port",
            str(port),
            "--master-port",
            str(master_port),
            "--served-model-name",
            served_model_name,
            "--max-model-len",
            str(effective_max_model_len),
            "--gpu-memory-utilization",
            str(gpu_memory_utilization),
            "--api-key",
            api_key,
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            tool_call_parser,
        ]
        if max_num_seqs is not None:
            serve_cmd.extend(["--max-num-seqs", str(max_num_seqs)])
        if max_num_batched_tokens is not None:
            serve_cmd.extend(
                ["--max-num-batched-tokens", str(max_num_batched_tokens)]
            )
        if enforce_eager:
            serve_cmd.append("--enforce-eager")
        if kv_cache_dtype:
            serve_cmd.extend(["--kv-cache-dtype", kv_cache_dtype])
        if cpu_offload_gb is not None:
            serve_cmd.extend(["--cpu-offload-gb", str(cpu_offload_gb)])
        if swap_space_gb is not None:
            serve_cmd.extend(["--swap-space", str(swap_space_gb)])
        if load_format:
            serve_cmd.extend(["--load-format", load_format])
        if quantization:
            serve_cmd.extend(["--quantization", quantization])
        if dtype:
            serve_cmd.extend(["--dtype", dtype])
        if extra_args:
            serve_cmd.extend(extra_args)
        if hf_token:
            serve_cmd.extend(["--hf-token", hf_token])

        log_dir = PROJECT_ROOT / ".cache"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"vllm-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log"
        log_handle = log_path.open("w", encoding="utf-8")

        try:
            process = subprocess.Popen(
                serve_cmd,
                cwd=str(PROJECT_ROOT),
                text=True,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            log_handle.close()
            raise RuntimeError(
                "USE_VLLM=1 but 'vllm' executable was not found. "
                "Install vLLM in the active environment."
            ) from exc
        finally:
            log_handle.close()

        try:
            _wait_for_vllm_ready(endpoint, wait_timeout_seconds, process, log_path)
            launch_error = None
            break
        except Exception as exc:
            launch_error = exc
            _stop_process_gracefully(process)
            process = None
            if not auto_adjust_max_model_len or attempt > 0:
                break
            log_tail = _tail_text_file(log_path, max_lines=300)
            estimated_max_len = _extract_estimated_max_model_len(log_tail)
            if estimated_max_len is None:
                break
            if estimated_max_len >= effective_max_model_len:
                break
            effective_max_model_len = estimated_max_len
            os.environ["VLLM_AUTO_ADJUSTED_MAX_MODEL_LEN"] = str(effective_max_model_len)

    if launch_error is not None:
        raise launch_error
    assert process is not None
    assert log_path is not None

    prior_model_env = os.environ.get("MODEL", "")

    os.environ["ANTHROPIC_BASE_URL"] = endpoint
    os.environ["ANTHROPIC_AUTH_TOKEN"] = api_key
    os.environ["ANTHROPIC_API_KEY"] = api_key
    os.environ.setdefault("ANTHROPIC_DEFAULT_OPUS_MODEL", served_model_name)
    os.environ.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", served_model_name)
    os.environ.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", served_model_name)
    os.environ["MODEL"] = served_model_name
    if prior_model_env and prior_model_env != served_model_name:
        os.environ["VLLM_ORIGINAL_MODEL_ENV"] = prior_model_env
    os.environ["VLLM_LOG_PATH"] = str(log_path)
    os.environ["VLLM_REQUESTED_PORT"] = str(requested_port)
    os.environ["VLLM_EFFECTIVE_PORT"] = str(port)
    os.environ["VLLM_REQUESTED_MASTER_PORT"] = str(requested_master_port)
    os.environ["VLLM_EFFECTIVE_MASTER_PORT"] = str(master_port)
    os.environ["VLLM_REQUESTED_MAX_MODEL_LEN"] = str(max_model_len)
    os.environ["VLLM_EFFECTIVE_MAX_MODEL_LEN"] = str(effective_max_model_len)
    return process, endpoint


def _validate_agent_context_capacity() -> str | None:
    """Return a non-blocking advisory when vLLM context appears too small."""

    if not _env_flag("USE_VLLM"):
        return None

    try:
        effective_max = int(os.environ.get("VLLM_EFFECTIVE_MAX_MODEL_LEN", "0"))
        required_min = int(os.environ.get("AGENT_MIN_CONTEXT_TOKENS", "16000"))
    except ValueError:
        return None

    if required_min <= 0:
        return None

    if effective_max < required_min:
        return (
            "vLLM context advisory: effective capacity appears below workflow target.\n\n"
            f"Effective vLLM max context: {effective_max} tokens\n"
            f"Advisory minimum (AGENT_MIN_CONTEXT_TOKENS): {required_min} tokens\n\n"
            "vLLM ultimately decides the runnable context at startup. This check is guidance only.\n"
            "If requests fail with context/token-limit errors, consider:\n"
            "- Use USE_VLLM=0 to route through ANTHROPIC_BASE_URL instead.\n"
            "- Tune VLLM_MAX_MODEL_LEN / VLLM_GPU_MEMORY_UTILIZATION / fit-profile vars.\n"
            "- Reduce prompt/tool payload, then lower AGENT_MIN_CONTEXT_TOKENS accordingly."
        )

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FeedSubscription(BaseModel):
    """A single RSS/Atom feed subscription extracted from an OPML file."""

    name: str = Field(description="Human-readable display name of the feed")
    xml_url: str = Field(description="Full URL of the RSS or Atom feed")
    category: str = Field(
        default="Uncategorized",
        description="Category or folder the feed is grouped under in the OPML",
    )
    source_file: str = Field(
        description="Filename of the OPML file this subscription was parsed from"
    )


class Article(BaseModel):
    """
    Fully-populated article suitable for rendering as a web 'card'.

    Every field has a description so LLM agents understand its purpose.
    All tool parameters that produce an Article MUST populate every field.
    """

    title: str = Field(description="The headline / title of the article")
    canonical_url: str = Field(
        description="The canonical permalink URL of the article"
    )
    feed_name: str = Field(
        description="Human-readable name of the RSS/Atom feed this article belongs to"
    )
    feed_url: str = Field(description="URL of the RSS/Atom feed itself")
    category: str = Field(
        default="Uncategorized",
        description="Category the feed is grouped under (from OPML)",
    )
    author: str = Field(
        default="Unknown", description="Author or byline of the article"
    )
    published_date: str = Field(
        description="ISO 8601 date-time when the article was published"
    )
    summary: str = Field(
        default="", description="A short plain-text summary or excerpt of the article"
    )
    content_html: str = Field(
        default="",
        description="Full or partial article body as HTML (for card rendering)",
    )
    image_url: str = Field(
        default="",
        description="URL of the hero / title image to display on the card",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Keywords, tags, or categories associated with the article",
    )
    language: str = Field(
        default="en", description="ISO 639-1 language code of the article"
    )

    # ── Metadata ──
    fetched_at: str = Field(
        description="ISO 8601 timestamp of when this article data was fetched/saved"
    )
    source_opml: str = Field(
        description="Filename of the OPML file that contained this feed subscription"
    )
    guid: str = Field(
        description=(
            "Globally unique identifier for the article — "
            "the feed's own GUID, or a SHA-256 hash derived from the link+title"
        )
    )
    content_kind: Literal["article", "podcast", "video"] = Field(
        default="article",
        description="Primary content type for rendering and UX treatment",
    )
    media_assets: list["MediaAsset"] = Field(
        default_factory=list,
        description=(
            "Structured media attachments for the item (e.g. mp3/mp4/youtube). "
            "Useful for podcast/video cards and playback UIs."
        ),
    )


class MediaAsset(BaseModel):
    """A single playable media attachment associated with an article/feed item."""

    kind: Literal["audio", "video", "youtube"] = Field(
        description="Media type for player selection"
    )
    url: str = Field(description="Absolute HTTPS URL to the media resource")
    mime_type: str = Field(
        default="",
        description="MIME type when available (e.g. audio/mpeg, video/mp4)",
    )
    title: str = Field(
        default="",
        description="Human-friendly media label, usually episode title",
    )
    duration_seconds: int | None = Field(
        default=None,
        description="Duration in seconds when available from feed metadata",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helper Functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def slugify(text: str, max_length: int = 80) -> str:
    """Convert arbitrary text to a filesystem-safe, lowercase slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_length] if text else "unnamed"


def _to_str(value: Any) -> str:
    """Convert a loosely-typed value to a safe string for typed storage."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    return str(value)


def _is_https_url(url: str) -> bool:
    """Return True only for absolute https:// URLs."""

    candidate = url.strip()
    if not candidate:
        return False
    parsed = urlparse(candidate)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _html_to_plain_text(raw_html: str) -> str:
    """Convert HTML (or plain text) into safe plain text."""

    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)


def _sanitize_content_html(raw_html: str) -> str:
    """Sanitize article HTML to semantic markup only.

    Security policy:
      - Remove scripts, styles, forms, embeds, and similar active content.
      - Remove inline styling and non-whitelisted attributes.
      - Keep semantic formatting elements.
      - Any hyperlink whose href is not absolute https:// is converted to text.
      - Images are kept only when src is absolute https://.
    """

    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup.find_all(True):
        name = tag.name.lower()

        if name in STRIP_ENTIRELY_TAGS:
            tag.decompose()
            continue

        if name not in SAFE_HTML_TAGS:
            tag.unwrap()
            continue

        if name == "a":
            href = _to_str(tag.get("href", "")).strip()
            if not _is_https_url(href):
                tag.unwrap()
                continue

            for attr in list(tag.attrs):
                if attr not in {"href", "title"}:
                    del tag.attrs[attr]
            tag["href"] = href
            tag["rel"] = "noopener noreferrer nofollow"
            tag["target"] = "_blank"
            continue

        if name == "img":
            src = _to_str(tag.get("src", "")).strip()
            if not _is_https_url(src):
                tag.decompose()
                continue

            alt_text = _to_str(tag.get("alt", ""))
            title_text = _to_str(tag.get("title", ""))
            tag.attrs = {"src": src}
            if alt_text:
                tag["alt"] = alt_text
            if title_text:
                tag["title"] = title_text
            tag["loading"] = "lazy"
            continue

        tag.attrs = {}

    return str(soup)


def _parse_duration_seconds(raw_value: Any) -> int | None:
    """Parse duration values like '3720' or '01:02:00' into seconds."""

    if raw_value is None:
        return None

    value = _to_str(raw_value).strip()
    if not value:
        return None

    if value.isdigit():
        return int(value)

    parts = value.split(":")
    if all(part.strip().isdigit() for part in parts):
        numbers = [int(part.strip()) for part in parts]
        if len(numbers) == 3:
            return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        if len(numbers) == 2:
            return numbers[0] * 60 + numbers[1]
        if len(numbers) == 1:
            return numbers[0]
    return None


def _infer_media_kind(url: str, mime_type: str) -> Literal["audio", "video", "youtube"] | None:
    """Infer media kind from URL/mime hints."""

    lower_url = url.lower()
    lower_mime = mime_type.lower()
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"

    if lower_mime.startswith("audio/"):
        return "audio"
    if lower_mime.startswith("video/"):
        return "video"

    if any(lower_url.endswith(ext) for ext in (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac")):
        return "audio"
    if any(lower_url.endswith(ext) for ext in (".mp4", ".m4v", ".mov", ".webm", ".mkv")):
        return "video"

    return None


def _extract_media_assets(
    entry: Any,
    canonical_url: str,
    content_html: str,
) -> list[MediaAsset]:
    """Extract mp3/mp4/youtube media assets from feed entry metadata/content."""

    seen_urls: set[str] = set()
    assets: list[MediaAsset] = []

    def add_candidate(url: str, mime_type: str, title: str, duration: int | None) -> None:
        clean_url = _to_str(url).strip()
        if not _is_https_url(clean_url):
            return
        if clean_url in seen_urls:
            return

        media_kind = _infer_media_kind(clean_url, _to_str(mime_type))
        if media_kind is None:
            return

        seen_urls.add(clean_url)
        assets.append(
            MediaAsset(
                kind=media_kind,
                url=clean_url,
                mime_type=_to_str(mime_type),
                title=_to_str(title),
                duration_seconds=duration,
            )
        )

    duration_seconds = _parse_duration_seconds(
        getattr(entry, "itunes_duration", None) or getattr(entry, "duration", None)
    )
    title = _to_str(getattr(entry, "title", ""))

    enclosures = getattr(entry, "enclosures", []) or []
    for enclosure in enclosures:
        if isinstance(enclosure, dict):
            add_candidate(
                enclosure.get("href", enclosure.get("url", "")),
                enclosure.get("type", ""),
                title,
                duration_seconds,
            )

    media_content = getattr(entry, "media_content", []) or []
    for media in media_content:
        if isinstance(media, dict):
            add_candidate(
                media.get("url", ""),
                media.get("type", ""),
                title,
                duration_seconds,
            )

    links = getattr(entry, "links", []) or []
    for link in links:
        if isinstance(link, dict):
            rel = _to_str(link.get("rel", "")).lower()
            if rel in {"enclosure", "alternate"}:
                add_candidate(
                    link.get("href", ""),
                    link.get("type", ""),
                    title,
                    duration_seconds,
                )

    add_candidate(canonical_url, "", title, duration_seconds)

    if content_html:
        soup = BeautifulSoup(content_html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            add_candidate(_to_str(anchor.get("href", "")), "", title, duration_seconds)
        for iframe in soup.find_all("iframe", src=True):
            add_candidate(_to_str(iframe.get("src", "")), "video/mp4", title, duration_seconds)

    return assets


def article_filepath(
    feed_name: str, published_date: str, article_title: str
) -> Path:
    """Compute the canonical file path for an article JSON file.

    Pattern: data/articles/<feed-slug>/<yyyy-mm-dd_HHmmss>-<feed-slug>-<title-slug>.json
    """
    feed_slug = slugify(feed_name)
    title_slug = slugify(article_title, max_length=60)
    try:
        dt = datetime.fromisoformat(published_date)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    date_prefix = dt.strftime("%Y-%m-%d_%H%M%S")
    filename = f"{date_prefix}-{feed_slug}-{title_slug}.json"
    return ARTICLES_DIR / feed_slug / filename


def compute_guid(entry: dict[str, Any], feed_url: str) -> str:
    """Derive a stable GUID for a feed entry.

    Prefers the feed's own <id>/<guid>, falls back to <link>,
    and finally hashes feed_url + title + date.
    """
    if entry.get("id"):
        return str(entry["id"])
    if entry.get("link"):
        return str(entry["link"])
    raw = f"{feed_url}|{entry.get('title', '')}|{entry.get('published', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_entry_date(entry: Any) -> datetime | None:
    """Try to extract a timezone-aware datetime from a feedparser entry."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    # Try ISO 8601 string fields
    for field in ("published", "updated", "created"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                pass
    return None


def _extract_entry_image(entry: Any) -> str:
    """Extract the best image URL from a feedparser entry."""
    # media:content
    if hasattr(entry, "media_content") and entry.media_content:
        for mc in entry.media_content:
            if mc.get("medium") == "image" or mc.get("type", "").startswith(
                "image"
            ):
                url = mc.get("url", "")
                if url:
                    return url
    # media:thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        url = entry.media_thumbnail[0].get("url", "")
        if url:
            return url
    # enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image"):
                url = enc.get("href", enc.get("url", ""))
                if url:
                    return url
    # image embedded in content/summary
    for attr in ("summary", "description"):
        html = getattr(entry, attr, "")
        if html and "<img" in str(html):
            soup = BeautifulSoup(str(html), "html.parser")
            img = soup.find("img", src=True)
            if img:
                return _to_str(img.get("src", ""))
    return ""


def _extract_entry_content(entry: Any) -> str:
    """Extract article body HTML from a feedparser entry."""
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    if hasattr(entry, "summary_detail"):
        return getattr(entry.summary_detail, "value", "")
    return getattr(entry, "summary", "")


def _scrape_page_metadata(url: str) -> dict[str, str]:
    """Fetch a URL and extract OG/meta tags, canonical URL, and hero image.

    Returns a dict with keys: image_url, canonical_url, description, author,
    site_name, body_html (truncated to 5000 chars).
    """
    result: dict[str, str] = {}
    try:
        resp = requests.get(
            url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
    except Exception:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Open Graph & meta tags
    for meta in soup.find_all("meta"):
        prop = _to_str(meta.get("property", "") or meta.get("name", ""))
        content = _to_str(meta.get("content", ""))
        if not content:
            continue
        if prop == "og:image":
            result.setdefault("image_url", content)
        elif prop == "og:description":
            result.setdefault("description", content)
        elif prop in ("og:author", "author", "article:author"):
            result.setdefault("author", content)
        elif prop == "og:site_name":
            result.setdefault("site_name", content)
        elif prop == "twitter:image":
            result.setdefault("image_url", content)
        elif prop == "description":
            result.setdefault("description", content)

    # Canonical URL
    link_tag = soup.find("link", rel="canonical")
    if link_tag and link_tag.get("href"):
        result["canonical_url"] = _to_str(link_tag.get("href", ""))

    # Article body (best-effort extraction)
    for selector in (
        "article",
        '[role="main"]',
        ".post-content",
        ".entry-content",
        ".article-body",
        ".article-content",
        "main",
    ):
        el = soup.select_one(selector)
        if el:
            result["body_html"] = str(el)[:5000]
            break

    return result


def _load_existing_guids(feed_slug: str) -> set[str]:
    """Load all known GUIDs from existing article files for a feed directory."""
    feed_dir = ARTICLES_DIR / feed_slug
    guids: set[str] = set()
    if not feed_dir.exists():
        return guids
    for json_path in feed_dir.glob("*.json"):
        try:
            with open(json_path) as f:
                data = json.load(f)
            g = data.get("guid", "")
            if g:
                guids.add(g)
        except Exception:
            continue
    return guids


def _get_opml_path(source_opml: str) -> Path:
    """Resolve an OPML filename to an absolute path under data/."""
    return DATA_DIR / source_opml


def _update_feed_url_in_opml(
    source_opml: str,
    feed_name: str,
    old_feed_url: str,
    new_feed_url: str,
) -> bool:
    """Update a feed xmlUrl in the source OPML file.

    Matches primarily by xmlUrl, and prefers a matching feed title when
    duplicates exist.
    """
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    exact_match: ET.Element | None = None
    fallback_match: ET.Element | None = None
    for outline in root.iter("outline"):
        xml_url = _to_str(outline.get("xmlUrl", ""))
        if xml_url != old_feed_url:
            continue
        if fallback_match is None:
            fallback_match = outline
        if _to_str(outline.get("text", "")) == feed_name:
            exact_match = outline
            break

    target = exact_match if exact_match is not None else fallback_match
    if target is None:
        return False

    target.set("xmlUrl", new_feed_url)
    tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return True


def _remove_feed_from_opml(source_opml: str, feed_name: str, feed_url: str) -> bool:
    """Remove a feed outline from its source OPML file."""
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    removed = False
    for parent in root.iter():
        for child in list(parent):
            if child.tag != "outline":
                continue
            child_url = _to_str(child.get("xmlUrl", ""))
            child_name = _to_str(child.get("text", ""))
            if child_url == feed_url and (child_name == feed_name or not feed_name):
                parent.remove(child)
                removed = True
                break
        if removed:
            break

    if removed:
        tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return removed


def _update_feed_category_in_opml(
    source_opml: str,
    feed_name: str,
    feed_url: str,
    new_category: str,
) -> bool:
    """Update a feed category in its source OPML file."""
    opml_path = _get_opml_path(source_opml)
    if not opml_path.exists():
        return False

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
    except Exception:
        return False

    updated = False
    fallback: ET.Element | None = None
    for outline in root.iter("outline"):
        xml_url = _to_str(outline.get("xmlUrl", ""))
        if xml_url != feed_url:
            continue
        if fallback is None:
            fallback = outline
        if _to_str(outline.get("text", "")) == feed_name:
            outline.set("category", new_category)
            updated = True
            break

    if not updated and fallback is not None:
        fallback.set("category", new_category)
        updated = True

    if updated:
        tree.write(opml_path, encoding="utf-8", xml_declaration=True)
    return updated


def _collect_known_categories() -> list[str]:
    """Collect existing categories from feed*.opml files."""
    categories: set[str] = set()
    for opml_path in sorted(glob.glob(str(DATA_DIR / "feed*.opml"))):
        try:
            tree = ET.parse(opml_path)
        except Exception:
            continue
        for outline in tree.getroot().iter("outline"):
            cat = _to_str(outline.get("category", "")).strip()
            if cat and cat.lower() != "uncategorized":
                categories.add(cat)
    return sorted(categories)


def _infer_category_for_feed(
    feed_name: str,
    feed_url: str,
    feed_title: str,
    feed_description: str,
    known_categories: list[str],
) -> str:
    """Infer a best-fit category for a feed when category is missing.

    Uses keyword matching against the feed name, URL, title, and description.
    Only matches against categories that actually exist in the OPML.
    Falls back to "News" (if present) or "Uncategorized" rather than
    guessing wrong.
    """
    haystack = " ".join(
        [
            _to_str(feed_name).lower(),
            _to_str(feed_url).lower(),
            _to_str(feed_title).lower(),
            _to_str(feed_description).lower(),
        ]
    )

    # Ordered by specificity — most specific categories first to avoid
    # false positives from broad keywords.  Only categories that exist
    # in the OPML are considered (checked via known_lookup below).
    keyword_priority: list[tuple[str, list[str]]] = [
        ("Cryptography", ["cryptograph", "cipher", "encryption", "cr.yp.to"]),
        ("Security", ["security", "infosec", "cyber", "owasp", "malware", "threat", "vulnerability"]),
        ("Intelligence", ["intelligence", "espionage", "geopolitics", "osint"]),
        ("Science", ["science", "research", "journal", "nature", "physics", "biology"]),
        ("Space", ["space", "nasa", "astronomy", "rocket", "satellite"]),
        ("Programming", ["python", "developer", "coding", "programming", "software", "github"]),
        ("Electronics", ["electronics", "hardware", "circuit", "embedded", "arduino"]),
        ("Gaming", ["gaming", "game", "esports", "playstation", "xbox"]),
        ("Weather", ["weather", "forecast", "solar storm", "met office"]),
        ("Energy", ["energy", "renewable", "solar power", "grid"]),
        ("Comedy", ["comedy", "humor", "humour", "satire", "stand-up"]),
        ("Entertainment", ["entertainment", "movie", "film", "music", "drama", "theatre"]),
        ("Podcasts", ["podcast", "libsyn", "megaphone", "podbean", "buzzsprout", "simplecast"]),
        ("News", ["news", "breaking", "headlines", "world news", "bbc", "guardian", "reuters"]),
        ("Tech", ["tech", "technology", "ai", "cloud", "startup", "openai"]),
    ]

    known_lookup = {c.lower(): c for c in known_categories}
    for target, keywords in keyword_priority:
        if target.lower() not in known_lookup:
            continue
        if any(keyword in haystack for keyword in keywords):
            return known_lookup[target.lower()]

    # Conservative fallback: prefer "News" over "Tech" since news is
    # the most common generic content type; never guess niche categories.
    for fallback_cat in ("News", "Tech"):
        if fallback_cat in known_categories:
            return fallback_cat
    return "Uncategorized"


def _append_retired_feed_to_archive(
    feed_name: str,
    feed_url: str,
    category: str,
    source_opml: str,
    reason: str,
) -> bool:
    """Append a retired feed to data/archive/retired-feeds.opml.

    Skips insertion if a matching xmlUrl already exists.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    if RETIRED_FEEDS_OPML.exists():
        try:
            tree = ET.parse(RETIRED_FEEDS_OPML)
            root = tree.getroot()
        except Exception:
            return False
    else:
        root = ET.Element("opml", {"version": "2.0"})
        head = ET.SubElement(root, "head")
        ET.SubElement(head, "title").text = "Retired Feed Subscriptions"
        ET.SubElement(root, "body")
        tree = ET.ElementTree(root)

    body = root.find("body")
    if body is None:
        body = ET.SubElement(root, "body")

    for outline in root.iter("outline"):
        if _to_str(outline.get("xmlUrl", "")) == feed_url:
            return True

    retired_at = datetime.now(timezone.utc).isoformat()
    ET.SubElement(
        body,
        "outline",
        {
            "type": "rss",
            "text": feed_name,
            "xmlUrl": feed_url,
            "category": category or "Uncategorized",
            "sourceOpml": source_opml,
            "retiredReason": reason,
            "retiredAt": retired_at,
        },
    )
    tree.write(RETIRED_FEEDS_OPML, encoding="utf-8", xml_declaration=True)
    return True


def _probe_feed_url(feed_url: str) -> tuple[int | None, str]:
    """Probe a feed URL without following redirects.

    Returns (status_code, redirect_location). redirect_location is an absolute
    URL when present.
    """
    try:
        resp = requests.get(
            feed_url,
            headers=HTTP_HEADERS,
            timeout=HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except Exception:
        return None, ""

    location = _to_str(resp.headers.get("Location", ""))
    absolute_location = urljoin(feed_url, location) if location else ""
    return resp.status_code, absolute_location


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-Python helpers for OPML parsing & run statistics
#  ─ Used by main() to avoid routing these through the LLM agent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def discover_subscriptions(
    glob_pattern: str = "feed*.opml",
) -> list[FeedSubscription]:
    """Parse all OPML files matching *glob_pattern* under data/ and return a
    flat list of :class:`FeedSubscription` objects.

    This is the pure-Python equivalent of the ``parse_opml_files`` MCP tool —
    called directly from ``main()`` so OPML parsing never touches the LLM.
    """

    opml_paths = sorted(glob.glob(str(DATA_DIR / glob_pattern)))
    if not opml_paths:
        print(f"  [warn] No OPML files matched pattern: {glob_pattern}")
        return []

    subscriptions: list[FeedSubscription] = []
    for fpath in opml_paths:
        try:
            tree = ET.parse(fpath)
            for outline in tree.getroot().iter("outline"):
                xml_url = outline.get("xmlUrl")
                if xml_url:
                    subscriptions.append(
                        FeedSubscription(
                            name=outline.get("text", "Unnamed Feed"),
                            xml_url=xml_url,
                            category=outline.get("category", "Uncategorized")
                            or "Uncategorized",
                            source_file=Path(fpath).name,
                        )
                    )
        except Exception as exc:
            print(f"  [warn] Failed to parse {fpath}: {exc}")

    print(f"  OPML files found: {len(opml_paths)}")
    print(f"  Total subscriptions: {len(subscriptions)}")
    return subscriptions


def compute_run_statistics() -> dict[str, Any]:
    """Scan data/articles/ and return run statistics as a plain dict.

    This is the pure-Python equivalent of the ``get_run_statistics`` MCP tool.
    """

    if not ARTICLES_DIR.exists():
        return {
            "total_feeds": 0,
            "total_articles": 0,
            "missing_image_count": 0,
            "missing_content_count": 0,
            "per_feed": [],
        }

    per_feed: list[dict[str, Any]] = []
    total_articles = 0
    missing_image = 0
    missing_content = 0

    for feed_dir in sorted(ARTICLES_DIR.iterdir()):
        if not feed_dir.is_dir():
            continue
        count = 0
        for json_file in feed_dir.glob("*.json"):
            count += 1
            try:
                with open(json_file) as f:
                    data = json.load(f)
                if not data.get("image_url"):
                    missing_image += 1
                if not data.get("content_html"):
                    missing_content += 1
            except Exception:
                pass
        total_articles += count
        per_feed.append({"feed_slug": feed_dir.name, "article_count": count})

    return {
        "total_feeds": len(per_feed),
        "total_articles": total_articles,
        "missing_image_count": missing_image,
        "missing_content_count": missing_content,
        "per_feed": per_feed,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP Tool Definitions
#  ─ Each tool has:
#      • JSON Schema input_schema with 'description' on every property
#      • All parameters are required (no defaults in tool schemas)
#      • A docstring explaining purpose and return shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@tool(
    "parse_opml_files",
    (
        "Parse all data/feed*.opml files and return a JSON array of feed "
        "subscriptions. Each subscription includes name, xml_url, category, "
        "and source_file. Use this as the first step to discover feeds."
    ),
    {
        "type": "object",
        "properties": {
            "glob_pattern": {
                "type": "string",
                "description": (
                    "Glob pattern relative to the data/ directory to match "
                    "OPML files, e.g. 'feed*.opml' or '*.opml'"
                ),
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for paginating subscriptions",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of subscriptions to return per page",
            }
        },
        "required": ["glob_pattern", "page", "page_size"],
    },
)
async def parse_opml_files(args: dict[str, Any]) -> dict[str, Any]:
    """Parse OPML files matching the glob pattern and extract all RSS/Atom
    feed subscriptions.

    Returns JSON with keys:
        opml_files_found  - number of OPML files matched
        total_subscriptions - total valid subscriptions extracted
        subscriptions - list of {name, xml_url, category, source_file}
    """
    pattern = args["glob_pattern"]
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc)})
    opml_paths = sorted(glob.glob(str(DATA_DIR / pattern)))

    if not opml_paths:
        return _text_result(
            {
                "error": f"No OPML files matched pattern: {pattern}",
                "subscriptions": [],
            }
        )

    subscriptions: list[dict[str, Any]] = []
    for fpath in opml_paths:
        try:
            tree = ET.parse(fpath)
            for outline in tree.getroot().iter("outline"):
                xml_url = outline.get("xmlUrl")
                if xml_url:
                    sub = FeedSubscription(
                        name=outline.get("text", "Unnamed Feed"),
                        xml_url=xml_url,
                        category=outline.get("category", "Uncategorized")
                        or "Uncategorized",
                        source_file=Path(fpath).name,
                    )
                    subscriptions.append(sub.model_dump())
        except Exception as exc:
            subscriptions.append({"error": f"Failed to parse {fpath}: {exc}"})

    valid_subscriptions = [s for s in subscriptions if "xml_url" in s]
    subscriptions_page, pagination = _paginate_items(valid_subscriptions, page, page_size)

    return _text_result(
        {
            "opml_files_found": len(opml_paths),
            "total_subscriptions": len(valid_subscriptions),
            "subscriptions": subscriptions_page,
            "pagination": pagination,
        }
    )


@tool(
    "process_single_feed",
    (
        "Full pipeline for ONE feed: fetch the RSS/Atom feed, filter to the "
        "last 3 days, deduplicate against already-saved articles, auto-enrich "
        "missing image/content by scraping the article page, and save all new "
        "articles as JSON. Returns a summary of what was saved and skipped."
    ),
    {
        "type": "object",
        "properties": {
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the feed",
            },
            "feed_url": {
                "type": "string",
                "description": "Full URL of the RSS or Atom feed to fetch",
            },
            "category": {
                "type": "string",
                "description": "Category of the feed from the OPML file",
            },
            "source_opml": {
                "type": "string",
                "description": "Filename of the OPML file this feed came from",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for paginating tool output lists",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of list items to return per page",
            },
            "chunk_page": {
                "type": "integer",
                "description": "1-based chunk page for recent feed entries to process in this call",
            },
            "chunk_size": {
                "type": "integer",
                "description": "Number of recent feed entries to process in this call",
            },
        },
        "required": [
            "feed_name",
            "feed_url",
            "category",
            "source_opml",
            "page",
            "page_size",
            "chunk_page",
            "chunk_size",
        ],
    },
)
async def process_single_feed(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch one RSS/Atom feed, filter to last 3 days, deduplicate, enrich
    missing fields by scraping, and save new articles as JSON.

    Returns JSON with keys:
        feed_name, feed_url, status,
        total_entries, recent_entries, new_saved, duplicates_skipped,
        enriched_count, errors, saved_articles[]
    """
    feed_name: str = args["feed_name"]
    feed_url: str = args["feed_url"]
    category: str = args["category"]
    source_opml: str = args["source_opml"]
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
        chunk_page = _require_positive_int(args["chunk_page"], "chunk_page")
        chunk_size = _require_positive_int(args["chunk_size"], "chunk_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc)})

    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)
    feed_slug = slugify(feed_name)
    now_iso = datetime.now(timezone.utc).isoformat()
    effective_feed_url = feed_url
    opml_updated = False
    category_assigned = False
    category_updated_in_opml = False

    status_code, redirect_url = _probe_feed_url(feed_url)

    if status_code in PERMANENT_REDIRECT_CODES and redirect_url:
        effective_feed_url = redirect_url
        opml_updated = _update_feed_url_in_opml(
            source_opml=source_opml,
            feed_name=feed_name,
            old_feed_url=feed_url,
            new_feed_url=redirect_url,
        )

    if status_code == 404:
        archived = _append_retired_feed_to_archive(
            feed_name=feed_name,
            feed_url=feed_url,
            category=category,
            source_opml=source_opml,
            reason="404 Not Found",
        )
        removed = _remove_feed_from_opml(
            source_opml=source_opml,
            feed_name=feed_name,
            feed_url=feed_url,
        )
        return _text_result(
            {
                "feed_name": feed_name,
                "feed_url": feed_url,
                "status": "retired",
                "reason": "404 Not Found",
                "removed_from_opml": removed,
                "archived_to": str(RETIRED_FEEDS_OPML),
                "archived": archived,
                "new_saved": 0,
            }
        )

    # ── Fetch feed ──────────────────────────────────────────────────────────
    try:
        feed = feedparser.parse(effective_feed_url)
        if feed.bozo and not feed.entries:
            return _text_result(
                {
                    "feed_name": feed_name,
                    "feed_url": effective_feed_url,
                    "status": "error",
                    "error": f"Feed parse error: {feed.bozo_exception}",
                    "feed_url_updated_in_opml": opml_updated,
                    "new_saved": 0,
                }
            )
    except Exception as exc:
        return _text_result(
            {
                "feed_name": feed_name,
                "feed_url": effective_feed_url,
                "status": "error",
                "error": str(exc),
                "feed_url_updated_in_opml": opml_updated,
                "new_saved": 0,
            }
        )

    if (not category.strip()) or (category.lower() == "uncategorized"):
        feed_meta = getattr(feed, "feed", None)
        feed_title = ""
        feed_description = ""
        if isinstance(feed_meta, dict):
            feed_title = _to_str(feed_meta.get("title", ""))
            feed_description = _to_str(
                feed_meta.get("subtitle", feed_meta.get("description", ""))
            )
        elif feed_meta is not None:
            feed_title = _to_str(getattr(feed_meta, "title", ""))
            feed_description = _to_str(
                getattr(feed_meta, "subtitle", getattr(feed_meta, "description", ""))
            )

        known_categories = _collect_known_categories()
        inferred_category = _infer_category_for_feed(
            feed_name=feed_name,
            feed_url=effective_feed_url,
            feed_title=feed_title,
            feed_description=feed_description,
            known_categories=known_categories,
        )
        if inferred_category and inferred_category.lower() != "uncategorized":
            category = inferred_category
            category_assigned = True
            category_updated_in_opml = _update_feed_category_in_opml(
                source_opml=source_opml,
                feed_name=feed_name,
                feed_url=feed_url,
                new_category=category,
            )

    # ── Load existing GUIDs for dedup ───────────────────────────────────────
    existing_guids = _load_existing_guids(feed_slug)

    feed_lang = ""
    feed_meta = getattr(feed, "feed", None)
    if feed_meta is not None:
        if isinstance(feed_meta, dict):
            feed_lang = _to_str(feed_meta.get("language", ""))
        else:
            feed_lang = _to_str(getattr(feed_meta, "language", ""))
    feed_lang = feed_lang or "en"

    saved_articles: list[dict[str, str]] = []
    skipped_dupes = 0
    skipped_old = 0
    enriched_count = 0
    errors: list[str] = []

    recent_entries: list[Any] = []
    for entry in feed.entries:
        pub_date = _parse_entry_date(entry)
        if pub_date and pub_date < cutoff:
            skipped_old += 1
            continue
        recent_entries.append(entry)

    entries_to_process, chunk_pagination = _paginate_items(
        recent_entries,
        chunk_page,
        chunk_size,
    )

    for entry in entries_to_process:
        # ── Date filter (kept for safety when dates are malformed) ──────────
        pub_date = _parse_entry_date(entry)
        if pub_date and pub_date < cutoff:
            continue

        # ── GUID & dedup ────────────────────────────────────────────────────
        guid = compute_guid(entry, effective_feed_url)
        if guid in existing_guids:
            skipped_dupes += 1
            continue

        # ── Extract data from feed entry ────────────────────────────────────
        title = getattr(entry, "title", "Untitled") or "Untitled"
        link = getattr(entry, "link", "") or ""
        author = getattr(entry, "author", "Unknown") or "Unknown"
        summary = _html_to_plain_text(_to_str(getattr(entry, "summary", "") or ""))
        if len(summary) > 500:
            summary = summary[:497] + "..."
        content_html = _extract_entry_content(entry)
        image_url = _extract_entry_image(entry)
        tags: list[str] = []
        if hasattr(entry, "tags"):
            for tag in getattr(entry, "tags", []):
                if isinstance(tag, dict):
                    term = _to_str(tag.get("term", ""))
                    if term:
                        tags.append(term)

        pub_iso = pub_date.isoformat() if pub_date else now_iso

        # ── Enrich missing fields by scraping the article page ──────────────
        if link and (not image_url or not content_html):
            try:
                page_meta = _scrape_page_metadata(link)
                if not image_url and page_meta.get("image_url"):
                    image_url = page_meta["image_url"]
                    enriched_count += 1
                if not content_html and page_meta.get("body_html"):
                    content_html = page_meta["body_html"]
                    enriched_count += 1
                if author == "Unknown" and page_meta.get("author"):
                    author = page_meta["author"]
                if not summary and page_meta.get("description"):
                    summary = _html_to_plain_text(page_meta["description"])
            except Exception:
                pass  # enrichment is best-effort

        content_html = _sanitize_content_html(_to_str(content_html))

        # ── Build & validate Article ────────────────────────────────────────
        media_assets = _extract_media_assets(
            entry=entry,
            canonical_url=link,
            content_html=content_html,
        )
        has_audio = any(asset.kind == "audio" for asset in media_assets)
        has_video = any(asset.kind in {"video", "youtube"} for asset in media_assets)
        content_kind: Literal["article", "podcast", "video"]
        if has_audio:
            content_kind = "podcast"
        elif has_video:
            content_kind = "video"
        else:
            content_kind = "article"

        try:
            article = Article(
                title=title,
                canonical_url=link,
                feed_name=feed_name,
                feed_url=effective_feed_url,
                category=category,
                author=author,
                published_date=pub_iso,
                summary=summary,
                content_html=content_html,
                image_url=image_url,
                tags=tags,
                language=feed_lang,
                fetched_at=now_iso,
                source_opml=source_opml,
                guid=guid,
                content_kind=content_kind,
                media_assets=media_assets,
            )
        except Exception as exc:
            errors.append(f"Validation error for '{title}': {exc}")
            continue

        # ── Save ────────────────────────────────────────────────────────────
        fpath = article_filepath(feed_name, pub_iso, title)
        try:
            fpath.parent.mkdir(parents=True, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(article.model_dump(), f, indent=2, ensure_ascii=False)
            saved_articles.append({"title": title, "path": str(fpath)})
            existing_guids.add(guid)  # prevent intra-batch dupes
        except Exception as exc:
            errors.append(f"Save error for '{title}': {exc}")

    errors_page, errors_pagination = _paginate_items(errors, page, page_size)
    saved_articles_page, saved_articles_pagination = _paginate_items(
        saved_articles, page, page_size
    )

    return _text_result(
        {
            "feed_name": feed_name,
            "feed_url": effective_feed_url,
            "feed_url_original": feed_url,
            "feed_url_updated_in_opml": opml_updated,
            "category": category,
            "category_assigned": category_assigned,
            "category_updated_in_opml": category_updated_in_opml,
            "status": "ok",
            "total_entries": len(feed.entries),
            "recent_entries": len(recent_entries),
            "chunk_entries_considered": len(entries_to_process),
            "chunk_pagination": chunk_pagination,
            "new_saved": len(saved_articles),
            "duplicates_skipped": skipped_dupes,
            "old_skipped": skipped_old,
            "enriched_fields": enriched_count,
            "error_count": len(errors),
            "errors": errors_page,
            "errors_pagination": errors_pagination,
            "saved_articles": saved_articles_page,
            "saved_articles_pagination": saved_articles_pagination,
        }
    )


@tool(
    "extract_page_metadata",
    (
        "Fetch a web page by URL and extract Open Graph metadata, canonical "
        "URL, hero image, title, description, and article body HTML. Use this "
        "to manually enrich an article that is still missing fields after the "
        "initial feed processing."
    ),
    {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL of the web page to fetch and scrape",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for body HTML pagination",
            },
            "page_size": {
                "type": "integer",
                "description": "Characters per page for body HTML pagination",
            }
        },
        "required": ["url", "page", "page_size"],
    },
)
async def extract_page_metadata(args: dict[str, Any]) -> dict[str, Any]:
    """Fetch a web page and extract OG metadata, canonical URL, hero image,
    and article body content.

    Returns JSON with keys:
        url, canonical_url, title, image_url, description, author,
        site_name, body_html_preview, error (if any)
    """
    url = args["url"]
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc)})
    try:
        resp = requests.get(
            url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True
        )
        resp.raise_for_status()
    except Exception as exc:
        return _text_result({"error": f"Failed to fetch {url}: {exc}"})

    soup = BeautifulSoup(resp.text, "html.parser")

    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        prop = _to_str(meta.get("property", "") or meta.get("name", ""))
        content = _to_str(meta.get("content", ""))
        if prop.startswith("og:") and content:
            og[prop[3:]] = content
        elif prop == "twitter:image" and content:
            og.setdefault("image", content)
        elif prop == "description" and content:
            og.setdefault("description", content)
        elif prop in ("author", "article:author") and content:
            og.setdefault("author", content)

    canonical = ""
    link_tag = soup.find("link", rel="canonical")
    if link_tag and link_tag.get("href"):
        canonical = _to_str(link_tag.get("href", ""))

    title = og.get("title", "")
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

    body_html = ""
    for sel in (
        "article",
        '[role="main"]',
        ".post-content",
        ".entry-content",
        ".article-body",
        "main",
    ):
        el = soup.select_one(sel)
        if el:
            body_html = str(el)[:3000]
            break

    body_html_page, body_html_pagination = _paginate_text(body_html, page, page_size)

    return _text_result(
        {
            "url": url,
            "final_url": resp.url,
            "canonical_url": canonical or resp.url,
            "title": title,
            "image_url": og.get("image", ""),
            "description": og.get("description", ""),
            "author": og.get("author", ""),
            "site_name": og.get("site_name", ""),
            "body_html_page": body_html_page,
            "body_html_pagination": body_html_pagination,
        }
    )


@tool(
    "save_article",
    (
        "Validate article data against the Article schema and save it as a "
        "JSON file at the canonical path under data/articles/<feed-slug>/. "
        "Use this to save or overwrite an article after manual enrichment."
    ),
    {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Article headline / title",
            },
            "canonical_url": {
                "type": "string",
                "description": "Canonical permalink URL of the article",
            },
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the RSS/Atom feed",
            },
            "feed_url": {
                "type": "string",
                "description": "URL of the RSS/Atom feed",
            },
            "category": {
                "type": "string",
                "description": "Feed category from the OPML",
            },
            "author": {
                "type": "string",
                "description": "Author or byline of the article",
            },
            "published_date": {
                "type": "string",
                "description": "ISO 8601 publication date-time",
            },
            "summary": {
                "type": "string",
                "description": "Short plain-text summary or excerpt",
            },
            "content_html": {
                "type": "string",
                "description": "Full or partial article body as HTML",
            },
            "image_url": {
                "type": "string",
                "description": "Hero/title image URL for card display",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keywords or tags for the article",
            },
            "language": {
                "type": "string",
                "description": "ISO 639-1 language code (e.g. 'en')",
            },
            "fetched_at": {
                "type": "string",
                "description": "ISO 8601 timestamp when article data was fetched",
            },
            "source_opml": {
                "type": "string",
                "description": "OPML filename this feed came from",
            },
            "guid": {
                "type": "string",
                "description": "Globally unique identifier for the article",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for paginating save result payload",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of result records to return per page",
            },
        },
        "required": [
            "title",
            "canonical_url",
            "feed_name",
            "feed_url",
            "category",
            "author",
            "published_date",
            "summary",
            "content_html",
            "image_url",
            "tags",
            "language",
            "fetched_at",
            "source_opml",
            "guid",
            "page",
            "page_size",
        ],
    },
)
async def save_article(args: dict[str, Any]) -> dict[str, Any]:
    """Validate the supplied article data against the Article Pydantic model
    and persist it to the correct JSON file path.

    Returns JSON with keys: saved (bool), path, feed_name, title, error (if any)
    """
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc), "saved": False})

    sanitized_args = dict(args)
    sanitized_args.pop("page", None)
    sanitized_args.pop("page_size", None)
    sanitized_args["summary"] = _html_to_plain_text(_to_str(args.get("summary", "")))
    sanitized_args["content_html"] = _sanitize_content_html(
        _to_str(args.get("content_html", ""))
    )
    media_assets_in = args.get("media_assets", [])
    sanitized_media_assets: list[dict[str, Any]] = []
    if isinstance(media_assets_in, list):
        for item in media_assets_in:
            if not isinstance(item, dict):
                continue
            media_url = _to_str(item.get("url", "")).strip()
            if not _is_https_url(media_url):
                continue
            media_mime = _to_str(item.get("mime_type", ""))
            media_kind = _to_str(item.get("kind", "")).strip().lower()
            inferred_kind = _infer_media_kind(media_url, media_mime)
            if media_kind not in {"audio", "video", "youtube"}:
                media_kind = inferred_kind or "video"
            sanitized_media_assets.append(
                {
                    "kind": media_kind,
                    "url": media_url,
                    "mime_type": media_mime,
                    "title": _to_str(item.get("title", "")),
                    "duration_seconds": _parse_duration_seconds(
                        item.get("duration_seconds", None)
                    ),
                }
            )
    sanitized_args["media_assets"] = sanitized_media_assets

    explicit_kind = _to_str(args.get("content_kind", "")).strip().lower()
    if explicit_kind in {"article", "podcast", "video"}:
        sanitized_args["content_kind"] = explicit_kind
    else:
        if any(asset.get("kind") == "audio" for asset in sanitized_media_assets):
            sanitized_args["content_kind"] = "podcast"
        elif any(
            asset.get("kind") in {"video", "youtube"}
            for asset in sanitized_media_assets
        ):
            sanitized_args["content_kind"] = "video"
        else:
            sanitized_args["content_kind"] = "article"

    try:
        article = Article(**sanitized_args)
    except Exception as exc:
        return _text_result(
            {"error": f"Validation failed: {exc}", "saved": False}
        )

    fpath = article_filepath(
        article.feed_name, article.published_date, article.title
    )
    fpath.parent.mkdir(parents=True, exist_ok=True)

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(article.model_dump(), f, indent=2, ensure_ascii=False)

    saved_payload = {
        "saved": True,
        "path": str(fpath),
        "feed_name": article.feed_name,
        "title": article.title,
    }
    result_page, pagination = _paginate_items([saved_payload], page, page_size)

    return _text_result(
        {
            **saved_payload,
            "result_page": result_page,
            "pagination": pagination,
        }
    )


@tool(
    "list_feed_articles",
    (
        "List all existing article JSON files for a given feed name. "
        "Returns filenames, titles, GUIDs, and published dates. Use this "
        "to inspect what has already been saved for a feed."
    ),
    {
        "type": "object",
        "properties": {
            "feed_name": {
                "type": "string",
                "description": "Human-readable name of the feed to list articles for",
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for paginating article file list",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of articles to return per page",
            }
        },
        "required": ["feed_name", "page", "page_size"],
    },
)
async def list_feed_articles(args: dict[str, Any]) -> dict[str, Any]:
    """List saved article JSON files for a given feed.

    Returns JSON with keys:
        feed_name, feed_dir, exists, article_count, articles[]
    """
    feed_name = args["feed_name"]
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc)})
    feed_dir = ARTICLES_DIR / slugify(feed_name)

    if not feed_dir.exists():
        return _text_result(
            {
                "feed_name": feed_name,
                "exists": False,
                "article_count": 0,
                "articles": [],
            }
        )

    articles: list[dict[str, Any]] = []
    for json_file in sorted(feed_dir.glob("*.json")):
        try:
            with open(json_file) as f:
                data = json.load(f)
            articles.append(
                {
                    "filename": json_file.name,
                    "title": data.get("title", ""),
                    "guid": data.get("guid", ""),
                    "published_date": data.get("published_date", ""),
                    "has_image": bool(data.get("image_url")),
                    "has_content": bool(data.get("content_html")),
                }
            )
        except Exception:
            articles.append({"filename": json_file.name, "error": "unreadable"})

    articles_page, pagination = _paginate_items(articles, page, page_size)

    return _text_result(
        {
            "feed_name": feed_name,
            "exists": True,
            "article_count": len(articles),
            "articles": articles_page,
            "pagination": pagination,
        }
    )


@tool(
    "get_run_statistics",
    (
        "Get aggregate statistics about the current article archive: total "
        "feeds with saved articles, total article count, breakdown by feed, "
        "and articles missing key fields (image, content). Use at the end "
        "of a run for a summary report."
    ),
    {
        "type": "object",
        "properties": {
            "include_per_feed_detail": {
                "type": "boolean",
                "description": (
                    "If true, include per-feed article counts. "
                    "If false, only return aggregate totals."
                ),
            },
            "page": {
                "type": "integer",
                "description": "1-based page number for paginating per-feed detail",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of per-feed records to return per page",
            }
        },
        "required": ["include_per_feed_detail", "page", "page_size"],
    },
)
async def get_run_statistics(args: dict[str, Any]) -> dict[str, Any]:
    """Compute and return statistics about the article archive.

    Returns JSON with keys:
        total_feeds, total_articles, missing_image_count,
        missing_content_count, per_feed (optional)
    """
    include_detail = args["include_per_feed_detail"]
    try:
        page = _require_positive_int(args["page"], "page")
        page_size = _require_positive_int(args["page_size"], "page_size")
    except (KeyError, ValueError) as exc:
        return _text_result({"error": str(exc)})

    if not ARTICLES_DIR.exists():
        return _text_result(
            {"total_feeds": 0, "total_articles": 0, "per_feed": []}
        )

    per_feed: list[dict[str, Any]] = []
    total_articles = 0
    missing_image = 0
    missing_content = 0

    for feed_dir in sorted(ARTICLES_DIR.iterdir()):
        if not feed_dir.is_dir():
            continue
        count = 0
        for json_file in feed_dir.glob("*.json"):
            count += 1
            try:
                with open(json_file) as f:
                    data = json.load(f)
                if not data.get("image_url"):
                    missing_image += 1
                if not data.get("content_html"):
                    missing_content += 1
            except Exception:
                pass
        total_articles += count
        if include_detail:
            per_feed.append({"feed_slug": feed_dir.name, "article_count": count})

    result: dict[str, Any] = {
        "total_feeds": len(
            [d for d in ARTICLES_DIR.iterdir() if d.is_dir()]
        ),
        "total_articles": total_articles,
        "missing_image_count": missing_image,
        "missing_content_count": missing_content,
    }
    if include_detail:
        per_feed_page, pagination = _paginate_items(per_feed, page, page_size)
        result["per_feed"] = per_feed_page
        result["pagination"] = pagination
    return _text_result(result)


# ── Tool response helper ────────────────────────────────────────────────────


def _text_result(data: Any) -> dict[str, Any]:
    """Wrap a JSON-serialisable value in the MCP tool response format."""
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2)}]
    }


def _require_positive_int(raw_value: Any, field_name: str) -> int:
    """Convert a value to a positive integer, otherwise raise ValueError."""

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _paginate_items(
    items: list[Any],
    page: int,
    page_size: int,
) -> tuple[list[Any], dict[str, Any]]:
    """Paginate a list and return (page_items, pagination_metadata)."""

    safe_page = _require_positive_int(page, "page")
    safe_page_size = _require_positive_int(page_size, "page_size")
    total_items = len(items)
    total_pages = max(1, (total_items + safe_page_size - 1) // safe_page_size)
    current_page = min(safe_page, total_pages)

    start = (current_page - 1) * safe_page_size
    end = start + safe_page_size
    page_items = items[start:end]

    pagination = {
        "page": current_page,
        "page_size": safe_page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next_page": current_page < total_pages,
        "has_prev_page": current_page > 1,
        "next_page": current_page + 1 if current_page < total_pages else None,
        "prev_page": current_page - 1 if current_page > 1 else None,
    }
    return page_items, pagination


def _paginate_text(
    text: str,
    page: int,
    page_size: int,
) -> tuple[str, dict[str, Any]]:
    """Paginate text by characters and return (text_page, pagination_metadata)."""

    safe_text = text or ""
    safe_page = _require_positive_int(page, "page")
    chars_per_page = _require_positive_int(page_size, "page_size")

    total_chars = len(safe_text)
    total_pages = max(1, (total_chars + chars_per_page - 1) // chars_per_page)
    current_page = min(safe_page, total_pages)
    start = (current_page - 1) * chars_per_page
    end = min(start + chars_per_page, total_chars)

    page_text = safe_text[start:end]
    pagination = {
        "page": current_page,
        "page_size": chars_per_page,
        "total_chars": total_chars,
        "total_pages": total_pages,
        "has_next_page": current_page < total_pages,
        "has_prev_page": current_page > 1,
        "next_page": current_page + 1 if current_page < total_pages else None,
        "prev_page": current_page - 1 if current_page > 1 else None,
        "char_start": start,
        "char_end": end,
    }
    return page_text, pagination


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP Server Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

rss_server = create_sdk_mcp_server(
    name="rss_tools",
    version="1.0.0",
    tools=[
        parse_opml_files,
        process_single_feed,
        extract_page_metadata,
        save_article,
        list_feed_articles,
        get_run_statistics,
    ],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agent System Prompts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PER_FEED_SYSTEM_PROMPT = """\
You are an RSS/Atom feed article fetcher.  Your job is to fully process ONE
feed: fetch it, filter to the last 3 days, deduplicate against already-saved
articles, auto-enrich missing metadata by scraping, and save new articles as
JSON.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Call `mcp__rss_tools__process_single_feed` with the feed details provided
   in the user message, using chunk_page=1, chunk_size=10, page=1, page_size=20.
2. If the result has chunk_pagination.has_next_page=true, call again with
   chunk_page=2, 3, … until all chunks are processed.
3. When all chunks are done, output a brief summary of what was saved/skipped.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Keep each call small by using chunk_size around 5-15.
• ALL tools are paginated.  Always pass page and page_size.
• For any paginated result, continue page-by-page until has_next_page is false.
• Response limit: """ + str(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", 32768)) + """ tokens.
"""

# Legacy multi-feed prompt kept for reference but no longer used by main().
_LEGACY_SYSTEM_PROMPT = """\
You are a meticulous RSS/Atom feed article fetcher agent.  Your mission is to
build a complete, deduplicated archive of recent articles as JSON files so they
can be rendered as "cards" on a newspaper-style web page.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW — Follow these steps in strict, numbered order:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### Step 1: DISCOVERY
Call `mcp__rss_tools__parse_opml_files` with glob_pattern="feed*.opml",
page=1, page_size=50.  Then continue paging with page=2,3,… while
pagination.has_next_page is true.

### Step 2: PROCESS EACH FEED
For **every** subscription, call `mcp__rss_tools__process_single_feed`
with the feed's details, page=1, page_size=20, chunk_page=1, chunk_size=10.
Continue chunking while chunk_pagination.has_next_page is true.

### Step 3: SUMMARY
Call `mcp__rss_tools__get_run_statistics` with include_per_feed_detail=true,
page=1, page_size=100 and present a clear summary.

RULES
• Do NOT skip any feed.
• ALL tools are paginated.
"""


def _build_agent_prompts(
    system_prompt: str,
    user_prompt: str,
    force_user_only: bool = False,
) -> tuple[str | None, str]:
    """Return system/user prompts with optional model-template compatibility mode."""

    if not force_user_only:
        return system_prompt, user_prompt

    merged_prompt = (
        "SYSTEM INSTRUCTIONS:\n"
        f"{system_prompt.strip()}\n\n"
        "USER REQUEST:\n"
        f"{user_prompt.strip()}"
    )
    return None, merged_prompt


def _is_role_template_error(exc: Exception) -> bool:
    """Detect backend prompt-template role errors that can be fixed by user-only fallback."""

    text = str(exc)
    return (
        "Error rendering prompt with jinja template" in text
        and (
            "Only user and assistant roles are supported" in text
            or "conversation roles must alternate user/assistant" in text
        )
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _run_feed_agent(
    sub: FeedSubscription,
    feed_index: int,
    total_feeds: int,
    force_user_only: bool,
    allow_auto_retry: bool,
    agent_max_turns: int | None,
) -> dict[str, Any]:
    """Spawn a fresh agent to process a single feed and return a result dict.

    Each invocation creates its own :class:`ClaudeSDKClient` context so the
    LLM conversation stays short (just one feed) — avoiding the progressive
    slowdown caused by growing context in a single long-running agent.

    Returns a dict with ``feed_name``, ``status`` ("ok" / "error"),
    ``new_saved``, ``duplicates_skipped``, ``turns``, ``duration_s``, and
    ``error`` (if any).
    """

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = (
        f"Today is {today}.  Process this feed now:\n"
        f"  feed_name:   {sub.name}\n"
        f"  feed_url:    {sub.xml_url}\n"
        f"  category:    {sub.category}\n"
        f"  source_opml: {sub.source_file}\n\n"
        f"Call process_single_feed with these details, chunk_page=1, "
        f"chunk_size=10, page=1, page_size=20.  Continue chunking until done."
    )

    result: dict[str, Any] = {
        "feed_name": sub.name,
        "status": "error",
        "new_saved": 0,
        "duplicates_skipped": 0,
        "turns": 0,
        "duration_s": 0.0,
        "error": None,
    }

    for attempt in range(2):
        system_prompt, prompt = _build_agent_prompts(
            PER_FEED_SYSTEM_PROMPT,
            user_prompt,
            force_user_only=force_user_only,
        )

        options_kwargs: dict[str, Any] = {
            "mcp_servers": {"rss_tools": rss_server},
            "model": os.environ.get("MODEL"),
            "allowed_tools": [
                "mcp__rss_tools__process_single_feed",
                "mcp__rss_tools__extract_page_metadata",
                "mcp__rss_tools__save_article",
                "mcp__rss_tools__list_feed_articles",
            ],
            "permission_mode": "bypassPermissions",
            "cwd": str(PROJECT_ROOT),
        }
        if agent_max_turns is not None:
            options_kwargs["max_turns"] = agent_max_turns
        if system_prompt is not None:
            options_kwargs["system_prompt"] = system_prompt

        options = ClaudeAgentOptions(**options_kwargs)

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_messages():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                print(f"    [Agent] {block.text}")
                            elif isinstance(block, ToolUseBlock):
                                tool_name_short = block.name.split("__")[-1]
                                input_preview = json.dumps(block.input)
                                if len(input_preview) > 120:
                                    input_preview = input_preview[:117] + "..."
                                print(f"      → {tool_name_short}({input_preview})")
                            elif isinstance(block, ToolResultBlock):
                                if block.content and isinstance(block.content, str):
                                    try:
                                        data = json.loads(block.content)
                                        if "new_saved" in data:
                                            result["new_saved"] += data["new_saved"]
                                            result["duplicates_skipped"] += data.get(
                                                "duplicates_skipped", 0
                                            )
                                            print(
                                                f"      ✓ saved={data['new_saved']}  "
                                                f"dupes={data.get('duplicates_skipped', 0)}  "
                                                f"status={data.get('status', '?')}"
                                            )
                                    except (json.JSONDecodeError, TypeError):
                                        pass

                    elif isinstance(message, ResultMessage):
                        result["status"] = "ok"
                        result["turns"] = message.num_turns
                        result["duration_s"] = message.duration_ms / 1000
            break  # success — no retry needed
        except Exception as exc:
            should_retry = (
                attempt == 0
                and not force_user_only
                and allow_auto_retry
                and _is_role_template_error(exc)
            )
            if should_retry:
                print(
                    "    [Advisory] Prompt-template role mismatch; "
                    "retrying with user-only prompt."
                )
                force_user_only = True
                continue
            result["error"] = str(exc)
            print(f"    [ERROR] {exc}")
            break

    return result


async def main() -> None:
    """Run the agentic RSS feed fetcher workflow.

    Architecture (per-feed agent spawning):
      1. Parse OPML files in pure Python (no LLM)
      2. For each feed, spawn a *fresh* agent with a short conversation
      3. Compute run statistics in pure Python (no LLM)

    This keeps each agent's context small and avoids the progressive slowdown
    caused by attention over a single long-running conversation.
    """

    vllm_process, llm_endpoint = configure_llm_runtime()
    context_capacity_advisory = _validate_agent_context_capacity()
    agent_max_turns = _optional_env_positive_int("AGENT_MAX_TURNS")
    try:
        print("=" * 70)
        print("  RSS Newspaper — Agentic Fetcher  (per-feed agent mode)")
        print("=" * 70)
        print(f"  Project root: {PROJECT_ROOT}")
        print(f"  Data dir:     {DATA_DIR}")
        print(f"  Articles dir: {ARTICLES_DIR}")
        print(f"  Cutoff:       last {CUTOFF_DAYS} days")
        print(f"  LLM endpoint: {llm_endpoint}")
        print(f"  Model:        {os.environ.get('MODEL', '(default)')}")
        if agent_max_turns is None:
            print("  Max turns:    unlimited (SDK/CLI default)")
        else:
            print(f"  Max turns:    {agent_max_turns}")
        if vllm_process is not None:
            print(f"  Runtime:      vLLM (pid={vllm_process.pid})")
            print(f"  vLLM model:   {os.environ.get('VLLM_MODEL', '(unknown)')}")
            original_model = os.environ.get("VLLM_ORIGINAL_MODEL_ENV")
            if original_model:
                print(f"  prior MODEL:  {original_model}")
            requested_port = os.environ.get("VLLM_REQUESTED_PORT")
            effective_port = os.environ.get("VLLM_EFFECTIVE_PORT")
            if requested_port and effective_port and requested_port != effective_port:
                print(f"  vLLM port:    requested {requested_port} → using {effective_port}")
            requested_master_port = os.environ.get("VLLM_REQUESTED_MASTER_PORT")
            effective_master_port = os.environ.get("VLLM_EFFECTIVE_MASTER_PORT")
            if requested_master_port and effective_master_port:
                if requested_master_port != effective_master_port:
                    print(
                        "  vLLM master:  "
                        f"requested {requested_master_port} → using {effective_master_port}"
                    )
                else:
                    print(f"  vLLM master:  {effective_master_port}")
            requested_max_len = os.environ.get("VLLM_REQUESTED_MAX_MODEL_LEN")
            effective_max_len = os.environ.get("VLLM_EFFECTIVE_MAX_MODEL_LEN")
            if requested_max_len and effective_max_len:
                if requested_max_len != effective_max_len:
                    print(
                        "  vLLM maxlen:  "
                        f"requested {requested_max_len} → using {effective_max_len}"
                    )
                else:
                    print(f"  vLLM maxlen:  {effective_max_len}")
            print(f"  vLLM logs:    {os.environ.get('VLLM_LOG_PATH', '(unknown)')}")
            if context_capacity_advisory:
                print()
                print("  [Advisory]")
                for line in context_capacity_advisory.splitlines():
                    print(f"  {line}")
        print("=" * 70)
        print()

        # Ensure output directory exists
        ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

        # ── Step 1: Discover feeds (pure Python — no LLM) ──────────────────
        print("─" * 70)
        print("  Step 1/3: OPML Discovery  (pure Python)")
        print("─" * 70)
        subscriptions = discover_subscriptions()
        if not subscriptions:
            print("  No subscriptions found.  Nothing to do.")
            return
        for i, sub in enumerate(subscriptions, 1):
            print(f"    {i:>3}. [{sub.category}] {sub.name}")
        print()

        # ── Step 2: Process each feed (one agent per feed) ─────────────────
        print("─" * 70)
        print("  Step 2/3: Per-Feed Agent Processing")
        print("─" * 70)
        print()
        force_user_only = _env_flag("LMSTUDIO_USER_ONLY_PROMPT")
        allow_auto_retry = _env_flag("LMSTUDIO_AUTO_USER_ONLY_RETRY")
        if force_user_only:
            print("  Prompt mode: model-template compatibility (user-only merge)")
            print()

        total_feeds = len(subscriptions)
        feed_results: list[dict[str, Any]] = []
        total_new = 0
        total_dupes = 0
        total_errors = 0
        run_start = time.monotonic()

        for i, sub in enumerate(subscriptions, 1):
            print(f"  ┌─ Feed {i}/{total_feeds}: {sub.name}")
            print(f"  │  URL: {sub.xml_url}")
            print(f"  │  Category: {sub.category}  |  OPML: {sub.source_file}")
            feed_start = time.monotonic()

            result = await _run_feed_agent(
                sub=sub,
                feed_index=i,
                total_feeds=total_feeds,
                force_user_only=force_user_only,
                allow_auto_retry=allow_auto_retry,
                agent_max_turns=agent_max_turns,
            )
            feed_results.append(result)

            elapsed = time.monotonic() - feed_start
            status_icon = "✓" if result["status"] == "ok" else "✗"
            print(
                f"  └─ {status_icon} {result['new_saved']} saved, "
                f"{result['duplicates_skipped']} dupes, "
                f"{result['turns']} turns, {elapsed:.1f}s"
            )
            if result["error"]:
                print(f"     Error: {result['error']}")
            print()

            total_new += result["new_saved"]
            total_dupes += result["duplicates_skipped"]
            if result["status"] != "ok":
                total_errors += 1

        run_elapsed = time.monotonic() - run_start

        # ── Step 3: Statistics (pure Python — no LLM) ──────────────────────
        print("─" * 70)
        print("  Step 3/3: Run Statistics  (pure Python)")
        print("─" * 70)
        stats = compute_run_statistics()
        print()

        # ── Final summary ──────────────────────────────────────────────────
        print("=" * 70)
        print("  RSS Newspaper — Run Summary")
        print("=" * 70)
        print(f"  Feeds attempted:       {total_feeds}")
        print(f"  Feeds succeeded:       {total_feeds - total_errors}")
        print(f"  Feeds failed:          {total_errors}")
        print(f"  New articles saved:    {total_new}")
        print(f"  Duplicates skipped:    {total_dupes}")
        print(f"  Total processing time: {run_elapsed:.1f}s")
        print()
        print(f"  Archive totals:")
        print(f"    Total feeds on disk:   {stats['total_feeds']}")
        print(f"    Total articles:        {stats['total_articles']}")
        print(f"    Missing hero image:    {stats['missing_image_count']}")
        print(f"    Missing body content:  {stats['missing_content_count']}")

        if total_errors:
            print()
            print("  Failed feeds:")
            for r in feed_results:
                if r["status"] != "ok":
                    print(f"    • {r['feed_name']}: {r['error']}")

        if stats["per_feed"]:
            print()
            print("  Per-feed article counts:")
            for pf in stats["per_feed"]:
                print(f"    {pf['feed_slug']:.<45} {pf['article_count']}")

        print()
        print(f"  Article files at: {ARTICLES_DIR}")
        print("=" * 70)
        print("  Done.")
    finally:
        if vllm_process is not None and vllm_process.poll() is None:
            _stop_process_gracefully(vllm_process)


if __name__ == "__main__":
    asyncio.run(main())
