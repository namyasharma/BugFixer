"""
sandbox.py

Manages the lifecycle of the Docker sandbox: build/reuse the image,
start a container with the target repo mounted in, run commands inside
it, and tear it down.

Design choices, and why:
- One container PER RUN, always destroyed after (even on error) — we
  never want state leaking between runs. A "fix worked" result must be
  attributable to the fix, not to some leftover file from a previous
  attempt.
- The repo is bind-mounted, not COPY'd into the image. This means the
  same base image works for any repo — no rebuilding per-target.
- Dependency installation happens with network ON (repos need pip
  installs), but test execution happens with network OFF, so an
  LLM-generated patch can't phone home or fetch something unexpected
  mid-test-run.
- All output is captured as bytes/text, not streamed to your terminal
  directly, so callers (test_runner.py, orchestrator.py) can parse it
  programmatically instead of scraping stdout.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import BuildError, ContainerError, ImageNotFound

SANDBOX_IMAGE_TAG = "bugfixer-sandbox:latest"
CONTAINER_WORKDIR = "/workspace/repo"
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Sandbox:
    """
    Usage:
        with Sandbox(repo_path=Path("./some_repo")) as sb:
            sb.run("pip install -e .", network=True)
            result = sb.run("pytest --json-report", network=False)
    """

    def __init__(
        self,
        repo_path: Path,
        dockerfile_dir: Path,
        image_tag: str = SANDBOX_IMAGE_TAG,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.dockerfile_dir = Path(dockerfile_dir).resolve()
        self.image_tag = image_tag
        self.client = docker.from_env()
        self.container = None
        self._container_name = f"bugfixer-{uuid.uuid4().hex[:10]}"

    def __enter__(self) -> "Sandbox":
        self._ensure_image_built()
        self._start_container()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.teardown()

    def _ensure_image_built(self):
        """Build the sandbox image if it doesn't already exist locally."""
        try:
            self.client.images.get(self.image_tag)
        except ImageNotFound:
            print(f"[sandbox] Building image {self.image_tag} (first run only)...")
            try:
                self.client.images.build(
                    path=str(self.dockerfile_dir),
                    dockerfile="Dockerfile.sandbox",
                    tag=self.image_tag,
                    rm=True,
                )
            except BuildError as e:
                raise RuntimeError(f"Failed to build sandbox image: {e}") from e

    def _start_container(self):
        """
        Start a long-lived container with the repo bind-mounted in.
        We keep it alive with a no-op command and `exec_run` into it
        for each actual command, rather than starting a fresh container
        per command — much faster for the retry loop.
        """
        self.container = self.client.containers.run(
            self.image_tag,
            name=self._container_name,
            command="sleep infinity",
            volumes={
                str(self.repo_path): {"bind": CONTAINER_WORKDIR, "mode": "rw"}
            },
            working_dir=CONTAINER_WORKDIR,
            detach=True,
            network_mode="bridge",  # start with network on for dependency install
            mem_limit="2g",
            nano_cpus=2_000_000_000,  # cap at 2 CPUs
        )

    def run(
        self,
        command: str,
        network: bool = False,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ExecResult:
        """
        Run a shell command inside the running container.

        network=False disconnects the container from all networks
        before running the command, and reconnects afterward — this is
        what keeps test execution (and patch application) from being
        able to reach the internet.
        """
        if not self.container:
            raise RuntimeError("Sandbox container not started")

        if not network:
            self._set_network(enabled=False)

        try:
            exit_code, output = self.container.exec_run(
                cmd=["bash", "-c", command],
                workdir=CONTAINER_WORKDIR,
                demux=True,
            )
            stdout_b, stderr_b = output
            return ExecResult(
                exit_code=exit_code,
                stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
                stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
            )
        except ContainerError as e:
            return ExecResult(exit_code=1, stdout="", stderr=str(e))
        finally:
            if not network:
                self._set_network(enabled=True)

    def _set_network(self, enabled: bool):
        """Connect/disconnect the container from the bridge network."""
        network = self.client.networks.get("bridge")
        connected = any(
            c.name == self._container_name
            for c in self._containers_on_network(network)
        )
        if enabled and not connected:
            network.connect(self.container)
        elif not enabled and connected:
            network.disconnect(self.container, force=True)

    def _containers_on_network(self, network):
        network.reload()
        return network.containers

    def teardown(self):
        """Always call this — kills and removes the container."""
        if self.container:
            try:
                self.container.remove(force=True)
            except Exception:
                pass  # best-effort cleanup
            self.container = None


def prepare_repo_copy(source_repo: Path) -> Path:
    """
    Copies a repo to a temp directory before mounting it into the
    sandbox. This is a deliberate safety choice: the sandbox mount is
    read-write (tests often need to write cache/tmp files), and we
    never want a run to be able to mutate your actual working copy of
    a repo on disk.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="bugfixer-repo-"))
    dest = tmp_dir / "repo"
    shutil.copytree(source_repo, dest)
    return dest
