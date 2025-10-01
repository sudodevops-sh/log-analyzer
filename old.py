import boto3
import json
from datetime import datetime, timezone, timedelta
import time

def fetch_cloudwatch_logs(
    task_id: str,
    aws_profile: str,
    region: str,
    log_groups: list,
    query: str,
    days: int = 30          # default = last 30 days
):
    """
    Fetch CloudWatch Logs Insights results for the given log groups and a fixed look-back window.

    :param days: How many days back from now to query (default 30).
    """
    boto3.setup_default_session(profile_name=aws_profile)
    client = boto3.client("logs", region_name=region)

    # Compute epoch seconds for "now" and "days ago"
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    start_epoch = int(start_dt.timestamp())
    end_epoch   = int(end_dt.timestamp())

    print(f"Querying logs from {start_dt} to {end_dt} ({days} days window)")

    response = client.start_query(
        logGroupNames=log_groups,
        startTime=start_epoch,
        endTime=end_epoch,
        queryString=query
    )
    query_id = response["queryId"]
    print(f"Started query: {query_id}")

    # Poll until query completes
    status = "Running"
    while status in ("Running", "Scheduled"):
        time.sleep(2)
        result = client.get_query_results(queryId=query_id)
        status = result["status"]

    print(f"Query finished with status: {status}")

    output_filename = f"{task_id}-logs.json"
    with open(output_filename, "w") as f:
        json.dump(result["results"], f, indent=2)

    print(f"Logs saved to {output_filename}")


if __name__ == "__main__":
    fetch_cloudwatch_logs(
        task_id="2657010755572505681",
        aws_profile="az",
        region="us-east-1",
        log_groups=[
            "/aws/eks/az-prod-eks-cluster/cluster",
            "/az-prod/eks-cluster-log-group",
            "/az-prod/api-log-group"
        ],
        query="fields @timestamp, @message, @logStream, @log | "
              "filter @message like /2657010755572505681/ | "
              "sort @timestamp desc",
        days=30   # adjust if you want a different window
    )
