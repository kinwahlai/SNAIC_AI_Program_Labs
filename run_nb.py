#!/usr/bin/env python
"""
Headless notebook runner for autonomous fix-loop.
Prints ONLY the failing cell index + source + traceback tail — cheap for Claude.
Writes an executed copy (*_executed.ipynb) for plot/output inspection.

Usage:
    python run_nb.py <notebook.ipynb> [--sample N] [--timeout S] [--out PATH]

Options:
    --sample N   Set CAPSTONE_SAMPLE=N in the kernel env (0 = full data).
                 Use ~30000 for fast iteration, omit for final full-data run.
    --timeout S  Per-cell timeout in seconds (default 1200).
    --out PATH   Path to write executed notebook (default: <nb>_executed.ipynb).

Exit code: 0 = success, 1 = cell execution error or other failure.
"""

import argparse
import os
import sys
import json
import textwrap
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError


def _tail(text, lines=20):
    return "\n".join(text.strip().splitlines()[-lines:])


def run(nb_path: str, sample: int, timeout: int, out_path: str | None) -> int:
    nb_path = Path(nb_path).resolve()
    if not nb_path.exists():
        print(f"ERROR: notebook not found: {nb_path}", file=sys.stderr)
        return 1

    nb = nbformat.read(str(nb_path), as_version=4)

    out = Path(out_path) if out_path else nb_path.with_name(nb_path.stem + "_executed.ipynb")

    # Run from notebook's own directory so relative paths (data/train.csv) resolve correctly.
    kernel_env = {**os.environ, "CAPSTONE_SAMPLE": str(sample)}
    # nbclient doesn't expose kernel_env directly; we inject via os.environ so the
    # spawned IPython kernel inherits it.
    os.environ["CAPSTONE_SAMPLE"] = str(sample)

    client = NotebookClient(
        nb,
        timeout=timeout,
        kernel_name="python3",
        resources={"metadata": {"path": str(nb_path.parent)}},
    )

    print(f"Running: {nb_path.name}")
    print(f"  sample={sample or 'full'}  timeout={timeout}s  out={out.name}")
    print(f"  cwd (kernel): {nb_path.parent}")
    print()

    try:
        with client.setup_kernel():
            for idx, cell in enumerate(nb.cells):
                if cell.cell_type != "code":
                    continue
                src = "".join(cell.source)
                if not src.strip():
                    continue
                try:
                    client.execute_cell(cell, idx)
                except CellExecutionError as e:
                    print(f"✗ FAILED at cell [{idx:02d}]")
                    print(f"\n--- cell source ---")
                    print(textwrap.indent(src[:600], "  "))
                    print(f"\n--- traceback (tail) ---")
                    print(textwrap.indent(_tail(str(e), 25), "  "))
                    # Write what we have so outputs up to failure are inspectable.
                    nbformat.write(nb, str(out))
                    print(f"\nPartial output written to: {out}")
                    return 1

        nbformat.write(nb, str(out))
        print(f"✓ All cells passed. Output: {out}")
        return 0

    except Exception as e:
        print(f"Runner error: {e}", file=sys.stderr)
        return 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("notebook", help="Path to .ipynb file")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                   help="CAPSTONE_SAMPLE rows (0 = full data)")
    p.add_argument("--timeout", type=int, default=1200, metavar="S",
                   help="Per-cell timeout seconds (default 1200)")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Executed notebook output path")
    args = p.parse_args()
    sys.exit(run(args.notebook, args.sample, args.timeout, args.out))


if __name__ == "__main__":
    main()
