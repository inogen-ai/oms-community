from pathlib import Path
import re

from oms.ingestion.schema import ExecutionContext


class FilePayloadStore:
    """Persists sanitised payloads. Raw payloads are never written here."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, transaction_id: str, *, quarantine: bool = False) -> Path:
        if not isinstance(transaction_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", transaction_id):
            raise ValueError("invalid transaction identifier")
        parent = self._root / "_flagged_errors" if quarantine else self._root
        path = parent / (transaction_id + (".txt" if quarantine else ".json"))
        if path.is_symlink() or parent.is_symlink() or not path.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("payload path escapes its store")
        return path

    def put(self, transaction_id: str, ctx: ExecutionContext) -> str:
        ref = f"{transaction_id}.json"
        self._path(transaction_id).write_text(ctx.model_dump_json(indent=2), encoding="utf-8")
        return ref

    def get(self, transaction_id: str) -> ExecutionContext | None:
        path = self._path(transaction_id)
        if not path.exists():
            return None
        return ExecutionContext.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, transaction_id: str) -> bool:
        """Remove one sanitised payload. True when a file went, False when
        there was nothing to remove.

        The absent case is not an error and must not read as one. A payload can
        legitimately be gone already - purged by an earlier sweep, or lost to a
        payload root under /tmp - and a retention run that raised on the second
        pass would be unrunnable on a schedule.

        Deliberately narrow: this deletes the sanitised context and nothing
        else. The Transaction node, its summary and its lineage stay, because
        they are what proves a rule came from somewhere. Erasing the person
        behind them is a different operation with a different authority.
        """
        path = self._path(transaction_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def delete_quarantine(self, transaction_id: str) -> bool:
        """Remove one quarantine file. True when a file went.

        Separate from `delete` because the two hold different things and a
        caller may legitimately want one without the other. A purge wants
        both: the quarantine file is the only text under this root that was
        never scrubbed at all, so an erasure that took the sanitised payload
        and left this behind would have erased the safer copy.
        """
        path = self._path(transaction_id, quarantine=True)
        if not path.exists():
            return False
        path.unlink()
        return True

    def quarantine(self, transaction_id: str, raw: str) -> str:
        rel = f"_flagged_errors/{transaction_id}.txt"
        path = self._path(transaction_id, quarantine=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return rel
