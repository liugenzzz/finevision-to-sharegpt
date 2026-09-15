from __future__ import annotations

import re

DEFAULT_COLLATION = "utf8mb4_0900_ai_ci"
CHARSET_CLAUSE = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE={collation}"

DATASET_VERSION = """
CREATE TABLE IF NOT EXISTS dataset_version (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  dataset       VARCHAR(128)    NOT NULL,
  source_file   VARCHAR(512)    NOT NULL,
  source_hash   CHAR(64)        NOT NULL,
  file_size     BIGINT          NOT NULL,
  file_mtime    BIGINT          NOT NULL,
  images_root   VARCHAR(512)             DEFAULT NULL,
  data_format   VARCHAR(16)     NOT NULL DEFAULT 'sharegpt',
  first_seen_at DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ds_hash (dataset, source_hash),
  KEY idx_dataset (dataset)
) {charset}
"""

SAMPLE_SOURCE = """
CREATE TABLE IF NOT EXISTS sample_source (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  version_id       BIGINT UNSIGNED NOT NULL,
  dataset          VARCHAR(128)    NOT NULL,
  sample_id        VARCHAR(512)    NOT NULL,
  parquet_name     VARCHAR(255)    NOT NULL,
  row_index        INT             NOT NULL,
  conversations    JSON                     DEFAULT NULL,
  image_paths      JSON                     DEFAULT NULL,
  image_count      SMALLINT        NOT NULL DEFAULT 0,
  status           ENUM('pending','claimed','done','failed','rejected') NOT NULL DEFAULT 'pending',
  source_lang      VARCHAR(16)     NOT NULL DEFAULT 'en',
  category         VARCHAR(64)     NOT NULL DEFAULT '',
  lang_assigned    ENUM('zh','en')          DEFAULT NULL,
  reject_reason    VARCHAR(255)             DEFAULT NULL,
  batch_id         VARCHAR(64)              DEFAULT NULL,
  claimed_at       DATETIME                 DEFAULT NULL,
  claim_expires_at DATETIME                 DEFAULT NULL,
  done_at          DATETIME                 DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ver_sample (version_id, sample_id(255)),
  KEY idx_pick (dataset, status, id),
  KEY idx_category (category, status, id, dataset),
  KEY idx_scan (version_id, parquet_name, row_index),
  KEY idx_batch (batch_id),
  KEY idx_expire (status, claim_expires_at)
) {charset}
"""

SAMPLE_TRANSLATION = """
CREATE TABLE IF NOT EXISTS sample_translation (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_id      BIGINT UNSIGNED NOT NULL,
  version_id     BIGINT UNSIGNED NOT NULL,
  sample_id      VARCHAR(512)    NOT NULL,
  conversations  JSON            NOT NULL,
  backend_name   VARCHAR(64)              DEFAULT NULL,
  model_name     VARCHAR(128)    NOT NULL DEFAULT '',
  prompt_version VARCHAR(64)     NOT NULL DEFAULT '',
  batch_id       VARCHAR(64)              DEFAULT NULL,
  latency_ms     INT                      DEFAULT NULL,
  created_at     DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_src_model (source_id, model_name, prompt_version),
  KEY idx_source (source_id),
  KEY idx_batch (batch_id),
  KEY idx_sample (version_id, sample_id(255))
) {charset}
"""

DATASET_CURSOR = """
CREATE TABLE IF NOT EXISTS dataset_cursor (
  version_id            BIGINT UNSIGNED NOT NULL,
  parquet_name          VARCHAR(255)    NOT NULL,
  max_scanned_row_index INT             NOT NULL DEFAULT -1,
  fully_scanned         TINYINT(1)      NOT NULL DEFAULT 0,
  updated_at            DATETIME        NOT NULL,
  PRIMARY KEY (version_id, parquet_name)
) {charset}
"""

