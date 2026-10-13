"""Plan -> build context -> pinned Docker image.

The image is FROM the generic twin base. Files the attacker can write to at runtime (/tmp, /home/user)
are sandboxed in tmpfs, so the plan stores them under /usr/share/hornet/seed and the driver copies
them in when a twin starts.
"""
import hashlib
import shutil
from pathlib import Path
from typing import Protocol

import docker
from docker.errors import BuildError, DockerException

from app.environment.plan import EnvPlan

DOCKERFILE = """FROM {base}
USER root
COPY rootfs/ /
COPY manifest.tsv /tmp/hornet-manifest.tsv
RUN while IFS="$(printf '\\t')" read -r mode uid gid path; do chown "$uid:$gid" "$path" && chmod "$mode" "$path"; done \\
      < /tmp/hornet-manifest.tsv \\
 && rm -f /tmp/hornet-manifest.tsv
LABEL com.hornet.role="twin" com.hornet.profile_hash="{profile_hash}" com.hornet.hostname="{hostname}"
USER 1001:1001
WORKDIR /home/user
CMD ["sleep", "infinity"]
"""


class ImageBuildError(Exception):
    pass


class ImageBuilder(Protocol):
    def build(self, context_dir: Path, tag: str) -> str:
        """Build the image and return its immutable ID (sha256:...)."""

    def remove(self, image_id: str) -> None: ...


def write_context(plan: EnvPlan, ctx_dir: Path, base_image: str, profile_hash: str) -> str:
    """Write rootfs/, manifest.tsv and Dockerfile. Returns a digest of the whole context."""
    if ctx_dir.exists():
        shutil.rmtree(ctx_dir)
    rootfs = ctx_dir / "rootfs"
    rootfs.mkdir(parents=True)
    manifest, h = [], hashlib.sha256()
    for path in sorted(plan.files):
        f = plan.files[path]
        if f.exists_in_base and f.kind == "dir":
            continue  # already in the base image; don't touch its ownership
        target = rootfs / f.image_path.lstrip("/")
        if f.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = (f.content or "").encode()
            target.write_bytes(data)
            h.update(f.image_path.encode() + b"\0" + data)
        manifest.append(f"{f.mode:04o}\t{f.uid}\t{f.gid}\t{f.image_path}")
    (ctx_dir / "manifest.tsv").write_bytes(("\n".join(manifest) + "\n").encode())
    (ctx_dir / "Dockerfile").write_bytes(
        DOCKERFILE.format(base=base_image, profile_hash=profile_hash[:16], hostname=plan.hostname).encode()
    )
    return h.hexdigest()


class DockerImageBuilder:
    def __init__(self, client: docker.DockerClient | None = None):
        self.client = client or docker.from_env()

    def build(self, context_dir: Path, tag: str) -> str:
        try:
            image, _ = self.client.images.build(path=str(context_dir), tag=tag, rm=True, forcerm=True, pull=False)
        except BuildError as exc:
            raise ImageBuildError(f"image build failed: {exc.msg[:200]}") from exc
        except DockerException as exc:
            raise ImageBuildError(f"docker unavailable: {exc}") from exc
        return image.id

    def remove(self, image_id: str) -> None:
        try:
            self.client.images.remove(image_id, force=True)
        except DockerException:
            pass
