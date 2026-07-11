#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from string import Template


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render_config.py TEMPLATE OUTPUT", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    rendered = Template(template_path.read_text(encoding="utf-8")).safe_substitute(os.environ)

    unresolved = sorted(set(re.findall(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", rendered)))
    if unresolved:
        raise RuntimeError(f"unresolved environment variables: {', '.join(unresolved)}")

    json.loads(rendered)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered + ("" if rendered.endswith("\n") else "\n"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
