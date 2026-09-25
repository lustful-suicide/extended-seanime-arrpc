#!/usr/bin/env python3
"""Build the single-file Seanime plugin payload.

Injects arrpc_helper.py (JSON-escaped) into plugin.src.ts at the
"__ARRPC_HELPER_PY__" placeholder and writes arrpc-bridge.ts.
Verifies the round-trip: decoding the embedded string must reproduce the
helper source byte-for-byte.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "plugin.src.ts")
HELPER = os.path.join(HERE, "arrpc_helper.py")
OUT = os.path.join(HERE, "arrpc-bridge.ts")
PLACEHOLDER = '"__ARRPC_HELPER_PY__"'


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        template = f.read()
    with open(HELPER, "r", encoding="utf-8") as f:
        helper = f.read()

    if PLACEHOLDER not in template:
        print("placeholder %s not found in %s" % (PLACEHOLDER, SRC))
        return 1

    embedded = json.dumps(helper)  # double-quoted JS string literal
    out = template.replace(PLACEHOLDER, embedded)

    # Round-trip check: extract the literal back and compare.
    start = out.index(embedded)
    decoded = json.loads(out[start:start + len(embedded)])
    assert decoded == helper, "round-trip mismatch!"

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out)
    print("wrote %s (%d bytes, helper %d bytes)" % (OUT, len(out), len(helper)))


if __name__ == "__main__":
    sys.exit(main())