_TEMPLATES = (DATASET_VERSION, SAMPLE_SOURCE, SAMPLE_TRANSLATION, DATASET_CURSOR)

# 已经建过表的库不会被 CREATE TABLE IF NOT EXISTS 补上新列，得单独加。
# MySQL 没有 ADD COLUMN IF NOT EXISTS（那是 MariaDB 的写法），只能先问
# information_schema 再决定要不要 ALTER。
_ADDED_COLUMNS = (
    ("sample_source", "source_lang", "VARCHAR(16) NOT NULL DEFAULT 'en' AFTER status"),
    ("sample_source", "category", "VARCHAR(64) NOT NULL DEFAULT '' AFTER source_lang"),
    # CPT 语料和多模态数据集并排躺在 dataset_version 里，光看名字分不出来。
    # 放在这张表而不是 sample_source：CPT 的行根本不进 sample_source，
    # 而且这是数据集级别的属性，不是每行的。这张表只有一百多行，加列不要钱。
    ("dataset_version", "data_format", "VARCHAR(16) NOT NULL DEFAULT 'sharegpt' AFTER images_root"),
)

# 索引同理：CREATE TABLE IF NOT EXISTS 不会给已有表补索引，而 MySQL 也没有
# CREATE INDEX IF NOT EXISTS，只能先查 information_schema。
# idx_category 是抽样的主力。带上 dataset 是因为封顶要按数据集分桶，取候选的
# 查询是 `SELECT id, dataset`——少这一列就得每行回表，百万级回表会把收益吃光。
# id 排在 dataset 前面，所以 ORDER BY id 仍然是零成本。
_ADDED_INDEXES = (
    ("sample_source", "idx_category", ("category", "status", "id", "dataset")),
)


def index_columns_query(table: str, index: str) -> tuple[str, tuple[str, str]]:
    """索引现有的列，按顺序。

    只按名字判断存在与否是不够的：索引定义改过之后，老库里那个同名索引列不对，
    却会被当成「已经有了」而跳过。这里取出列名逐个比，形状不对就重建。
    """

    return (
        "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s "
        "ORDER BY SEQ_IN_INDEX",
        (table, index),
    )


def added_indexes() -> tuple[tuple[str, str, str], ...]:
    """Indexes introduced after the first release, for in-place upgrades."""

    return _ADDED_INDEXES


def missing_column_query(table: str, column: str) -> tuple[str, tuple[str, str]]:
    return (
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )


def added_columns() -> tuple[tuple[str, str, str], ...]:
    """Columns introduced after the first release, for in-place upgrades."""

    return _ADDED_COLUMNS


def table_statements(collation: str = DEFAULT_COLLATION) -> tuple[str, ...]:
    """Table DDL for one collation.

    MySQL 8.0 is the target, but the collation is a parameter so the same
    schema installs on a server that does not carry ``utf8mb4_0900_ai_ci``.
    """

    charset = CHARSET_CLAUSE.format(collation=collation)
    return tuple(template.format(charset=charset) for template in _TEMPLATES)


TABLES = table_statements()

_UNSAFE_VIEW_CHARS = re.compile(r"[^0-9A-Za-z_]+")


def view_name(dataset: str) -> str:
    """Per-dataset view name; the physical rows all live in ``sample_source``."""

    safe = _UNSAFE_VIEW_CHARS.sub("_", dataset).strip("_").lower()
    return f"v_sample_source_{safe or 'dataset'}"


def create_view(dataset: str) -> str:
    """DDL creating (or replacing) the per-dataset view.

    MySQL forbids placeholders in DDL, so the dataset filter is emitted as a
    literal. Backslashes and quotes are escaped and the view name is
    sanitized above, keeping the statement injection-safe.
    """

    literal = dataset.replace("\\", "\\\\").replace("'", "''")
    return (
        f"CREATE OR REPLACE VIEW {view_name(dataset)} AS "
        f"SELECT * FROM sample_source WHERE dataset = '{literal}'"
    )
