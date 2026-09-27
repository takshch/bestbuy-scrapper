from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    database_url: str

    scraper_headless: bool = True

    # --- Workers / queue ---
    # Browsers each worker process runs in parallel (one page task per browser).
    worker_concurrency: int = 2
    # Seconds between queue polls when idle.
    worker_poll_interval: float = 2.0
    # A running task whose heartbeat is older than this is presumed dead and requeued.
    task_lease_seconds: int = 120
    # Attempts per page task before it is marked failed (requeues after crashes count).
    task_max_attempts: int = 2
    # Random delay before each page request, per browser, to spread load (seconds).
    request_jitter_min: float = 1.0
    request_jitter_max: float = 3.0
    # Upper bound on waiting for a page's lazily loaded product data (seconds).
    page_load_budget: float = 25.0
    # Optional path to a CloakBrowser binary; by default it is downloaded automatically.
    chrome_binary: Optional[str] = None
    # Keep these consistent with the proxy's exit location.
    browser_timezone: str = "America/New_York"
    browser_locale: str = "en-US"

    oxylabs_username: Optional[str] = None
    oxylabs_password: Optional[str] = None
    oxylabs_proxy_type: str = "residential"
    oxylabs_country: str = "US"
    # Optional residential state targeting, e.g. "us_new_york" (match browser_timezone).
    oxylabs_state: Optional[str] = None
    oxylabs_host: Optional[str] = None
    oxylabs_port: Optional[str] = None
    # fallback: deployment IP first, Oxylabs only when that fails (default)
    # always:   every request through Oxylabs;  never: never use Oxylabs
    proxy_mode: Literal["fallback", "always", "never"] = "fallback"
    # After a fallback, send new jobs straight to the proxy for this many seconds
    # before trying the deployment IP again (0 = always try direct first).
    direct_retry_after: int = 1800

    @property
    def proxy_configured(self) -> bool:
        return bool(self.oxylabs_username and self.oxylabs_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
