"""Install pinned permuteseq into the disposable PostgreSQL CI container."""

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

COMMIT = "15f86f650002bca5b79a9f85a678388d6999efde"
SHA256 = "e372ca79cfc85b9d7e39f085db45c1ce30125b1d3bc361e2779f4fffd3d9e9ff"


def install(container: str) -> None:
    """Build the pinned extension inside the selected test container."""
    with urlopen(
        f"https://codeload.github.com/dverite/permuteseq/tar.gz/{COMMIT}",
        timeout=60,
    ) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError("permuteseq source checksum mismatch")
    docker = ["docker", "exec", "--user", "root", container]
    version = (
        subprocess.check_output([*docker, "pg_config", "--version"], text=True)
        .split()[1]
        .split(".")[0]
    )
    if not version.isdigit():
        raise ValueError("Cannot determine PostgreSQL major version")
    with tempfile.TemporaryDirectory(
        prefix="papilio-permuteseq-"
    ) as directory:
        source = Path(directory) / "permuteseq.tar.gz"
        source.write_bytes(archive)
        subprocess.run(
            [
                "docker",
                "cp",
                str(source),
                f"{container}:/tmp/permuteseq.tar.gz",
            ],
            check=True,
        )
    subprocess.run([*docker, "apt-get", "update", "-qq"], check=True)
    subprocess.run(
        [
            *docker,
            "apt-get",
            "install",
            "--yes",
            "--no-install-recommends",
            "build-essential",
            f"postgresql-server-dev-{version}",
        ],
        check=True,
    )
    subprocess.run(
        [*docker, "tar", "-xzf", "/tmp/permuteseq.tar.gz", "-C", "/tmp"],
        check=True,
    )
    for target in ([], ["install"]):
        subprocess.run(
            [*docker, "make", "-C", f"/tmp/permuteseq-{COMMIT}", *target],
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container", help="Disposable PostgreSQL container ID")
    install(parser.parse_args().container)
