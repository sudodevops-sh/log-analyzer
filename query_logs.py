import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import boto3

# ----------------------------------------
# Logging setup
# ----------------------------------------
LOGGER = logging.getLogger("query_logs")
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
handler.setFormatter(formatter)
LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)


class LogsQueryError(Exception):
    pass


def _epoch_range(days: int, start: Optional[str] = None, end: Optional[str] = None) -> tuple[int, int, datetime, datetime]:
    """
    Compute epoch range.
    If start/end (ISO 8601) provided, use those; otherwise compute from days.
    Returns (start_epoch, end_epoch, start_dt, end_dt)
    """
    if start and end:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc)
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
    return int(start_dt.timestamp()), int(end_dt.timestamp()), start_dt, end_dt


def _start_logs_query(
    client,
    log_groups: List[str],
    query: str,
    start_epoch: int,
    end_epoch: int,
) -> str:
    """Start a CloudWatch Logs Insights query and return the queryId."""
    for attempt in range(5):
        try:
            resp = client.start_query(
                logGroupNames=log_groups,
                startTime=start_epoch,
                endTime=end_epoch,
                queryString=query,
            )
            return resp["queryId"]
        except Exception as e:
            wait = 2 ** attempt
            LOGGER.warning("start_query failed (attempt %d): %s", attempt + 1, e)
            time.sleep(wait)
    raise LogsQueryError("Failed to start logs query after retries")


def _poll_query_results(client, query_id: str, timeout_s: int = 600, interval_s: float = 2.0) -> dict:
    """Poll query results until completion or timeout. Returns get_query_results response."""
    start_time = time.monotonic()
    status = "Running"
    last_status = None
    while status in ("Running", "Scheduled"):
        if time.monotonic() - start_time > timeout_s:
            raise LogsQueryError("Timed out waiting for query to complete")
        time.sleep(interval_s)
        result = client.get_query_results(queryId=query_id)
        status = result.get("status")
        if status != last_status:
            LOGGER.debug("Query %s status: %s", query_id, status)
            last_status = status
    if status != "Complete":
        LOGGER.error("Query %s finished with status %s", query_id, status)
    return result


def _save_results(results: list, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    LOGGER.info("Logs saved to %s (%d rows)", output_path, len(results))


def build_task_query(task_id: str, error_only: bool = False) -> str:
    """
    Build a CloudWatch Logs Insights query for a given task id.
    If error_only is True, try to focus on error-like messages.
    """
    base = "fields @timestamp, @message, @logStream, @log | " \
           f"filter @message like /{task_id}/ | "
    if error_only:
        error_filter = (
            "filter @message like /(?i)(error|exception|failed|killed|oomkilled|crashloopbackoff|backoff|imagepull|deadlineexceeded|node not ready|evicted|terminated)/ | "
        )
    else:
        error_filter = ""
    sort = "sort @timestamp asc"
    return base + error_filter + sort


def fetch_cloudwatch_logs(
    task_id: str,
    aws_profile: Optional[str],
    region: str,
    log_groups: List[str],
    query: Optional[str] = None,
    days: int = 30,
    start: Optional[str] = None,
    end: Optional[str] = None,
    output_dir: str = ".",
    error_only: bool = False,
    timeout_s: int = 600,
) -> str:
    """
    Fetch CloudWatch Logs Insights results for the given log groups within a window.

    Returns path to the JSON file written.
    """
    if aws_profile:
        boto3.setup_default_session(profile_name=aws_profile)
    client = boto3.client("logs", region_name=region)

    start_epoch, end_epoch, start_dt, end_dt = _epoch_range(days=days, start=start, end=end)
    LOGGER.info("Querying logs from %s to %s", start_dt.isoformat(), end_dt.isoformat())

    if not query:
        query = build_task_query(task_id, error_only=error_only)

    query_id = _start_logs_query(client, log_groups, query, start_epoch, end_epoch)
    LOGGER.info("Started query: %s", query_id)

    result = _poll_query_results(client, query_id, timeout_s=timeout_s)
    status = result.get("status")
    LOGGER.info("Query finished with status: %s", status)

    ts_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = os.path.join(output_dir, f"{task_id}-{ts_suffix}-logs.json")
    _save_results(result.get("results", []), out_file)

    return out_file


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CloudWatch Logs Insights for a task and optionally analyze with Ollama")
    parser.add_argument("--task-id", dest="task_ids", action="append", help="Task ID to search for. Can be specified multiple times.")
    parser.add_argument("--task-file", help="Path to a file containing task IDs (one per line)")
    parser.add_argument("--aws-profile", default=os.getenv("AWS_PROFILE"), help="AWS profile name (defaults to AWS_PROFILE env)")
    parser.add_argument("--region", default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1", help="AWS region")
    parser.add_argument("--log-group", dest="log_groups", action="append", help="CloudWatch log group name. Can be specified multiple times.")
    parser.add_argument("--log-groups-file", help="Path to a file with log group names (one per line)")
    parser.add_argument("--days", type=int, default=7, help="Days lookback (ignored if --start/--end provided)")
    parser.add_argument("--start", help="Start time ISO 8601 (e.g., 2025-09-01T00:00:00Z)")
    parser.add_argument("--end", help="End time ISO 8601 (e.g., 2025-09-02T00:00:00Z)")
    parser.add_argument("--query", help="Custom Logs Insights query string. If set, overrides task-based query.")
    parser.add_argument("--output-dir", default=".", help="Directory to write output JSON logs")
    parser.add_argument("--error-only", action="store_true", help="Add error-focused filter terms to the task-based query")
    parser.add_argument("--timeout", type=int, default=600, help="Query timeout in seconds")

    # Analysis options
    parser.add_argument("--analyze", action="store_true", help="Analyze the saved logs using local Ollama")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "llama3.2"), help="Ollama model name (e.g., llama3.2, llama3.1, deepseek-r1)")
    parser.add_argument("--ollama-host", default=os.getenv("OLLAMA_HOST", "http://localhost"), help="Ollama host base URL (default http://localhost)")
    parser.add_argument("--ollama-port", type=int, default=int(os.getenv("OLLAMA_PORT", "11434")), help="Ollama port (default 11434)")
    parser.add_argument("--max-chunk-chars", type=int, default=12000, help="Approximate max characters per analysis chunk")
    parser.add_argument("--ollama-timeout", type=int, default=int(os.getenv("OLLAMA_TIMEOUT", "300")), help="Ollama request timeout in seconds per call")

    return parser.parse_args(argv)


