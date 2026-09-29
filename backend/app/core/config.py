from functools import lru_cache

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

    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_refresh_token: str = ""
    graph_onedrive_folder: str = "/BikeTrip2026"

    static_files_dir: str = "../frontend/dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
