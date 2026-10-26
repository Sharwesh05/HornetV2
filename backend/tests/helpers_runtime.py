"""Which twin runtime the real-Docker tests use (Phase H8): runc by default, gVisor with HORNET_TEST_RUNTIME=gvisor.

`python -m scripts.dev test` runs the suite once; on a host with runsc, run the Docker tests a second time with
HORNET_TEST_RUNTIME=gvisor so every isolation, gateway and build test also passes under gVisor."""
import os

from app.runtime.docker_driver import GVisorDriver, HardenedDockerDriver

TEST_RUNTIME = os.environ.get("HORNET_TEST_RUNTIME", "runc")


def twin_driver(client=None) -> HardenedDockerDriver:
    if TEST_RUNTIME == "gvisor":
        return GVisorDriver(client)
    if TEST_RUNTIME != "runc":
        raise ValueError(f"HORNET_TEST_RUNTIME must be runc or gvisor, not {TEST_RUNTIME!r}")
    return HardenedDockerDriver(client)
