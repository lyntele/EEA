#!/usr/bin/env python3
"""Offline probe for implicit subquery join-edge extraction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from method.EEA.rulebook.common.analysis.role_graph_normalizer import RoleGraphNormalizer
from method.EEA.rulebook.common.analysis.signal_summary import _norm_join_edges
from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature
from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess
from method.EEA.rulebook.common.io.local_schema import build_local_schema_view


DEFAULT_CASES_JSON = Path("/data/liuyining/ace4sql/bench/bird/dev/dev.json")
DEFAULT_DB_ROOT = Path("/data/liuyining/ace4sql/bench/bird/dev/dev_databases")


def _case_sqls(cases_json: Path) -> Dict[str, str]:
    rows = json.loads(cases_json.read_text())
    return {
        str(row.get("question_id") or row.get("id")): str(row.get("SQL") or "")
        for row in rows
        if row.get("db_id") == "card_games"
    }


def _role_edges(sql: str, db_root: Path) -> list[str]:
    db_path = db_root / "card_games" / "card_games.sqlite"
    access = SqliteDBSchemaAccess("card_games", str(db_path), str(db_path.parent))
    view, _ = build_local_schema_view(
        db_id="card_games",
        access=access,
        t_pred=["cards", "set_translations", "foreign_data"],
        t_q=[],
        t_mem=[],
    )
    role_graph = RoleGraphNormalizer().normalize_sql(
        sql=sql,
        schema_view=view,
        source="target_sql",
    )
    return _norm_join_edges(role_graph)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases_json", type=Path, default=DEFAULT_CASES_JSON)
    parser.add_argument("--db_root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--output_json", type=Path, default=Path("/tmp/subquery_join_edge_probe.json"))
    args = parser.parse_args()

    sqls = _case_sqls(args.cases_json)
    rows: Dict[str, Dict[str, Any]] = {}
    for qid in ("360", "438", "365"):
        ast = cached_ast_signature(sqls[qid])
        rows[qid] = {
            "sql": sqls[qid],
            "ast_join_edges": ast.get("join_edges") or [],
            "role_graph_join_edges": _role_edges(sqls[qid], args.db_root),
        }
    payload = {
        "rows": rows,
        "pass": (
            "cards.id=set_translations.id" in rows["360"]["role_graph_join_edges"]
            and rows["438"]["role_graph_join_edges"] == []
            and "cards.uuid=foreign_data.uuid" in rows["365"]["role_graph_join_edges"]
        ),
        "notes": {
            "q360": "implicit IN subquery edge should connect cards and set_translations",
            "q438": "single-table gold SQL should not fabricate a join edge",
            "q365": "explicit JOIN edge should remain intact",
        },
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
