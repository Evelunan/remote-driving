"""Build each application/shared package without packaging the virtual workspace."""

import argparse
import subprocess
import tomllib
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=root / "dist")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    manifests = sorted(root.glob("packages/*/pyproject.toml")) + sorted(
        root.glob("apps/*/pyproject.toml")
    )
    for manifest in manifests:
        name = tomllib.loads(manifest.read_text())["project"]["name"]
        command = [
            "uv",
            "build",
            "--package",
            name,
            "--wheel",
            "--out-dir",
            str(args.output_dir.resolve()),
        ]
        if args.offline:
            command.append("--offline")
        subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
