"""Review page: a staged batch's flags, then commit or reject it. The same rules as the
CLI, since both call BatchStore: errors block a commit, warnings need accepting."""

from collections import Counter

import streamlit as st

from our_pocket.etl.load import BatchError, BatchReport, BatchStore
from our_pocket.interfaces.web import database


def _decide(report: BatchReport) -> None:
    st.subheader("Decision")
    if report.errors:
        st.error(
            f"{report.errors} error(s) block the commit: fix the source file and import it "
            "again, or reject the batch."
        )
    else:
        accepted = not report.warnings or st.checkbox(
            f"I reviewed the {report.warnings} warning(s) and accept them"
        )
        if st.button("Commit to the ledger", type="primary", disabled=not accepted):
            try:
                with database.engine().begin() as conn:
                    result = BatchStore(conn).commit(
                        report.batch_id, accept_warnings=bool(report.warnings)
                    )
            except BatchError as exc:
                st.error(str(exc))
                return
            st.session_state["notice"] = (
                f"Batch {result.batch_id} committed: {result.inserted} ledger entries, "
                f"{result.skipped_duplicates} duplicates skipped."
            )
            st.rerun()
    reason = st.text_input("Reason for rejecting")
    if st.button("Reject", disabled=not reason.strip()):
        try:
            with database.engine().begin() as conn:
                BatchStore(conn).reject(report.batch_id, reason.strip())
        except BatchError as exc:
            st.error(str(exc))
            return
        st.session_state["notice"] = f"Batch {report.batch_id} rejected."
        st.rerun()


def render() -> None:
    st.title("Review")
    if notice := st.session_state.pop("notice", None):
        st.success(notice)
    with database.engine().connect() as conn:
        store = BatchStore(conn)
        reports = [store.report(batch_id) for batch_id in store.find_batch_ids("staged")]
    if not reports:
        st.info("Nothing to review: no staged batches. Import a file with `our-pocket import`.")
        return
    choices = {f"#{r.batch_id}  {r.file_name} → {r.account}": r for r in reports}
    report = choices[st.selectbox("Staged batch", list(choices)) or next(iter(choices))]

    columns = st.columns(4)
    columns[0].metric("Rows", report.row_count, border=True)
    columns[1].metric("Already in the ledger", report.duplicate_count, border=True)
    columns[2].metric("Errors", report.errors, border=True)
    columns[3].metric("Warnings", report.warnings, border=True)
    st.caption(
        f"Schema {report.schema_id} v{report.schema_version}, parser {report.parser_version}."
    )
    if report.flags:
        by_code = Counter((f["severity"], f["code"]) for f in report.flags)
        st.dataframe(
            [{"severity": s, "code": c, "rows": n} for (s, c), n in sorted(by_code.items())],
            hide_index=True,
        )
        st.caption("Each flag, with the line it points at in the file:")
        st.dataframe(report.flags, hide_index=True)
    _decide(report)
