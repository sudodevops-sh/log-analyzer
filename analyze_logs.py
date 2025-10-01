import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

LOGGER = logging.getLogger("analyze_logs")
_handler = logging.StreamHandler(sys.stdout)
_formatter = logging.Formatter(
    fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
_handler.setFormatter(_formatter)
LOGGER.addHandler(_handler)
LOGGER.setLevel(logging.INFO)

# Patterns to focus on likely failure signals in EKS/app logs
ERROR_PATTERNS = re.compile(
    r"(?i)\b(error|exception|traceback|failed|failure|killed|oomkilled|crashloopbackoff|backoff|imagepull|imagepullbackoff|deadlineexceeded|node ?not ?ready|evicted|terminated|pod .*failed|panic|segfault|stacktrace|kubectl|kubelet|probe failed|readiness probe|liveness probe)\b"
)

# Provide a little surrounding context around the error lines
CONTEXT_RADIUS = 3


class OllamaTimeout(Exception):
    pass


@dataclass
class LogEntry:
    timestamp: str
    message: str
    log_stream: Optional[str] = None
    log_group: Optional[str] = None

    def short(self) -> str:
        ts = self.timestamp
        src = self.log_stream or "-"
        return f"{ts} [{src}] {self.message}".strip()


def _load_insights_rows(path: str) -> List[List[Dict[str, str]]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Unexpected JSON structure in {path}; expected a list of rows")
    return data


def _normalize_insights_rows(rows: List[List[Dict[str, str]]]) -> List[LogEntry]:
    entries: List[LogEntry] = []
    for row in rows:
        mapping: Dict[str, str] = {cell.get("field"): cell.get("value") for cell in row if isinstance(cell, dict)}
        ts = mapping.get("@timestamp") or mapping.get("timestamp") or ""
        msg = mapping.get("@message") or mapping.get("message") or ""
        log_stream = mapping.get("@logStream") or mapping.get("logStream")
        log_group = mapping.get("@log") or mapping.get("log")
        if not msg:
            continue
        # Keep timestamp as-is; try to sort later
        entries.append(LogEntry(timestamp=ts, message=msg, log_stream=log_stream, log_group=log_group))
    # Sort by timestamp if parsable
    def _ts_key(e: LogEntry) -> Tuple[int, str]:
        try:
            # CloudWatch ts looks like 2025-09-29 10:30:21.123
            dt = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
            return (int(dt.timestamp()), e.log_stream or "")
        except Exception:
            return (0, e.log_stream or "")
    entries.sort(key=_ts_key)
    return entries


def _select_focus_lines(lines: List[str]) -> List[str]:
    """Return a subset of lines focused on errors with some context radius."""
    idx_matches = [i for i, line in enumerate(lines) if ERROR_PATTERNS.search(line)]
    if not idx_matches:
        # No matches; fall back to first N lines to give the model something
        return lines[:2000]
    selected: Dict[int, None] = {}
    for idx in idx_matches:
        start = max(0, idx - CONTEXT_RADIUS)
        end = min(len(lines), idx + CONTEXT_RADIUS + 1)
        for j in range(start, end):
            selected[j] = None
    result = [lines[i] for i in sorted(selected.keys())]
    return result


def _chunk_text(lines: List[str], max_chars: int) -> List[str]:
    chunks: List[str] = []
    buf: List[str] = []
    count = 0
    for line in lines:
        # Ensure each line ends with newline for readability
        l = line if line.endswith("\n") else line + "\n"
        if count + len(l) > max_chars and buf:
            chunks.append("".join(buf))
            buf = []
            count = 0
        buf.append(l)
        count += len(l)
    if buf:
        chunks.append("".join(buf))
    return chunks


def _ollama_generate(
    model: str,
    prompt: str,
    host: str = "http://localhost",
    port: int = 11434,
    expect_json: bool = False,
    timeout: int = 300,
    options: Optional[Dict[str, Any]] = None,
    retries: int = 1,
    backoff_seconds: float = 1.5,
) -> Any:
    """Call Ollama /api/generate with optional retries and options."""
    url = f"{host}:{port}/api/generate"
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if expect_json:
        payload["format"] = "json"
    if options:
        payload["options"] = options

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"Failed to connect to Ollama at {url}. Ensure the server is running and the model '{model}' is available.") from e
        except requests.exceptions.Timeout as e:
            if attempt <= retries:
                LOGGER.warning("Ollama request timed out, retrying (attempt %d/%d)", attempt, retries)
                try:
                    import time as _t
                    _t.sleep(backoff_seconds * attempt)
                except Exception:
                    pass
                continue
            raise OllamaTimeout("Timed out waiting for Ollama response") from e

        if resp.status_code != 200:
            # Do not retry non-timeout errors by default
            raise RuntimeError(f"Ollama returned status {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content = data.get("response", "")
        if expect_json:
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # Fallback: try to extract JSON
                m = re.search(r"\{[\s\S]*\}$", content.strip())
                if m:
                    return json.loads(m.group(0))
                raise
        return content


def _chunk_summary_prompt(task_id: str, chunk_text: str) -> str:
    return (
        "You are a senior SRE analyzing AWS CloudWatch logs from EKS workloads.\n"
        f"Task ID: {task_id}\n"
        "From the following log excerpt, extract key failure signals and possible root causes.\n"
        "Summarize concisely and return JSON with keys: key_events (list), suspected_causes (list), indicators (list), timeframe (object with start,end if any).\n"
        "Only include information supported by the logs.\n\n"
        "Log excerpt:\n"
        f"{chunk_text}\n\n"
        "Return JSON only."
    )


def _final_report_prompt(task_id: str, summaries: List[Dict[str, Any]]) -> str:
    return (
        "You are a senior SRE analyzing multiple partial summaries from CloudWatch logs for a single failed task on EKS.\n"
        f"Task ID: {task_id}\n"
        "Combine the partial summaries into a single root-cause report.\n"
        "Return strict JSON with these keys: \n"
        "task_id (string), root_cause (string), error_type (string), contributing_factors (list), key_events (list), timeline (list), suggested_actions (list), indicators (list), confidence (0-1 number), eks_related (boolean).\n"
        "Be precise and avoid speculation. If unsure, say so and lower confidence.\n\n"
        f"Partial summaries: {json.dumps(summaries)}\n\n"
        "Return JSON only."
    )


def _write_outputs(base_path: str, report_json: Dict[str, Any]) -> Tuple[str, str]:
    base, _ = os.path.splitext(base_path)
    json_path = f"{base}-report.json"
    md_path = f"{base}-report.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, indent=2)
    # Also render a readable markdown
    md = [
        f"# Failure Analysis for Task {report_json.get('task_id','')}\n",
        f"- Confidence: {report_json.get('confidence', 0):.2f}\n",
        f"- Error Type: {report_json.get('error_type','unknown')}\n",
        f"- EKS Related: {bool(report_json.get('eks_related'))}\n\n",
        "## Root Cause\n",
        report_json.get("root_cause", "Unknown") + "\n\n",
        "## Key Events\n",
        "\n".join(f"- {e}" for e in report_json.get("key_events", []) ) + "\n\n",
        "## Timeline\n",
        "\n".join(f"- {e}" for e in report_json.get("timeline", []) ) + "\n\n",
        "## Indicators\n",
        "\n".join(f"- {e}" for e in report_json.get("indicators", []) ) + "\n\n",
        "## Suggested Actions\n",
        "\n".join(f"- {a}" for a in report_json.get("suggested_actions", []) ) + "\n",
    ]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("".join(md))
    return json_path, md_path


def _merge_summaries(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _unique(seq: List[Any]) -> List[Any]:
        seen = set()
        out = []
        for x in seq:
            k = json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else str(x)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out

    merged: Dict[str, Any] = {
        "key_events": [],
        "suspected_causes": [],
        "indicators": [],
        "timeframe": {},
    }
    starts: List[str] = []
    ends: List[str] = []
    for p in parts:
        merged["key_events"].extend(p.get("key_events", []) or [])
        merged["suspected_causes"].extend(p.get("suspected_causes", []) or [])
        merged["indicators"].extend(p.get("indicators", []) or [])
        tf = p.get("timeframe") or {}
        if isinstance(tf, dict):
            s = tf.get("start")
            e = tf.get("end")
            if s:
                starts.append(s)
            if e:
                ends.append(e)
    merged["key_events"] = _unique(merged["key_events"])[:200]
    merged["suspected_causes"] = _unique(merged["suspected_causes"])[:100]
    merged["indicators"] = _unique(merged["indicators"])[:200]
    if starts or ends:
        merged["timeframe"] = {"start": min(starts) if starts else None, "end": max(ends) if ends else None}
    return merged


def _summarize_chunk_with_split(
    task_id: str,
    chunk_text: str,
    model: str,
    host: str,
    port: int,
    timeout: int,
    min_split_chars: int = 6000,
    depth: int = 0,
    max_depth: int = 2,
) -> Dict[str, Any]:
    """Summarize a chunk; on timeout, recursively split and merge summaries."""
    prompt = _chunk_summary_prompt(task_id, chunk_text)
    try:
        return _ollama_generate(
            model=model,
            prompt=prompt,
            host=host,
            port=port,
            expect_json=True,
            timeout=timeout,
            options={"num_predict": 500, "temperature": 0.2, "top_p": 0.9},
            retries=1,
        )
    except OllamaTimeout:
        if len(chunk_text) > min_split_chars and depth < max_depth:
            mid = len(chunk_text) // 2
            # Split on nearest newline to keep lines intact
            nl = chunk_text.rfind("\n", 0, mid)
            if nl <= 0:
                nl = mid
            left = chunk_text[:nl]
            right = chunk_text[nl:]
            left_summary = _summarize_chunk_with_split(task_id, left, model, host, port, timeout, min_split_chars, depth + 1, max_depth)
            right_summary = _summarize_chunk_with_split(task_id, right, model, host, port, timeout, min_split_chars, depth + 1, max_depth)
            return _merge_summaries([left_summary, right_summary])
        # Fallback empty summary
        return {"key_events": [], "suspected_causes": [], "indicators": [], "timeframe": {}}
    except Exception as e:
        LOGGER.warning("Chunk summarization failed: %s", e)
        return {"key_events": [], "suspected_causes": [], "indicators": [], "timeframe": {}}


def analyze_file(
    path: str,
    model: str = "llama3.2",
    ollama_host: str = "http://localhost",
    ollama_port: int = 11434,
    max_chunk_chars: int = 12000,
    ollama_timeout: int = 300,
) -> Tuple[str, str]:
    """Analyze a single CloudWatch Insights JSON result file. Returns (json_report_path, md_report_path)."""
    LOGGER.info("Analyzing %s using model %s", path, model)
    rows = _load_insights_rows(path)
    entries = _normalize_insights_rows(rows)

    if not entries:
        raise ValueError(f"No entries parsed from {path}")

    all_lines = [e.short() for e in entries]
    focus_lines = _select_focus_lines(all_lines)
    chunks = _chunk_text(focus_lines, max_chars=max_chunk_chars)

    # Attempt to infer a task id from filename
    fname = os.path.basename(path)
    task_id = fname.split("-")[0] if "-" in fname else fname

    partials: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        LOGGER.info("Summarizing chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        summary = _summarize_chunk_with_split(
            task_id=task_id,
            chunk_text=chunk,
            model=model,
            host=ollama_host,
            port=ollama_port,
            timeout=ollama_timeout,
        )
        partials.append(summary)

    final_prompt = _final_report_prompt(task_id, partials)
    final_report = _ollama_generate(
        model=model,
        prompt=final_prompt,
        host=ollama_host,
        port=ollama_port,
        expect_json=True,
        timeout=ollama_timeout,
        options={"num_predict": 700, "temperature": 0.2, "top_p": 0.9},
        retries=1,
    )

    json_path, md_path = _write_outputs(path, final_report)
    LOGGER.info("Analysis written to %s and %s", json_path, md_path)
    return json_path, md_path


def analyze_files(
    paths: Iterable[str],
    model: str = "llama3.2",
    ollama_host: str = "http://localhost",
    ollama_port: int = 11434,
    max_chunk_chars: int = 12000,
    ollama_timeout: int = 300,
) -> List[Tuple[str, str]]:
    results: List[Tuple[str, str]] = []
    for p in paths:
        try:
            results.append(
                analyze_file(
                    path=p,
                    model=model,
                    ollama_host=ollama_host,
                    ollama_port=ollama_port,
                    max_chunk_chars=max_chunk_chars,
                    ollama_timeout=ollama_timeout,
                )
            )
        except Exception as e:
            LOGGER.exception("Failed to analyze %s: %s", p, e)
    return results

