import hashlib
import logging
import os

import fsspec
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper

logger = logging.getLogger(__name__)

fs, _base_path = fsspec.core.url_to_fs(os.environ["STORAGE_BASE_PATH"])
# Remote backends (gcs, s3) are natively async; local is sync, so wrap it.
# Either way we drive blob I/O through the async (`_`-prefixed) methods so a
# slow write never blocks the event loop and stalls other in-flight captures.
if not fs.async_impl:
    fs = AsyncFileSystemWrapper(fs)


def content_path(hash_hex: str) -> str:
    return os.path.join(_base_path, hash_hex)


async def put_blob(data: bytes) -> str:
    """Store *data* and return the content hash."""
    hash_hex = hashlib.sha256(data).hexdigest()
    path = content_path(hash_hex)

    if await fs._exists(path):
        logger.debug("Blob already exists: %s", hash_hex)
        return hash_hex

    await fs._makedirs(_base_path, exist_ok=True)
    await fs._pipe_file(path, data)

    logger.debug("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex
