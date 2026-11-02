import hashlib
import os
from pathlib import Path
from uuid import uuid4


class Storage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("无效的资产路径")
        return path

    def write(self, relative: str, data: str | bytes, kind: str, mime: str):
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            temp.write_bytes(payload)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "path": relative,
            "kind": kind,
            "mime_type": mime,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
