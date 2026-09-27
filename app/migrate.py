"""Apply database migrations (``alembic upgrade head``) programmatically."""
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def run_migrations() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False  # keep the app's logging config
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    run_migrations()
