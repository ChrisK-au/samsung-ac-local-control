"""Entry point for running: python -m samsung_ac"""

import argparse
import logging
import sys

from .web_app import run


def main():
    parser = argparse.ArgumentParser(description="Samsung AC Local WiFi Control")
    parser.add_argument("-c", "--config", help="Path to config.yaml", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("Samsung AC Local WiFi Control")
    print("Open http://localhost:8080 in your browser")
    print("Or from your phone: http://<this-computer-ip>:8080")
    print()

    run(config_path=args.config)


if __name__ == "__main__":
    main()
