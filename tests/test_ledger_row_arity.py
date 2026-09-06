"""写库的每一行，长度必须和 SQL 里的占位符数一致。

这类错只在 flush 那一刻才炸，而 flush 是攒满一批才发生的：现场跑了一小时二十
分钟、攒够 200 条被拒的行才崩，堆栈还指在 pool 里，看不出是哪个调用点少给了
一个值。加一列时改了 claim 忘了 mark_rejected，就是这么来的。

不连数据库：只比对语句里的 %s 个数和各调用点构造的元组长度。
"""

from finevision_to_sharegpt.db import mysql_ledger
from finevision_to_sharegpt.db.config import MysqlConfig
from finevision_to_sharegpt.db.ledger import DatasetVersion


class RecordingWriter:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def make_ledger():
    """不走 __init__，因为它会连库。这里只测行的形状。"""

    ledger = object.__new__(mysql_ledger.MySQLLedger)
    ledger.config = MysqlConfig(
        host="127.0.0.1", port=3306, user="u", password="p", database="d"
    )
    ledger.batch_id = "20260906-000000"
    ledger._source_writer = RecordingWriter()
    ledger._status_writer = RecordingWriter()
    ledger._translation_writer = RecordingWriter()
    # __init__ 里建的去重集合，mark_done 会往里塞
    ledger.completed_ids = set()
    return ledger


def placeholders(statement: str) -> int:
    return statement.count("%s")


VERSION = DatasetVersion(dataset="chartqa", version_id=7, source_lang="en")


def test_claim_builds_a_row_matching_the_upsert():
    ledger = make_ledger()

    ledger.claim(VERSION, "part.parquet", 3, "chartqa:part.parquet:3", [], ["a.jpg"])

    assert len(ledger._source_writer.rows[0]) == placeholders(mysql_ledger._UPSERT_SOURCE)


def test_mark_rejected_builds_a_row_matching_the_upsert():
    """漏掉一个值的那次，正是这里。"""

    ledger = make_ledger()

    ledger.mark_rejected(VERSION, "part.parquet", 3, "chartqa:part.parquet:3", "missing_image")

    assert len(ledger._source_writer.rows[0]) == placeholders(mysql_ledger._UPSERT_SOURCE)


def test_every_source_row_carries_the_same_columns():
    """两个调用点写的是同一张表，列的顺序也必须一致。"""

    ledger = make_ledger()
    ledger.claim(VERSION, "part.parquet", 1, "s:1", [], ["a.jpg"])
    ledger.mark_rejected(VERSION, "part.parquet", 2, "s:2", "missing_text")

    claimed, rejected = ledger._source_writer.rows
    assert len(claimed) == len(rejected)
    # source_lang 在 status 之后、reject_reason 之前，两行都得对上。
    status_index = 8
    assert claimed[status_index + 1] == rejected[status_index + 1] == "en"


def test_record_translation_matches_its_statement():
    ledger = make_ledger()

    ledger.record_translation(VERSION, "s:1", [], "vllm-8001", "m", "v1", 120)

    assert len(ledger._translation_writer.rows[0]) == placeholders(
        mysql_ledger._INSERT_TRANSLATION
    )


def test_mark_done_and_mark_failed_match_the_status_statement():
    ledger = make_ledger()

    ledger.mark_done(VERSION, "s:1", "zh")
    ledger.mark_failed(VERSION, "s:2", "boom")

    for row in ledger._status_writer.rows:
        assert len(row) == placeholders(mysql_ledger._UPDATE_STATUS)
