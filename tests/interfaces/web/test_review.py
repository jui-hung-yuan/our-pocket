"""Review page: the same rules as the CLI. Errors block a commit, warnings need accepting,
a rejection needs a reason."""

import pytest
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

from our_pocket.db.schema import ledger_entry
from our_pocket.etl.load import BatchStore
from our_pocket.etl.pipeline import ImportPipeline
from our_pocket.synthetic.trade_republic import SyntheticExport

pytestmark = pytest.mark.db


def _page():
    from our_pocket.interfaces.web import review

    review.render()


def _stage(conn, account, export: SyntheticExport, name: str = "tr.csv") -> int:
    return ImportPipeline(conn).run(export.to_csv_bytes(), name, account)


def _button(page: AppTest, label: str):
    return next(b for b in page.button if b.label == label)


def _status(conn, batch_id: int) -> str:
    return BatchStore(conn).report(batch_id).status


def test_nothing_staged(app_database):
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert not page.exception
    assert "Nothing to review" in page.info[0].value


def test_warnings_must_be_accepted_before_commit(app_database, tr_account, synthetic):
    batch_id = _stage(app_database, tr_account, synthetic)  # STOCKPERK: one warning
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert {m.label: m.value for m in page.metric} == {
        "Rows": str(len(synthetic.rows)),
        "Already in the ledger": "0",
        "Errors": "0",
        "Warnings": "1",
    }
    assert _button(page, "Commit to the ledger").disabled
    page.checkbox[0].check().run()
    _button(page, "Commit to the ledger").click().run()
    assert not page.exception
    assert _status(app_database, batch_id) == "committed"
    entries = app_database.execute(select(func.count()).select_from(ledger_entry)).scalar_one()
    assert entries == len(synthetic.rows)
    assert page.success[0].value.startswith(f"Batch {batch_id} committed: {entries} ledger entries")
    assert "Nothing to review" in page.info[0].value


def test_each_flag_shows_the_raw_fields_of_its_line(app_database, tr_account, synthetic):
    _stage(app_database, tr_account, synthetic)  # STOCKPERK: one warning
    page = AppTest.from_function(_page, default_timeout=10).run()
    [flag] = page.dataframe[1].value.to_dict("records")
    perk = next(r for r in synthetic.rows if r["type"] == "STOCKPERK")
    assert flag["code"] == "cash_semantics_unverified"
    assert flag["transaction_id"] == perk["transaction_id"]
    assert flag["type"] == "STOCKPERK"


def test_a_raw_field_named_like_a_flag_column_does_not_overwrite_it():
    from our_pocket.interfaces.web.review import _with_raw_fields

    flag = {"code": "unknown_type", "line_number": 7, "message": "unknown type"}
    row = _with_raw_fields(flag, {7: {"message": "Rent", "amount": "-900"}})
    assert row == flag | {"file: message": "Rent", "amount": "-900"}
    assert _with_raw_fields({"code": "x", "line_number": None}, {}) == {
        "code": "x",
        "line_number": None,
    }


def test_errors_leave_only_rejecting(app_database, tr_account, synthetic):
    rows = [dict(r) for r in synthetic.rows]
    rows[3]["amount"] = "not-a-number"
    batch_id = _stage(app_database, tr_account, SyntheticExport(rows=rows, holdings={}))
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert "block the commit" in page.error[0].value
    assert [b.label for b in page.button] == ["Reject"]
    assert _button(page, "Reject").disabled  # no reason yet
    page.text_input[0].input("amount column broken").run()
    _button(page, "Reject").click().run()
    assert _status(app_database, batch_id) == "rejected"
    assert page.success[0].value == f"Batch {batch_id} rejected."


def test_each_staged_batch_can_be_picked(app_database, tr_account, synthetic):
    first = _stage(
        app_database, tr_account, SyntheticExport(rows=synthetic.rows[:10], holdings={}), "a.csv"
    )
    second = _stage(app_database, tr_account, synthetic, "b.csv")
    page = AppTest.from_function(_page, default_timeout=10).run()
    assert page.selectbox[0].options == [
        f"#{first}  a.csv → TR Depot",
        f"#{second}  b.csv → TR Depot",
    ]
    page.selectbox[0].select(page.selectbox[0].options[1]).run()
    assert {m.label: m.value for m in page.metric}["Rows"] == str(len(synthetic.rows))
