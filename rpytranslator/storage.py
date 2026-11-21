"""Publish a complete settings file, checkpoint, or report in one replacement."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                            delete=False) as f:
        pending = Path(f.name)
    try:
        pending.write_bytes(text.encode(encoding))
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
