from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    environment: str = "local"

    database_url: str

    s3_endpoint_url: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket_name: str
    s3_region: str = "auto"
    s3_public_endpoint_url: str | None = Field(
        default=None,
        description="Endpoint that presigned photo GET URLs are signed for -- the host a "
        "browser can reach. Unset means presign against S3_ENDPOINT_URL, which is right when "
        "the API and the browser see storage at the same address (R2 in prod). Set it when "
        "they differ, e.g. S3_ENDPOINT_URL=http://minio:9000 inside docker compose and "
        "S3_PUBLIC_ENDPOINT_URL=http://localhost:9000 for the browser. Uploads and reads "
        "always use S3_ENDPOINT_URL.",
    )

    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_refresh_token: str = ""
    graph_onedrive_folder: str = "/BikeTrip2026"

    static_files_dir: str = "../frontend/dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
