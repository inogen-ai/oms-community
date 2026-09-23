"""The one place a deployed process builds its Neo4j driver, and the one
place it quietens the notification the server sends about nothing.

Two halves of the same job, because either alone leaves the logs unreadable
on some deployment.

`build_driver` asks the SERVER not to send UNRECOGNIZED notifications. That
classification carries Neo4j's "property key does not exist" hint (GQL status
01N52), which fires on every poll of a property no node has set yet -
`held_reason` before the first held transaction, for example. Deprecation,
performance and hint notifications still come through.

`quieten_unrecognised_notifications` is the client-side half, and exists
because the server-side request is only honoured by a Neo4j new enough to
support classification filtering. On an older one the notifications arrive
regardless, and the driver relays each at WARNING through the
`neo4j.notifications` logger - one per poll, which for a worker on a
30-second loop is a log nobody reads. Filtering by classification rather than
silencing the logger keeps the notifications an operator should act on: a
deprecation names a query about to stop working, a performance hint names a
missing index.
"""
from __future__ import annotations

import logging

from neo4j import Driver, GraphDatabase

# The driver's own logger for relayed server notifications
# (neo4j._sync.work.result).
NOTIFICATION_LOGGER = "neo4j.notifications"

# Neo4j's classification for "I did not recognise this name" - a missing
# property key, label or relationship type. Benign against an evolving
# schema, and the only classification worth dropping wholesale.
UNRECOGNISED = "UNRECOGNIZED"


class UnrecognisedNotificationFilter(logging.Filter):
    """Drops UNRECOGNIZED notification records and passes everything else.

    Reaches into the log record's args because that is where the driver puts
    the notification: `record.args[0]` is a `NotificationPrinter` wrapping the
    `GqlStatusObject`. Any record that does not have that shape is passed
    through untouched - a driver upgrade that changes it should cost a noisy
    log, never a swallowed warning.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or not args:
            return True
        notification = getattr(args[0], "notification", None)
        classification = getattr(notification, "classification", None)
        # `NotificationClassification` is a str enum, so `==` against the
        # plain string holds for both the enum and a raw value.
        return classification != UNRECOGNISED


def quieten_unrecognised_notifications() -> None:
    """Install the filter once. Idempotent: both long-running processes call
    it, and a re-run of a process's logging setup must not stack duplicates."""
    log = logging.getLogger(NOTIFICATION_LOGGER)
    if any(isinstance(f, UnrecognisedNotificationFilter) for f in log.filters):
        return
    log.addFilter(UnrecognisedNotificationFilter())


def build_driver(uri: str, user: str, password: str) -> Driver:
    # The client-side half is installed here rather than left to each
    # entrypoint's logging setup: every process that builds a driver is a
    # process that will receive these notifications, so the two belong
    # together and cannot be wired in one place and forgotten in another.
    quieten_unrecognised_notifications()
    return GraphDatabase.driver(
        uri, auth=(user, password),
        notifications_disabled_classifications=[UNRECOGNISED])
