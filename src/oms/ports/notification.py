"""Delivering word that something happened, outside the console.

`docs/console-administration.md` names notifications explicitly as "left out":
"infrastructure with delivery, retry and configuration surface of its own;
nothing in this slice would be the hard part of it." That is a fair read of
picking an email provider - templates, bounce handling, a sender identity to
authenticate - but it is not a fair read of the whole feature.

A webhook needs none of that. It is one URL and one POST, and every downstream
system an operator would actually want (Slack, PagerDuty, a Zapier chain, their
own listener) speaks it natively. So this is the vendor-neutral half: no
provider chosen, no secret beyond a URL the tenant supplies themselves. Email
stays a real follow-up behind this port, not a decision this port makes for it.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from oms.activity.models import AdminEvent


@runtime_checkable
class Notifier(Protocol):
    def notify(self, event: AdminEvent) -> None:
        """Best-effort delivery of one admin event.

        Never raises. The event has already been recorded in the graph by the
        time a notifier sees it - that write is the one that must not fail -
        so a delivery problem (an unreachable webhook, a DNS failure) is this
        protocol's problem to swallow, not the caller's to handle. An adapter
        that wants the failure visible logs it and reports through its own
        health surface, the way `injection.screen` never raising and `_screen`
        catching around it anyway is belt and braces for the same reason.
        """
        ...
