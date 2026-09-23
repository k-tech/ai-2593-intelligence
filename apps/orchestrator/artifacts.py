from __future__ import annotations

from pathlib import Path
import json

from .domain import canonical_json, sha256_text


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put_text(self, kind: str, text: str, suffix: str = ".txt") -> tuple[str, Path]:
        digest = sha256_text(text)
        path = self.root / kind / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
        return digest, path

    def put_json(self, kind: str, value: object) -> tuple[str, Path]:
        return self.put_text(kind, canonical_json(value) + "\n", ".json")

    def read_json(self, path: str | Path) -> object:
        return json.loads(Path(path).read_text(encoding="utf-8"))