def _read_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # Resolve task IDs
    task_ids: List[str] = []
    if args.task_ids:
        task_ids.extend(args.task_ids)
    if args.task_file:
        task_ids.extend(_read_lines(args.task_file))
    task_ids = [t for t in (ti.strip() for ti in task_ids) if t]

    if not task_ids and not args.query:
        LOGGER.error("Provide at least one --task-id/--task-file or a custom --query")
        return 2

    # Resolve log groups
    log_groups: List[str] = []
    if args.log_groups:
        log_groups.extend(args.log_groups)
    if args.log_groups_file:
        log_groups.extend(_read_lines(args.log_groups_file))
    log_groups = [lg for lg in (lg.strip() for lg in log_groups) if lg]
    if not log_groups:
        LOGGER.error("Provide at least one --log-group or --log-groups-file")
        return 2

    output_paths: List[str] = []

    if args.query:
        # Single query path
        try:
            out = fetch_cloudwatch_logs(
                task_id=task_ids[0] if task_ids else "custom",
                aws_profile=args.aws_profile,
                region=args.region,
                log_groups=log_groups,
                query=args.query,
                days=args.days,
                start=args.start,
                end=args.end,
                output_dir=args.output_dir,
                error_only=args.error_only,
                timeout_s=args.timeout,
            )
            output_paths.append(out)
        except Exception as e:
            LOGGER.exception("Query failed: %s", e)
            return 1
    else:
        # One query per task id
        for task_id in task_ids:
            try:
                out = fetch_cloudwatch_logs(
                    task_id=task_id,
                    aws_profile=args.aws_profile,
                    region=args.region,
                    log_groups=log_groups,
                    query=None,
                    days=args.days,
                    start=args.start,
                    end=args.end,
                    output_dir=args.output_dir,
                    error_only=args.error_only,
                    timeout_s=args.timeout,
                )
                output_paths.append(out)
            except Exception as e:
                LOGGER.exception("Query for task %s failed: %s", task_id, e)

    if args.analyze and output_paths:
        try:
            from analyze_logs import analyze_files
        except ImportError:
            LOGGER.error("analyze_logs.py not found or missing dependencies. Skipping analysis.")
            return 0
        for path in output_paths:
            try:
                analyze_files(
                    [path],
                    model=args.model,
                    ollama_host=args.ollama_host,
                    ollama_port=args.ollama_port,
                    max_chunk_chars=args.max_chunk_chars,
                    ollama_timeout=args.ollama_timeout,
                )
            except Exception as e:
                LOGGER.exception("Analysis failed for %s: %s", path, e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
