# OMS Community interface

This is an independent Next application. It imports only its own source and the versioned public `@inogen/oms-client` and `@inogen/oms-ui-core` packages. The existing application in `frontend/` remains a separate build.

From this directory:

```sh
npm ci
npm test
npm run build
OMS_COMMUNITY_API_URL=http://127.0.0.1:4317 npm run start
```

The build exports static HTML, CSS and JavaScript to `out/`. Point the Community API’s `OMS_UI_DIR` at that directory to serve the application and API together. The application reads `/oms-config.json` once at startup; `{ "api_url": "" }` uses the same origin. A separate static host can supply an explicit API URL in that file without changing the built JavaScript. The URL is public and must never contain a credential. Release builds should set `OMS_BUILD_ID` to the reviewed source commit; the local default is the deterministic package version identifier `oms-community-1.4.2`.

`npm run start` is an isolated local preview server bound to `127.0.0.1:4318`; its config endpoint reads `OMS_COMMUNITY_API_URL` and defaults to `http://127.0.0.1:4317`. Permit the exact browser origin `http://127.0.0.1:4318` in the API settings. `npm run dev` also uses API port 4317. Remote exposure belongs to the backend’s explicit local-security policy.

Public packages use local package dependencies during development. The checked-in `.npmrc` sets `install-links=true` so a fresh `npm ci` installs those packages and their runtime dependencies without relying on a parent application. After changing a shared package, run `npm install --install-links` to refresh its installed copy; remove `.next` if switching from the previous linked installation. Pack each with `npm pack --pack-destination <output-directory>` and pin the resulting tarball/version and integrity in downstream release manifests. No source from the original Enterprise app is included.

The Import page lists source revisions, missing rules and safety or polarity decisions separately, with controls naming the effect of each choice. Rules & constraints supports editing, retiring and restoring constraints created locally. Imported requirements retain their source ownership and must be changed in the original file. These local routes use the released client's `request` transport; the edition-specific API workflow stays outside the shared presentation components.

Run `npm run e2e` after a production build. Playwright starts a fresh server on 4318 and refuses to reuse any server. Its API requests are intercepted at 4317 with deterministic public fixtures; no database or existing service is contacted. The request allowlist rejects any endpoint outside the Community contract, and a controlled mutation checks the fence.

`npm run e2e:real` starts both the static UI and a real API using an installed core wheel. Set `OMS_CORE_PYTHON` to that environment’s Python interpreter; the default is the repository’s `.venv/bin/python`. The launcher rejects a source checkout import. This journey imports a ZIP, creates and reinforces a manually edited rule, rejects another correction, checks lineage and history, and publishes the original reference and script bytes without executing the script. HTTP requests pass through a Community route fence without mocked responses. All state lives in a fresh temporary directory, and Playwright stops only its own servers. It uses the memory graph adapter; the Python golden tests own Neo4j persistence, restart, atomicity and MCP parity.

The dependency audit and package/bundle scans are separate release checks. Neither a passing browser fixture nor a successful build proves the real database workflow by itself.

Skills offers upload in place using the same importer as the Import page. Documents share OMS Markdown and frontmatter rendering; source text remains available in a disclosure. Graph uses the shared 2D canvas and node vocabulary, with fit, zoom, layout, labels and keyboard node selection. Branding, fonts and theme match the paid OMS interface.

The library and inbox offer compact, filtered and paged views for hundreds of records. Their APIs still return complete arrays; this is browser presentation paging. Unfinished edits survive list/detail switches within the current page but are not persisted across reloads. The overview reads eight recent change summaries from `/api/skill-changes`, alongside pending corrections and source reviews. Skill history presents the most recent 50 saved versions with recorded attribution and expandable details.
