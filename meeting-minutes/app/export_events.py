"""Fixed-command JSONL-compatible export used through the existing SSH trust."""
from __future__ import annotations

import argparse
import json

from . import db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出详细会议记录增量事件")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.after < 0 or not 1 <= args.limit <= 1000:
        raise SystemExit("--after must be >= 0 and --limit must be 1..1000")
    db.init_db()
    print(json.dumps(db.export_events(args.after, args.limit), ensure_ascii=False,
                     separators=(",", ":")))


if __name__ == "__main__":
    main()
