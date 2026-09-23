# OMS public client

`@inogen/oms-client` 1.2.0 contains the Community HTTP transport, API types and capability parser. It supports API contracts 1.0 and 1.1 with schema 1. It has no runtime dependencies and carries no credentials. The caller supplies the API base URL and may supply `fetch` for testing.

The default `requestPolicy: "local"` omits ambient cookies, rejects redirects and bypasses caches. An application that already manages its browser requests can explicitly choose `requestPolicy: "browser"` to preserve `RequestInit` and browser defaults. Its feature client supplies any authentication headers. Error status, server messages and HTTP/2 fallback diagnostics use the same `HttpError` in both modes.

Run `npm test` here. `npm pack` produces the versioned JavaScript/type artefact; it contains only the explicitly listed public source. A private consumer should install that exact tarball or registry version rather than copy this source.
