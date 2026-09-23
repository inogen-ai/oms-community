# Contributing

Every contributor must accept the [Contributor Licence Agreement](CLA.md)
before a maintainer can merge their first pull request. The agreement lets
INOGEN AI UK LTD use contributions in OMS Community and in its paid editions.
You keep the copyright in your work. OMS Community itself is source-available
under SUL-1.0; see [licensing](LICENSING.md).

Keep public runtime code independent of proprietary packages. New public
features must work from the built wheel without access to a source checkout,
model clients or a licence service. Edition extensions attach through public
ports and service bundles.

Run the relevant Python contracts and the Community frontend tests. Changes to
storage must preserve tenant boundaries, lineage and idempotency in both memory
and Neo4j adapters. Changes to import or publication must preserve the bytes of
owned scripts and reference files.

Changes to exported files require review of the exact export manifest and built
archive allowlists. A candidate is promoted only after its exact bytes pass the
supported private product's compatibility job. See [releasing](docs/releasing.md).

Do not contribute customer data, access tokens or private regression corpora.
Keep example data synthetic and small enough to understand without a service.
