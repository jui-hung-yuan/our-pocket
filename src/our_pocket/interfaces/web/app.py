"""our-pocket in the browser. Run with `make app`; it serves on localhost only
(.streamlit/config.toml), since it shows real amounts and can commit batches."""

import streamlit as st

from our_pocket.interfaces.web import balances, review

st.set_page_config(page_title="our-pocket", layout="wide")
st.navigation(
    [
        st.Page(balances.render, title="Balances", url_path="balances", default=True),
        st.Page(review.render, title="Review", url_path="review"),
    ]
).run()
