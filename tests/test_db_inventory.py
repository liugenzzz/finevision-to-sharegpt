"""Ledger inventory: folding grouped rows into a picture of what is loaded."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db_inventory  # noqa: E402


# (dataset, status, lang, batch, rows, with_text, images)
ROWS = [
    ("chartqa", "done", "zh", "b1", 300, 300, 300),
    ("chartqa", "done", "en", "b1", 200, 200, 200),
    ("chartqa", "pending", None, None, 500, 500, 500),
    ("okvqa", "done", "zh", "b2", 100, 0, 250),
]


def test_totals_fold_across_status_lang_and_batch():
    summary = db_inventory.summarize(ROWS)

    assert summary["total"] == 1100
    assert summary["by_status"] == {"done": 600, "pending": 500}
    assert summary["by_lang"] == {"zh": 400, "en": 200, "(未分配)": 500}
    assert summary["by_batch"] == {"b1": 500, "b2": 100, "(无批次)": 500}
    assert summary["images"] == 1250


def test_a_dataset_keeps_its_own_status_and_language_split():
    summary = db_inventory.summarize(ROWS)

    assert summary["datasets"]["chartqa"] == {
        "total": 1000,
        "with_text": 1000,
        "images": 1000,
        "status": {"done": 500, "pending": 500},
        "lang": {"zh": 300, "en": 200, "(未分配)": 500},
    }


def test_rows_without_stored_text_are_counted_separately():
    # okvqa was loaded with store_conversations off: its English source text
    # is not in the ledger and cannot be exported from it.
    summary = db_inventory.summarize(ROWS)

    assert summary["with_text"] == 1000
    assert summary["datasets"]["okvqa"]["with_text"] == 0


def test_an_empty_ledger_folds_to_zero_without_dividing_by_it():
    assert db_inventory.summarize([]) == {
        "total": 0,
        "with_text": 0,
        "images": 0,
        "by_status": {},
        "by_lang": {},
        "by_batch": {},
        "datasets": {},
    }


def test_null_counts_from_sum_over_no_rows_are_treated_as_zero():
    summary = db_inventory.summarize([("okvqa", "rejected", None, None, 5, None, None)])

    assert summary["with_text"] == 0
    assert summary["images"] == 0


# -- report ------------------------------------------------------------------


def fake_query(sql, params=()):
    if "FROM sample_source\n" in sql:
        return ROWS
    if "FROM sample_translation\nGROUP BY" in sql:
        return [("Qwen3.8-27B", "a1b2c3", 400, 400)]
    if "JOIN sample_source" in sql:
        return [("chartqa", 300), ("okvqa", 100)]
    return []


def test_survey_collects_every_section():
    report = db_inventory.survey(fake_query)

    assert report["sources"]["total"] == 1100
    assert report["translated_by_dataset"] == {"chartqa": 300, "okvqa": 100}
    assert report["translations"][0][0] == "Qwen3.8-27B"


def test_report_flags_a_ledger_loaded_without_source_text(capsys):
    rows = [("okvqa", "done", "en", None, 100, 0, 100)]
    db_inventory.print_report(
        {"sources": db_inventory.summarize(rows), "translations": [], "translated_by_dataset": {}},
        storage=[],
        top=10,
    )

    output = capsys.readouterr().out
    assert "一行都没存原文" in output
    assert "store_conversations" in output


def test_report_flags_a_partially_loaded_ledger(capsys):
    db_inventory.print_report(
        {"sources": db_inventory.summarize(ROWS), "translations": [], "translated_by_dataset": {}},
        storage=[],
        top=10,
    )

    assert "前后两次灌库的 store_conversations 设置不一致" in capsys.readouterr().out


def test_an_empty_ledger_says_so_instead_of_printing_an_empty_table(capsys):
    db_inventory.print_report(
        {"sources": db_inventory.summarize([]), "translations": [], "translated_by_dataset": {}},
        storage=[],
        top=10,
    )

    assert "一行都没有" in capsys.readouterr().out
