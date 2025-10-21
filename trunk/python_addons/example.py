#!/usr/bin/env python3
"""Example SRS python addon that periodically prints a message."""

import argparse
import logging
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Example SRS python addon")
    parser.add_argument(
        "--message",
        default="python_addon: test message",
        help="Message to print to stdout.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds to sleep between prints.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        stream=sys.stdout,
    )

    logging.info("Starting example addon with interval=%s", args.interval)

    try:
        while True:
            logging.info(args.message)
            sys.stdout.flush()
            time.sleep(max(args.interval, 0.1))
    except KeyboardInterrupt:
        logging.info("Received KeyboardInterrupt, exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
