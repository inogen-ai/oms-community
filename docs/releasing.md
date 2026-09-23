# Release validation

The canonical shared source belongs in this public repository. The private
product consumes the exact Python wheel and npm packages built here.

## Initial extraction from the private working repository

Run these commands in the isolated implementation worktree:

    python -m release.community manifest --out release/community-files.json
    python -m release.community export --manifest release/community-files.json --out dist/public-export
    python -m release.community verify --fresh dist/public-export

Review the explicit source-to-destination list before export. The exporter
rejects additional files, links, private implementation imports, likely
credentials and protected frontend routes. It copies only the approved public
roots and selected public tests/tooling, creates a new repository with one
initial root commit, and checks every Git object. It never copies private Git
history, creates a remote, or pushes code. Reusing an output path succeeds only
when every reviewed byte still matches; changed input requires a new path.

The first remote creation and publication remain owner actions. Confirm the
licence and notices, the Contributor Licence Agreement and its pull request
check, trademark wording and security contact before public launch. The local export does not claim those approvals.

## Build the candidate once

From the public repository, with Python 3.12, uv and Node 22 available:

    uv run --no-project --with pydantic --with 'PyJWT[crypto]' --with packaging python -m release.community build --out dist/candidate --report dist/release-report.json

This command checks the committed manifest and all reachable Git objects,
copies the reviewed source to a clean build directory, builds the wheel and
source distribution, packs both shared frontend packages and the static app,
and scans all archives. Package source must match the reviewed source bytes.
The command independently installs the wheel and source distribution outside
the checkout using the public lock's dependency versions, imports every public
module and exercises the installed CLI.

The report records the build toolchain, archive allowlists, artefact hashes, the public commit,
installed dependency versions and licence metadata, a CycloneDX SBOM, and any
unknown licence metadata. Review the dependency licences and unknown entries.
A metadata scan is evidence for that review, not a legal compatibility opinion.
The development flags that omit frontend or install checks mark those checks
false and are unsuitable for publication.

The build refuses to overwrite candidate bytes. Preserve the candidate
directory, its report and the successful producer CI run. Public CI publishes
only a short-lived candidate artefact, named "shared-candidate"; it does not
publish registry packages or release tags.

Compressed npm bytes can differ across Node/npm releases even when their
unpacked source is identical. Update the private lock from the produced
candidate tarballs, then preserve those exact files through compatibility and
promotion.

## Subsequent public changes

After extraction, edit shared source here first. Stage new files, review the
changes, then update the file manifest before committing:

    git add src frontend tests docs
    python -m release.community refresh .
    git add PUBLIC_MANIFEST.json
    git commit

The verifier permits subsequent public commits but scans every reachable
historical tree and rejects borrowed or unreachable history. It requires the
current committed tree to match PUBLIC_MANIFEST.json. Build inputs come from
that committed tree, so ignored environment files and stale local build output
cannot become candidate inputs.

## Private compatibility and exact-byte promotion

The private repository's protected compatibility workflow takes a selected
successful producer run and the reviewed candidate-manifest SHA-256. Its
trusted configuration includes the exact archive allowlists and real suite
paths. When package contents change, update and review those allowlists in the
private repository before running the gate.

The gate verifies producer repository, workflow, success and public commit;
installs the public wheel alone for Community tests; and installs that same
wheel with the paid wheel for Pro and Enterprise tests. Real migration tests
use a disposable loopback Neo4j in the isolated container. No host Docker
socket, external network, application secrets or signing key enters that
container.

The private frontend must pin the exact candidate npm tarballs and SHA-512
integrities in its committed lock. A separate preparation step copies committed
private frontend source and installs dependencies with npm lifecycle scripts
disabled and no application secrets. The isolated runner verifies the prepared
source, candidate digest and private commit, then runs actual private unit
tests, typechecking and production bundling using those installed packages.

Only after all suites pass and the container has stopped does trusted CI sign
an approval bound to the candidate digest, public commit, private commit and
tested/inspected artefact hashes. Configure protected maintainers, environment
reviewers, the exact producer workflow path, narrow shared-artefact read
credentials and signing/public keys before enabling this workflow. Public pull
request code never receives private repository credentials.

Promotion must verify that signed approval and copy the already tested
candidate bytes. Do not rebuild a package during promotion. The existing
release.cli promote command performs immutable local promotion; remote package
or container publication needs the destination credentials and owner-approved
release process. Keep the report and SBOM alongside the promoted artefacts.

## Local runtime smoke test

Run ./run-local.sh in the exported repository. It builds the public-only images
and starts an isolated Neo4j, API and static UI. Host ports default to 4317 and
4318 on loopback. Import a synthetic skill tree, submit a correction, make a
manual decision, inspect history and publish; verify imported scripts and
reference files retain their bytes.

The public browser tests can drive the same workflow with a freshly installed
core wheel. Set OMS_CORE_PYTHON to that environment's Python, run npm ci in
frontend/community, build the app, and run npm run e2e:real. They create their
own temporary workspace and refuse to reuse another server on the test ports.
