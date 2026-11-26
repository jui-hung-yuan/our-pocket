"""Database access for the app: one engine per process, and ledger reads cached until new
entries are committed."""

import streamlit as st
from sqlalchemy import Engine

from our_pocket.db import get_engine
from our_pocket.services import HistoryPoint, LedgerService


@st.cache_resource
def engine() -> Engine:
    return get_engine()


def ledger_version() -> int:
    with engine().connect() as conn:
        return LedgerService(conn).last_entry_id()


@st.cache_data
def history(
    version: int, account_name: str | None = None, institution: str | None = None
) -> list[HistoryPoint]:
    """`version` is the cache key: pass ledger_version(), so a commit refreshes the chart,
    whether it came from the Review page or the CLI."""
    with engine().connect() as conn:
        return LedgerService(conn).history(account_name, institution)
