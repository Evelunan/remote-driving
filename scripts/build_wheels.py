"""Build each application/shared package without packaging the virtual workspace."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


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
        flags = [
            "--python",
            sys.executable,
            "--build-constraints",
            str(root / "requirements-build.txt"),
            *(["--offline"] if args.offline else []),
        ]
        # Build the wheel from a clean source archive. In-place setuptools
        # build/lib can otherwise reintroduce files deleted from src/.
        with tempfile.TemporaryDirectory(prefix="rd-sdist-") as temporary:
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--package",
                    name,
                    "--sdist",
                    "--out-dir",
                    temporary,
                    *flags,
                ],
                cwd=root,
                check=True,
            )
            (archive,) = Path(temporary).glob("*.tar.gz")
            subprocess.run(
                [
                    "uv",
                    "build",
                    str(archive),
                    "--wheel",
                    "--out-dir",
                    str(args.output_dir.resolve()),
                    *flags,
                ],
                cwd=root,
                check=True,
            )


if __name__ == "__main__":
    main()
