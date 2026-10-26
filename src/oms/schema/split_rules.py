"""Report what the schema 3 rule split would do, without changing anything.

    python -m oms.schema.split_rules --report

Reads the Neo4j database named by OMS_NEO4J_URI, OMS_NEO4J_USER and
OMS_NEO4J_PASSWORD. For each tenant it lists the rules shared between skills,
the skills affected, the copies the split will make, the held items it will
copy or move, and the divergence items it will close. It writes nothing. A
rule it cannot plan (a damaged graph) stops it with that rule's refusal and
its repair, and the exit status 1.
"""
from __future__ import annotations

import argparse
import sys

from oms.schema.metadata import SchemaCompatibilityError
from oms.schema.migrations.rule_split import format_report, plan_split


def _driver():
    from oms.adapters.neo4j.driver import build_driver
    from oms.settings.core import CoreSettings
    settings = CoreSettings.from_env()
    return build_driver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m oms.schema.split_rules",
                                     description="Report what the rule split would do. Writes nothing.")
    parser.add_argument("--report", action="store_true", required=True,
                        help="print the report (the only mode: the split itself runs at start-up)")
    parser.parse_args(argv)
    driver = _driver()
    try:
        sys.stdout.write(format_report(plan_split(driver)))
    except SchemaCompatibilityError as refused:
        sys.stderr.write(f"Rule split report: nothing has been changed. The report stopped because {refused}.\n")
        return 1
    finally:
        if hasattr(driver, "close"):
            driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
