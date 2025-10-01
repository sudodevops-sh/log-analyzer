# AWS CloudWatch Logs Fetcher + Ollama Analyzer

Fetch filtered logs from AWS CloudWatch Logs Insights and generate an AI-assisted root-cause report using local Ollama models (llama3.2, llama3.1, deepseek-r1). Designed for EKS-/application-failure investigations with production-ready CLI, retries, timeouts, and structured outputs.

## Features
- CLI-driven: no manual prompts; pass task IDs, time ranges, and log groups via flags or files.
- Robust CloudWatch queries: retries with backoff, timeouts, structured logging.
- Sensible defaults: build a Logs Insights query from a task ID with optional error-focused filtering.
- Local AI analysis: summarize large, filtered logs in chunks and produce a final root-cause report using your local Ollama server.
- Structured outputs: timestamped JSON logs; JSON + Markdown analysis reports side-by-side.

## Requirements
- Python 3.9+ (tested with Python 3.13 on Ubuntu)
- AWS credentials configured (e.g., via `aws configure` or environment), with permissions to query your CloudWatch log groups
- Local Ollama server for analysis (default http://localhost:11434) with models:
  - llama3.2 (default), or
  - llama3.1, or
  - deepseek-r1

## Install
It’s recommended to use a virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick Start

### A) Fetch logs for a task ID
```bash
python query_logs.py \
  --task-id 2657010755572505681 \
  --aws-profile az \
  --region us-east-1 \
  --log-group /aws/eks/az-prod-eks-cluster/cluster \
  --log-group /az-prod/eks-cluster-log-group \
  --log-group /az-prod/api-log-group \
  --days 7 \
  --error-only \
  --output-dir ./logs
```
This writes a timestamped JSON file like:
```
./logs/2657010755572505681-YYYYMMDDTHHMMSSZ-logs.json
```
The file contains CloudWatch Logs Insights results["results"] (list of rows).

### B) Fetch logs and analyze in one step
Make sure your Ollama server is running and the model is available (see “Ollama setup” below).
```bash
python query_logs.py \
  --task-id 2657010755572505681 \
  --aws-profile az \
  --region us-east-1 \
  --log-group /aws/eks/az-prod-eks-cluster/cluster \
  --days 7 \
  --error-only \
  --output-dir ./logs \
  --analyze \
  --model llama3.2 \
  --ollama-host http://localhost \
  --ollama-port 11434 \
  --max-chunk-chars 12000
```
This writes:
- The JSON logs file (as above)
- The analysis side by side:
  - `./logs/<file>-report.json` (structured result)
  - `./logs/<file>-report.md` (human-readable summary)

### C) Fetch logs for multiple tasks from files
Create a tasks file with one task ID per line, and a log groups file with one group per line.
```bash
python query_logs.py \
  --task-file ./tasks.txt \
  --log-groups-file ./log_groups.txt \
  --aws-profile az \
  --region us-east-1 \
  --days 30 \
  --output-dir ./logs
```

### D) Use a custom start/end time window
```bash
python query_logs.py \
  --task-id 2657010755572505681 \
  --region us-east-1 \
  --log-group /aws/eks/az-prod-eks-cluster/cluster \
  --start 2025-09-28T00:00:00Z \
  --end   2025-09-29T00:00:00Z \
  --output-dir ./logs
```

### E) Use a custom Logs Insights query
When `--query` is provided, it overrides task-based query building.
```bash
python query_logs.py \
  --query 'fields @timestamp, @message, @logStream, @log | filter @message like /2657010755572505681/ | sort @timestamp asc' \
  --region us-east-1 \
  --log-group /aws/eks/az-prod-eks-cluster/cluster \
  --output-dir ./logs
```

### F) Analyze previously saved JSON logs
```bash
python -c 'from analyze_logs import analyze_files; analyze_files([
  "./logs/2657010755572505681-YYYYMMDDTHHMMSSZ-logs.json"
], model="deepseek-r1", ollama_host="http://localhost", ollama_port=11434)'
```
This generates `-report.json` and `-report.md` alongside the input JSON file.

## Ollama setup (local models)
1) Start (or ensure) the Ollama service is running on your machine:
```bash
ollama serve &   # or run it as a system service
```
2) Pull the model(s) you want to use:
```bash
ollama pull llama3.2
# or
ollama pull llama3.1
# or
ollama pull deepseek-r1
```
3) Verify it’s reachable:
```bash
curl http://localhost:11434/api/tags
```

