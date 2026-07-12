#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from followthrough.adb_bridge import AdbTranscriptBridge


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge Omi on-device Whisper logs into Followthrough")
    parser.add_argument("--serial", default="100.96.0.1:40785")
    parser.add_argument(
        "--receipts",
        type=Path,
        default=Path("data/phone-bridge/receipts.jsonl"),
    )
    args = parser.parse_args()
    AdbTranscriptBridge(
        serial=args.serial,
        receipts=args.receipts,
    ).run_forever()


if __name__ == "__main__":
    main()
