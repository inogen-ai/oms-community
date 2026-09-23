"""Digest-bound candidates and exact-byte promotion with trusted approval."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile

import jwt
from packaging.utils import canonicalize_name, parse_wheel_filename, InvalidWheelFilename
from pydantic import BaseModel, ConfigDict, Field, field_validator

SHA = r"^[0-9a-f]{64}$"
COMMIT = r"^[0-9a-f]{40}$"
SUITES = frozenset({"community", "pro", "enterprise", "migrations", "frontend", "package_contents"})
APPROVAL_TYPE = "oms-compatibility+jwt"


class ReleaseError(ValueError):
    pass


class Artefact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str
    package: str = Field(pattern=r"^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]*)?$")
    sha256: str = Field(pattern=SHA)
    size: int = Field(gt=0)

    @field_validator("filename")
    @classmethod
    def simple_filename(cls, value):
        if (not value or value in (".", "..") or PurePosixPath(value).name != value
                or "\\" in value or not re.fullmatch(r"[A-Za-z0-9_.+-]+", value)):
            raise ValueError("artefact filename must be a single safe path component")
        return value


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = Field(ge=1, le=1)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]*)?$")
    shared_commit: str = Field(pattern=COMMIT)
    shared_schema: int = Field(ge=1)
    artefacts: list[Artefact] = Field(min_length=1)

    def canonical(self) -> bytes:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_candidate(candidate: Candidate, directory: Path) -> None:
    filenames = [item.filename for item in candidate.artefacts]
    if len(set(filenames)) != len(filenames):
        raise ReleaseError("duplicate artefact filenames")
    if any(item.version != candidate.version for item in candidate.artefacts):
        raise ReleaseError("candidate version differs from an artefact version")
    actual = {p.name for p in directory.iterdir() if p.name != "candidate.json"}
    if actual != set(filenames):
        raise ReleaseError("candidate directory contains missing or unapproved files")
    for item in candidate.artefacts:
        path = directory / item.filename
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != item.size
                or digest(path) != item.sha256):
            raise ReleaseError(f"artefact digest/size mismatch: {item.filename}")
        if path.suffix == ".whl":
            from email.parser import BytesParser
            try:
                wheel_name, wheel_version, _, _ = parse_wheel_filename(item.filename)
            except InvalidWheelFilename as exc:
                raise ReleaseError("invalid wheel filename") from exc
            if wheel_name != canonicalize_name(item.package) or str(wheel_version) != item.version:
                raise ReleaseError("wheel filename does not match the candidate")
            with zipfile.ZipFile(path) as wheel:
                metadata = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
                if len(metadata) != 1:
                    raise ReleaseError("wheel must contain exactly one package metadata document")
                expected_directory = f"{item.package.replace('-', '_')}-{item.version}.dist-info"
                if metadata[0] != expected_directory + "/METADATA":
                    raise ReleaseError("wheel metadata directory does not match the candidate")
                fields = BytesParser().parsebytes(wheel.read(metadata[0]))
                normalise = lambda s: re.sub(r"[-_.]+", "-", s).lower()
                if (len(fields.get_all("Name", [])) != 1 or len(fields.get_all("Version", [])) != 1
                        or normalise(fields["Name"]) != normalise(item.package)
                        or fields["Version"] != item.version):
                    raise ReleaseError("wheel metadata does not match the candidate")


def verify_approval(document: str, public_keys: dict, candidate: Candidate, *,
                    private_commit: str, issuer: str = "oms-private-ci") -> dict:
    """Only a trusted signing job can authorise promotion, never test code."""
    if not re.fullmatch(COMMIT, private_commit):
        raise ReleaseError("an exact private commit is required")
    try:
        header = jwt.get_unverified_header(document)
        if (set(header) != {"alg", "kid", "typ"} or header["alg"] != "EdDSA"
                or header["typ"] != APPROVAL_TYPE or not isinstance(header["kid"], str)):
            raise ReleaseError("invalid compatibility approval header")
        claims = jwt.decode(document, public_keys[header["kid"]], algorithms=["EdDSA"],
                            issuer=issuer, audience="oms-shared-release",
                            options={"strict_aud": True, "require": ["exp", "iat", "nbf"]})
    except (jwt.PyJWTError, KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ReleaseError("invalid or expired compatibility approval") from exc
    if (claims.get("candidate_sha256") != candidate.digest()
            or claims.get("private_commit") != private_commit
            or claims.get("shared_commit") != candidate.shared_commit
            or claims.get("suites") != {suite: "passed" for suite in SUITES}
            or claims.get("installed_artefacts") != sorted(a.sha256 for a in candidate.artefacts
                                                          if a.filename.endswith(".whl"))
            or claims.get("inspected_artefacts", claims.get("installed_artefacts"))
                != sorted(a.sha256 for a in candidate.artefacts)):
        raise ReleaseError("compatibility approval does not match the tested release pair")
    return claims


def promote(candidate: Candidate, source: Path, releases: Path, *, approval: str,
            public_keys: dict, private_commit: str) -> Path:
    verify_candidate(candidate, source)
    verify_approval(approval, public_keys, candidate, private_commit=private_commit)
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / candidate.version
    # An exclusive lock serialises promoters. A crash leaves the lock behind;
    # an operator removes it only after confirming the promoter has stopped.
    lock = releases / f".{candidate.version}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ReleaseError("a promotion is already in progress") from exc
    os.close(fd)
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".candidate-", dir=releases))
        if target.exists():
            recorded = Candidate.model_validate_json((target / "candidate.json").read_bytes())
            if recorded.digest() != candidate.digest():
                raise ReleaseError("an immutable version already contains different bytes")
            verify_candidate(recorded, target)
            return target
        for artefact in candidate.artefacts:
            shutil.copyfile(source / artefact.filename, staging / artefact.filename)
        (staging / "candidate.json").write_bytes(candidate.canonical())
        verify_candidate(candidate, staging)
        # Atomic directory rename means an interrupted copy is never released.
        os.rename(staging, target)
        return target
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        lock.unlink()


def paid_release_manifest(candidate: Candidate, *, private_commit: str,
                          private_schema: int, required_shared_schema: int) -> dict:
    if not re.fullmatch(COMMIT, private_commit) or required_shared_schema > candidate.shared_schema:
        raise ReleaseError("private release requires an unavailable shared schema or commit")
    return {"schema_version": 1, "private_commit": private_commit,
            "private_schema": private_schema, "required_shared_schema": required_shared_schema,
            "shared_candidate_sha256": candidate.digest(),
            "shared_artefacts": [artefact.model_dump() for artefact in candidate.artefacts]}
