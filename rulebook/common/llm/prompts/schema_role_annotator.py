"""Schema role annotator prompt.

This prompt assigns per-column role_family hints for one database/local schema
view. role_family values are free natural short phrases, not a closed ontology.
"""

from __future__ import annotations


SCHEMA_ROLE_ANNOTATOR_PROMPT = """\
Task:
Assign a compact semantic role_family to each schema column.

Purpose:
The role_family is used only as a schema-local semantic hint for matching SQL
roles. It must be derived from the schema evidence below, not from a fixed
vocabulary or database-specific rules.

Input:
{schema_payload_json}

Definitions:
- role_family is a short free-form phrase, stable within this database.
- Use the same role_family for columns that play the same semantic role in this
  database, even if their column names differ.
- Prefer specific roles when evidence supports them, e.g. "editor reference",
  "event type marker", "monetary amount", "timestamp", "owner reference".
  These are examples, not an allowed list.
- If evidence is insufficient, use "unknown".
- Do not copy table names, database names, case ids, or SQL aliases into the
  role_family.
- Do not infer that every column ending in "_id" has the same role; distinguish
  what entity or relationship it refers to when FK/description evidence shows it.

Return strict JSON only:
{{
  "db_id": "...",
  "tables": [
    {{
      "name": "table name",
      "columns": [
        {{
          "name": "column name",
          "role_family": "free natural phrase or unknown",
          "rationale": "short evidence-based reason"
        }}
      ]
    }}
  ]
}}
"""


def build_schema_role_annotator_prompt(*, schema_payload_json: str) -> str:
    return SCHEMA_ROLE_ANNOTATOR_PROMPT.format(schema_payload_json=schema_payload_json)


__all__ = ["SCHEMA_ROLE_ANNOTATOR_PROMPT", "build_schema_role_annotator_prompt"]
