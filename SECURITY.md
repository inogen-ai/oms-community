# Security

Community is a local, single-operator workspace. Its default listener and the
provided Compose host ports bind to loopback. A deliberate non-loopback server
bind requires `OMS_ACKNOWLEDGE_NETWORK_EXPOSURE=1`; that acknowledgement does
not add multi-user authentication or network access control.

The local stack has no public Neo4j port. Do not commit `.env.local`, payload
directories, database dumps or generated credentials. Keep untrusted browser
origins out of `OMS_ALLOWED_ORIGINS`.

Import and contribution safety checks are part of the public product. Keep
archive traversal, size and symbolic-link checks enabled. An injection or
sanitisation warning is a held item, not a successful publication.

A Community server refuses writable use of a graph marked as managed by a
different edition. Use an explicit export into a new Community workspace when
moving data back; do not edit schema metadata to bypass that refusal.

Report a vulnerability privately to the repository maintainers through the
repository's security advisory channel once it is enabled. Do not post secrets,
customer data or a working exploit against a live deployment in a public issue.
