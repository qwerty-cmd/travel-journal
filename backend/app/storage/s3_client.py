# S3-compatible object storage — the *only* module that knows whether it's
# talking to MinIO (local) or Cloudflare R2 (prod). Same boto3 client either
# way, pointed at a different endpoint via config (spec Section 4, "Portability
# principle"). Route handlers and the OneDrive sync job call functions here,
# never boto3 directly.

import boto3

from app.core.config import get_settings

_settings = get_settings()


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_settings.s3_endpoint_url,
        aws_access_key_id=_settings.s3_access_key_id,
        aws_secret_access_key=_settings.s3_secret_access_key,
        region_name=_settings.s3_region,
    )


BUCKET_NAME = _settings.s3_bucket_name
