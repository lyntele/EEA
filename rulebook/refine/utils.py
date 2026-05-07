import csv
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SQLResultSummary:
    result_type: str
    columns: Optional[List[str]]
    row_count: int
    sample_rows: List[Tuple[Any, ...]]
    row_hash: Optional[str]
    error: Optional[str]


class SQLExecutionThread(threading.Thread):
    def __init__(self, db_path: str, sql: str, timeout: int = 30):
        super().__init__()
        self.db_path = db_path
        self.sql = sql
        self.timeout = timeout
        self.result_rows = None
        self.result_cols = None
        self.exception = None
        self.timeout_event = threading.Event()

    def run(self):
        def check_timeout():
            if self.timeout_event.is_set():
                raise TimeoutError(f"SQL execution timed out after {self.timeout} seconds")

        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.text_factory = lambda x: str(x, "utf-8", errors="replace")
                conn.set_progress_handler(check_timeout, 1000)
                cursor = conn.cursor()
                cursor.execute(self.sql)
                self.result_cols = [d[0] for d in cursor.description] if cursor.description else []
                self.result_rows = cursor.fetchall()
        except Exception as exc:
            self.exception = exc


def _hash_rows(rows: List[Tuple[Any, ...]]) -> str:
    normalized = json.dumps(rows, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def execute_and_summarize(db_path: str, sql: str, timeout: int = 30, sample_size: int = 3) -> SQLResultSummary:
    thread = SQLExecutionThread(db_path, sql, timeout)
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        thread.timeout_event.set()
        thread.join(1)
        return SQLResultSummary(
            result_type="timeout",
            columns=None,
            row_count=0,
            sample_rows=[],
            row_hash=None,
            error=f"SQL execution timed out after {timeout} seconds",
        )
    if thread.exception:
        return SQLResultSummary(
            result_type="execution_error",
            columns=None,
            row_count=0,
            sample_rows=[],
            row_hash=None,
            error=str(thread.exception),
        )

    rows = thread.result_rows or []
    cols = thread.result_cols or []
    if len(rows) == 0:
        return SQLResultSummary(
            result_type="empty_result",
            columns=cols,
            row_count=0,
            sample_rows=[],
            row_hash=_hash_rows(rows),
            error=None,
        )

    all_null = not any(any(val is not None for val in row) for row in rows)
    result_type = "all_null_result" if all_null else "success"
    sample_rows = rows[:sample_size]
    return SQLResultSummary(
        result_type=result_type,
        columns=cols,
        row_count=len(rows),
        sample_rows=sample_rows,
        row_hash=_hash_rows(rows),
        error=None,
    )


def compare_results(
    gold_summary: SQLResultSummary,
    candidate_summary: SQLResultSummary,
) -> Tuple[str, Optional[str]]:
    """
    Compare results using Execution Accuracy (EX) standard from BIRD evaluation.
    EX: set(predicted_res) == set(ground_truth_res)

    Only compares result values as sets, ignoring column names and row ordering.
    """
    if candidate_summary.result_type in {"execution_error"}:
        return "WRONG", "error"
    if candidate_summary.result_type == "timeout":
        return "WRONG", "timeout"
    if gold_summary.result_type != candidate_summary.result_type:
        return "WRONG", "error"

    gold_set = set(tuple(map(str, row)) for row in gold_summary.sample_rows)
    cand_set = set(tuple(map(str, row)) for row in candidate_summary.sample_rows)

    if gold_set == cand_set:
        return "CORRECT", None

    if gold_summary.row_count != candidate_summary.row_count:
        return "WRONG", "row_count"
    if gold_summary.columns != candidate_summary.columns:
        return "WRONG", "schema"
    return "WRONG", "value"


def load_cases_jsonl(path: Path) -> List[Dict[str, Any]]:
    cases = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def load_schema_and_descriptions(db_path: str) -> Tuple[str, Dict[str, str]]:
    db_path = Path(db_path)
    schema_profile = _build_schema_profile(db_path)
    column_desc = _load_column_descriptions(db_path)
    return schema_profile, column_desc


def _execute_pragma(db_path: Path, sql: str) -> List[Tuple[Any, ...]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return cursor.fetchall()


def _load_table_names(db_path: Path) -> List[str]:
    rows = _execute_pragma(db_path, "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence';")
    return [row[0] for row in rows]


def _load_table_info(db_path: Path, table_name: str) -> List[Tuple[Any, ...]]:
    return _execute_pragma(db_path, f"PRAGMA table_info(`{table_name}`);")


def _load_foreign_keys(db_path: Path, table_name: str) -> List[Tuple[Any, ...]]:
    return _execute_pragma(db_path, f"PRAGMA foreign_key_list(`{table_name}`);")


def _load_column_descriptions(db_path: Path) -> Dict[str, str]:
    descriptions = {}
    desc_dir = db_path.parent / "database_description"
    if not desc_dir.exists():
        return descriptions
    for csv_file in desc_dir.glob("*.csv"):
        table_name = csv_file.stem
        with open(csv_file, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                col_name = (row.get("original_column_name") or "").strip()
                if not col_name:
                    continue
                desc_parts = []
                for key in ["column_name", "column_description", "value_description"]:
                    value = (row.get(key) or "").strip()
                    if value:
                        desc_parts.append(value)
                if desc_parts:
                    descriptions[f"{table_name}.{col_name}"] = " | ".join(desc_parts)
    return descriptions


def _build_schema_profile(db_path: Path) -> str:
    table_names = _load_table_names(db_path)
    lines = [f"Database ID: `{db_path.stem}`", "Schema:"]
    for table_name in table_names:
        lines.append(f"- Table: `{table_name}`")
        lines.append("[")
        columns = _load_table_info(db_path, table_name)
        for col in columns:
            col_name = col[1]
            col_type = col[2]
            is_pk = col[5] != 0
            pk_tag = " | Primary Key" if is_pk else ""
            lines.append(f"(`{col_name}`: {col_type}{pk_tag})")
        lines.append("]")
        fks = _load_foreign_keys(db_path, table_name)
        if fks:
            lines.append("Foreign Keys:")
            for fk in fks:
                lines.append(f"`{table_name}`.`{fk[3]}` = `{fk[2]}`.`{fk[4]}`")
    return "\n".join(lines)
