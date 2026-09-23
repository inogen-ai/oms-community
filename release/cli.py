"""Local promotion entrypoint. It never rebuilds, uploads or deploys a release."""
import argparse
import json
from pathlib import Path

from release.artefacts import Candidate, promote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--verification-keys", type=Path, required=True)
    parser.add_argument("--private-commit", required=True)
    parser.add_argument("--releases", type=Path, required=True)
    args = parser.parse_args()
    candidate = Candidate.model_validate_json((args.candidate / "candidate.json").read_bytes())
    target = promote(candidate, args.candidate, args.releases,
                     approval=args.approval.read_text().strip(),
                     public_keys=json.loads(args.verification_keys.read_text()),
                     private_commit=args.private_commit)
    print(target)


if __name__ == "__main__":
    main()
