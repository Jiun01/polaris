"""Polaris v2 ingestion worker entrypoint — Phase 0 stub.

Real work lands in Phase 4+ (RAG corpus ingestion, embeddings, etc.). For now,
just an argparse stub so `--help` works and the deploy shape is right.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polaris-ingest",
        description="Polaris v2 ingestion worker (Phase 0 stub — no real work yet).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("noop", help="Phase 0 placeholder; exits 0 without doing anything.")
    sub.add_parser(
        "version",
        help="Print version and exit.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing (Phase 4+ uses this).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "noop"):
        print("polaris-ingest: Phase 0 stub — no work to do.")
        return 0

    if args.command == "version":
        from polaris_ingest import __version__

        print(__version__)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
