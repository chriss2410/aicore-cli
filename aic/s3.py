"""S3 operations: upload, list, check existence."""

import json
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from aic import ui
from aic.client import DEFAULT_S3_KEY

DEFAULT_PREFIX = ""


def load_credentials(json_key_path: str) -> dict:
    """Load S3 credentials from a JSON key file."""
    with open(json_key_path) as f:
        creds = json.load(f)
    for key in ("access_key_id", "secret_access_key", "bucket"):
        if key not in creds:
            raise ValueError(f"Missing key in S3 credentials: {key}")
    return creds


def create_client(creds: dict):
    """Create a boto3 S3 client from credentials dict."""
    endpoint_url = f"https://{creds['host']}" if "host" in creds else None
    return boto3.client(
        "s3",
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        region_name=creds.get("region", "eu-central-1"),
        endpoint_url=endpoint_url,
    )


def list_folders(s3_client, bucket: str, prefix: str) -> list[str]:
    """List immediate sub-folders under a prefix (model directories)."""
    prefix = prefix.rstrip("/") + "/"
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
    folders = []
    for cp in response.get("CommonPrefixes", []):
        folder = cp["Prefix"][len(prefix):].rstrip("/")
        if folder:
            folders.append(folder)
    return folders


def list_objects(s3_client, bucket: str, prefix: str, max_keys: int = 200) -> list[dict]:
    """List objects under a prefix. Returns list of {key, size}."""
    prefix = prefix.rstrip("/") + "/"
    try:
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
    except ClientError as e:
        ui.error(f"S3 list failed: {e}")
        return []
    return [{"key": obj["Key"], "size": obj["Size"]} for obj in response.get("Contents", [])]


def path_exists(s3_client, bucket: str, prefix: str) -> bool:
    """Check if any objects exist under prefix."""
    prefix = prefix.rstrip("/") + "/"
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return "Contents" in response


def count_and_size(s3_client, bucket: str, prefix: str) -> tuple[int, int]:
    """Count files and total size under prefix."""
    prefix = prefix.rstrip("/") + "/"
    total_count = 0
    total_size = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            total_count += 1
            total_size += obj["Size"]
    return total_count, total_size


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f}GB"


def upload_folder(s3_client, bucket: str, local_path: str, s3_prefix: str) -> int:
    """Upload a local folder to S3. Returns number of files uploaded."""
    local_path = Path(local_path)
    s3_prefix = s3_prefix.strip("/")
    uploaded = 0

    files = [f for f in local_path.rglob("*") if f.is_file() and ".cache" not in f.parts]
    total = len(files)

    for i, file_path in enumerate(files, 1):
        relative = file_path.relative_to(local_path)
        s3_key = f"{s3_prefix}/{relative}".replace("\\", "/")
        ui.dim(f"  [{i}/{total}] {relative}")
        s3_client.upload_file(str(file_path), bucket, s3_key)
        uploaded += 1

    return uploaded
