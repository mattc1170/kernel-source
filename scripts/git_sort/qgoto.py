#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import argparse
import os
import subprocess
import sys

import lib


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Print the quilt push or pop command required to reach the "
        "position where the specified commit should be imported.")
    parser.add_argument("rev", help="Upstream commit id.")
    args = parser.parse_args()

    if not lib.check_series():
        sys.exit(1)

    # remove "patches/" prefix
    top = subprocess.check_output(("quilt", "top",),
                                  preexec_fn=lib.restore_signals).strip()[8:]

    series = open("series")
    os.chdir("patches")

    try:
        (name, delta,) = lib.sequence_insert(series, args.rev, top)
    except lib.KSException as err:
        print("Error: %s" % (err,), file=sys.stderr)
        sys.exit(1)

    if delta > 0:
        print("push %d" % (delta,))
    elif delta < 0:
        print("pop %d" % (-1 * delta,))
