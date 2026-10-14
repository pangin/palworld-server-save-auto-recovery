#!/usr/bin/env python3
"""Only used by the publisher Git subprocess; never prints a stored credential."""
import os
import sys

if "username" in sys.argv[-1].lower():
    print("x-access-token")
else:
    print(os.environ["CYCLE_PUSH_TOKEN"])
