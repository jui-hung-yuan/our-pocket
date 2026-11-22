from sqlalchemy import Engine, create_engine

from our_pocket.config import database_url


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url())
