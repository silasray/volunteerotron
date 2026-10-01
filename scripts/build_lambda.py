"""Build the Lambda code package in .build/lambda (used as CodeUri in template.yaml).

Copies only the app code and installs dependencies as Linux wheels for the
Lambda runtime, so it works from Windows/macOS without Docker and never
packages local files such as .venv or the SQLite database in instance/.

    python scripts/build_lambda.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".build" / "lambda"

# Must match Runtime and Architectures in template.yaml.
PYTHON_VERSION = "3.13"
PLATFORM = "manylinux2014_aarch64"

CODE = ["api", "web", "migrations", "lambda_handlers.py"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for item in CODE:
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, OUT / item, ignore=IGNORE)
        else:
            shutil.copy2(src, OUT / item)

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--requirement", str(ROOT / "requirements.txt"),
            "--target", str(OUT),
            "--platform", PLATFORM,
            "--implementation", "cp",
            "--python-version", PYTHON_VERSION,
            "--only-binary=:all:",
            "--upgrade",
            "--quiet",
            "--disable-pip-version-check",
        ],
        check=True,
    )
    for cache in OUT.rglob("__pycache__"):
        shutil.rmtree(cache)

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"built {OUT.relative_to(ROOT)} ({size / 1_000_000:.1f} MB unzipped)")


if __name__ == "__main__":
    main()
