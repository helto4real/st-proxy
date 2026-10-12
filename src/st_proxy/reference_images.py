"""CPU-only reference preparation and the Helto root image store contract."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from aiohttp import ClientError, ClientSession, FormData

from .config import BrokerConfig
from .http import child_url
from .workflows import Workflow, WorkflowError

MAX_REFERENCE_BYTES = 10 * 1024**2


def _read_image(path: Path, roots: tuple[Path, ...]) -> bytes:
    resolved = path.resolve(strict=True)
    root = next((root for root in roots if resolved.is_relative_to(root)), None)
    if root is None:
        raise WorkflowError("image path is outside the configured input/output directories")
    parts = resolved.relative_to(root).parts
    if not parts:
        raise WorkflowError("image path must name a regular file")
    # Pin each directory; a sandbox client must not swap a parent for a symlink
    # between path validation and the file read.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise WorkflowError("image path must name a regular file")
            data = f.read(MAX_REFERENCE_BYTES + 1)
    finally:
        os.close(directory)
    if not data or len(data) > MAX_REFERENCE_BYTES:
        raise WorkflowError("reference image must be non-empty and at most 10 MiB")
    return data


def _normalize(path: Path, roots: tuple[Path, ...]) -> tuple[bytes, int, int]:
    from PIL import Image, ImageOps

    data = _read_image(path, roots)
    try:
        # Reject oversized decoded images as well as oversized transport payloads.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise WorkflowError("reference image must be PNG, JPEG or WebP")
                source.seek(0)
                oriented = ImageOps.exif_transpose(source)
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                normalized = oriented.convert("RGBA" if alpha else "RGB")
                normalized.info.clear()  # Do not copy source EXIF or text metadata.
                output = io.BytesIO()
                normalized.save(output, format="PNG", compress_level=6)
                data = output.getvalue()
                if len(data) > MAX_REFERENCE_BYTES:
                    raise WorkflowError("normalized reference exceeds 10 MiB")
                return data, normalized.width, normalized.height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise WorkflowError("reference image dimensions exceed the decode limit") from None


@dataclass(frozen=True)
class ReferenceImage:
    workflow_id: str
    root: Path
    filename: str
    width: int
    height: int

    @property
    def path(self) -> Path:
        return self.root / "references" / self.filename

    @property
    def inputs(self) -> dict[str, str]:
        return {"root": str(self.root), "subfolder": "references", "filename": self.filename}

    @property
    def fingerprint(self) -> list[str]:
        return [str(self.root), "references", self.filename]

    def verify(self) -> None:
        data = _read_image(self.path, (self.root,))
        if hashlib.sha256(data).hexdigest() + ".png" != self.filename:
            raise WorkflowError("registered reference has changed; register the image again")


class ReferenceImages:
    def __init__(self, config: BrokerConfig, session: ClientSession) -> None:
        self.config, self.session = config, session
        self.root = Path(config.mcp_output_dir).resolve()
        self.roots = (Path(config.mcp_input_dir).resolve(), self.root)
        self.images: dict[str, ReferenceImage] = {}

    async def register(self, workflow: Workflow, path: str) -> dict:
        if workflow.references is None:
            raise WorkflowError("workflow does not accept references")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise WorkflowError("image path must be absolute")
        try:
            data, width, height = await asyncio.to_thread(_normalize, Path(path), self.roots)
            form = FormData()
            form.add_field("image", data, filename="reference.png", content_type="image/png")
            form.add_field("root", str(self.root))
            form.add_field("subfolder", "references")
            async with asyncio.timeout(self.config.request_timeout):
                async with self.session.post(
                    child_url(self.config.comfy_url, "/helto_load_image_from_root/store"),
                    data=form, allow_redirects=False, auto_decompress=True,
                ) as response:
                    if response.status != 200:
                        raise WorkflowError("reference storage was rejected by ComfyUI")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise WorkflowError("invalid reference storage response")
                    result = json.loads(raw)
            if (not isinstance(result, dict)
                    or not isinstance(result.get("filename"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}\.png", result["filename"])
                    or result.get("imageId") != "sha256:" + result["filename"][:-4]
                    or result.get("mimeType") != "image/png"
                    or any(type(result.get(k)) is not int or result[k] <= 0
                           for k in ("width", "height", "byteSize"))
                    or result["byteSize"] > MAX_REFERENCE_BYTES
                    or (result["width"], result["height"]) != (width, height)):
                raise WorkflowError("invalid reference storage response")
            image = ReferenceImage(workflow.id, self.root, result["filename"], width, height)
            # This is a local ComfyUI deployment; its stored PNG is also Pi's preview.
            await asyncio.to_thread(image.verify)
        except WorkflowError:
            raise
        except (OSError, ValueError, ClientError, TimeoutError):
            raise WorkflowError("reference could not be read, decoded or stored") from None
        image_id = uuid4().hex
        self.images[image_id] = image
        return {"image_id": image_id, "width": width, "height": height,
                "mime_type": "image/png", "preview_path": str(image.path)}

    async def resolve(self, workflow: Workflow, ids: list[str]) -> list[ReferenceImage]:
        if (not isinstance(ids, list)
                or any(not isinstance(i, str) for i in ids)):
            raise WorkflowError("invalid reference list for this workflow")
        workflow.validate_reference_count(len(ids))
        result = []
        for image_id in ids:
            image = self.images.get(image_id)
            if image is None or image.workflow_id != workflow.id:
                raise WorkflowError("reference ID is unknown for this workflow; register it again")
            try:
                await asyncio.to_thread(image.verify)
            except OSError:
                raise WorkflowError(
                    "registered reference is unavailable; register it again",
                ) from None
            result.append(image)
        return result
