import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://pocket:pocket@localhost:5432/pocket"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
