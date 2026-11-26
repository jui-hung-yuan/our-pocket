import pytest
import streamlit as st

from our_pocket.interfaces.web import database


@pytest.fixture
def app_database(conn, shared_connection, monkeypatch):
    """The app reads and writes the test's connection; caches start empty."""
    monkeypatch.setattr(database, "get_engine", lambda: shared_connection)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield conn
    st.cache_resource.clear()
    st.cache_data.clear()
