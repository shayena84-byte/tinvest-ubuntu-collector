#!/usr/bin/env python3
"""Run offline tests with a temporary home and no permitted network connections."""
import argparse
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


def run(pattern="test*.py", verbosity=1, test_directory=None):
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="tinvest-offline-tests-") as directory:
        # Patch before discovery: module-level configuration paths must point to
        # temporary storage as well. Do not change the user's HOME environment.
        with patch.object(Path, "home", return_value=Path(directory)), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in offline tests")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden in offline tests")):
            suite = unittest.defaultTestLoader.discover(str(test_directory or source/"tests"), pattern=pattern)
            result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return run(args.pattern, 2 if args.verbose else 1)


if __name__ == "__main__":
    raise SystemExit(main())
