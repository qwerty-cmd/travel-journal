# S3-compatible object storage — the *only* module that knows whether it's
# talking to MinIO (local) or Cloudflare R2 (prod). Same boto3 client either
# way, pointed at a different endpoint via config (spec Section 4, "Portability
# principle"). Route handlers and the OneDrive sync job call functions here,
# never boto3 directly.

import boto3

from app.core.config import get_settings

_settings = get_settings()


def _client(endpoint_url: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=_settings.s3_access_key_id,
        aws_secret_access_key=_settings.s3_secret_access_key,
        region_name=_settings.s3_region,
    )


def get_s3_client():
    """Client for uploads and reads -- the endpoint the API process reaches storage at."""
    return _client(_settings.s3_endpoint_url)


def get_presign_client():
    """
    Client for presigning GET URLs a *browser* will fetch.

    SigV4 signs the Host header, so a URL presigned against the internal
    endpoint (``http://minio:9000`` inside docker compose) cannot be made valid
    for another host by rewriting it afterwards -- the signature would no
    longer match. Presigning is offline (no request is sent), so a client
    configured for ``S3_PUBLIC_ENDPOINT_URL`` signs for the host the browser
    uses. Falls back to ``S3_ENDPOINT_URL`` when no public endpoint is set.
    """
    return _client(_settings.s3_public_endpoint_url or _settings.s3_endpoint_url)


BUCKET_NAME = _settings.s3_bucket_name