If your service is not on `localhost:11434`, pass `--ollama-host` and `--ollama-port` to `query_logs.py` or to the `analyze_files` call.

## CLI Reference (query_logs.py)
- `--task-id`: Task ID to search for; can be repeated.
- `--task-file`: File containing one task ID per line.
- `--aws-profile`: AWS profile (defaults to `$AWS_PROFILE` if set).
- `--region`: AWS region (defaults to `$AWS_REGION`/`$AWS_DEFAULT_REGION` or `us-east-1`).
- `--log-group`: CloudWatch log group; can be repeated.
- `--log-groups-file`: File with one log group name per line.
- `--days`: Days lookback (ignored if `--start`/`--end` provided). Default: 7.
- `--start`: ISO 8601 start time (e.g., `2025-09-28T00:00:00Z`).
- `--end`: ISO 8601 end time (e.g., `2025-09-29T00:00:00Z`).
- `--query`: Custom Logs Insights query string (overrides task-based query).
- `--output-dir`: Directory to write outputs. Default: `.`
- `--error-only`: Add error-focused filter terms to the task-based query.
- `--timeout`: Query timeout in seconds. Default: 600.

Analysis options (optional):
- `--analyze`: Also run AI analysis after fetching.
- `--model`: Ollama model (default: `llama3.2`).
- `--ollama-host`: Ollama host base URL (default: `http://localhost`).
- `--ollama-port`: Ollama port (default: `11434`).
- `--max-chunk-chars`: Rough max characters per analysis chunk (default: `12000`).

## Outputs
- Logs JSON: `{output_dir}/{task_id}-{timestamp}-logs.json`
  - Contains the raw `results` array from CloudWatch Logs Insights. Each element is a row: a list of `{field, value}` pairs. Common fields include `@timestamp`, `@message`, `@logStream`, `@log`.
- Analysis JSON: `{logs_file_base}-report.json`
  - Structured root-cause analysis with keys: `task_id`, `root_cause`, `error_type`, `contributing_factors`, `key_events`, `timeline`, `indicators`, `suggested_actions`, `confidence`, `eks_related`.
- Analysis Markdown: `{logs_file_base}-report.md`
  - Human-readable summary suitable for sharing.

## How it works
- Fetch: `query_logs.py` runs a CloudWatch Logs Insights query across provided log groups and time ranges. It retries starts, polls with a timeout, and writes the `results` array to JSON.
- Analyze: `analyze_logs.py` parses the results, selects error-like lines and nearby context, chunks them to fit the model context, summarizes each chunk using Ollama, and combines summaries into a final report.

## Environment Variables
- `AWS_PROFILE`, `AWS_REGION`, `AWS_DEFAULT_REGION` for AWS credentials/region defaults.
- `OLLAMA_HOST` (default `http://localhost`), `OLLAMA_PORT` (default `11434`), `OLLAMA_MODEL` (default `llama3.2`).

## Troubleshooting
- AccessDenied / empty results:
  - Confirm the AWS profile/credentials have `logs:StartQuery` and `logs:GetQueryResults` on the log groups.
  - Verify `--region` and log group names are correct.
- Ollama connection errors:
  - Ensure `ollama serve` is running and the model is pulled (`ollama pull llama3.2`).
  - Test with `curl http://localhost:11434/api/tags`.
  - Adjust `--ollama-host` / `--ollama-port` if your service is not on the default endpoint.
- Large logs:
  - Increase `--max-chunk-chars` if your logs are highly fragmented.
  - Consider using `--error-only` to focus analysis on failure signals.
- Time window:
  - Use `--start` / `--end` for precise windows. Otherwise `--days` defaults to 7.

## Notes & Limitations
- The analyzer expects CloudWatch Logs Insights JSON structure (`results` array of rows). If you modify the query fields, keep `@timestamp`, `@message`, `@logStream`, `@log` to get the best output.
- Automatic discovery of task IDs is not implemented yet. If you’d like this, define how a “task id” appears in logs (regex pattern, fields, log groups), and we can add a discovery mode.
- Keep secrets out of commands and logs. Configure credentials via environment/CLI profiles.

## Development
- Code style: lightweight, standard library logging.
- Key files:
  - `query_logs.py` – CLI for fetching (and optionally analyzing) logs.
  - `analyze_logs.py` – Logic for chunked summarization and final report generation.
 
