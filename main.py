"""
Entry point kept for compatibility: `uvicorn main:app` and `python main.py` still work.

The code lives in the `taf` package - start with taf/__init__.py for a map, or
README.md. Everything can also be driven from a terminal: `python -m taf --help`.
"""
from taf.api.app import app  # noqa: F401  (re-exported for `uvicorn main:app`)

if __name__ == "__main__":
    from taf.cli import main

    raise SystemExit(main(["serve"]))
