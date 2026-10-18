"""Build a self-contained AR-1001 revision of the existing private probe.

The builder only writes a new private candidate file. Deployment remains an
operator action after review, tests, Zabbix backup and a rollback checkpoint.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
from pathlib import Path
import re


PARSER_NAMES = {"LEGACY_FIELDS", "ENRICHED_KEYS", "MAX_INT", "parse_integrated_psql_row"}
PRESERVED_FUNCTIONS = {"next_state", "sender_lines", "send", "load_state", "save_state"}


def _functions(source: str) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)}


def build(source: str, sql: str, projection: str) -> str:
    parser_tree = ast.parse(projection)
    parser_parts = []
    for node in parser_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in PARSER_NAMES
            for target in node.targets
        ):
            parser_parts.append(ast.get_source_segment(projection, node))
        elif isinstance(node, ast.FunctionDef) and node.name == "parse_integrated_psql_row":
            parser_parts.append(ast.get_source_segment(projection, node))
    if len(parser_parts) != 4:
        raise ValueError("AR1001_PROJECTION_SOURCE_INVALID")
    sql_body = "\n".join(line for line in sql.splitlines()
                         if not line.lstrip().startswith("--"))
    if not sql_body.startswith("BEGIN TRANSACTION READ ONLY;\nSET LOCAL statement_timeout = '3s';"):
        raise ValueError("AR1001_SQL_GUARD_MISSING")
    sql_match = list(re.finditer(r'(?s)^SQL = r""".*?^"""', source, re.MULTILINE))
    if len(sql_match) != 1:
        raise ValueError("AR1001_LEGACY_SQL_NOT_UNIQUE")
    marker = "    rows = [line for line in result.stdout.splitlines() if line]"
    tail = "\n\n\ndef next_state("
    if source.count(marker) != 1 or source.count(tail) != 1:
        raise ValueError("AR1001_LEGACY_PROBE_SHAPE_CHANGED")
    probe_start = source.index(marker)
    probe_end = source.index(tail, probe_start)
    if source.count("        evidence = probe()") != 1 or source.count("    reply = send(metrics)") != 1:
        raise ValueError("AR1001_LEGACY_MAIN_SHAPE_CHANGED")
    embedded = "# Embedded from the versioned AR-1001 projection; one self-contained probe.\n"
    embedded += "\n\n".join(parser_parts) + "\n\nSQL = " + repr(sql)
    candidate = source[:sql_match[0].start()] + embedded + source[sql_match[0].end():]
    probe_start = candidate.index(marker)
    probe_end = candidate.index(tail, probe_start)
    candidate = candidate[:probe_start] + (
        "    legacy, enriched = parse_integrated_psql_row(result.stdout)\n"
        "    return legacy, enriched"
    ) + candidate[probe_end:]
    candidate = candidate.replace("        evidence = probe()", "        evidence, enriched = probe()")
    candidate = candidate.replace("    reply = send(metrics)",
                                  "    metrics.update(enriched)\n    reply = send(metrics)")
    original_functions = _functions(source)
    new_functions = _functions(candidate)
    for name in PRESERVED_FUNCTIONS:
        if ast.dump(original_functions[name]) != ast.dump(new_functions[name]):
            raise ValueError("AR1001_LEGACY_FUNCTION_CHANGED")
    compile(candidate, "ar1001-reply-health-candidate", "exec")
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--sql", type=Path, required=True)
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old = args.source.read_bytes()
    if hashlib.sha256(old).hexdigest() != args.expected_sha256:
        raise ValueError("AR1001_SOURCE_HASH_MISMATCH")
    candidate = build(old.decode(), args.sql.read_text(), args.projection.read_text()).encode()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(candidate)
        stream.flush()
        os.fsync(stream.fileno())
    print("AR1001_CANDIDATE_BUILT", hashlib.sha256(candidate).hexdigest())


if __name__ == "__main__":
    main()
