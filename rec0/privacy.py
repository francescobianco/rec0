"""Privacy list: pages that must never be recorded, even inside a shared window.

X11 exposes window titles, not the URL of the active browser tab, so each
domain is recognised by the domain itself or by words its pages put in the
title (e.g. "Gmail"). Projects can add domains (`block`) or lift built-in
ones (`allow`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    domain: str
    titles: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, title: str) -> bool:
        t = title.casefold()
        return self.domain.casefold() in t or any(k.casefold() in t for k in self.titles)


BUILTIN: tuple[Rule, ...] = (
    # Mail
    Rule("gmail.com", ("Gmail", "@gmail.com", "mail.google.com")),
    Rule("outlook.live.com", ("Outlook", "Mail - ")),
    Rule("outlook.office.com", ("Outlook",)),
    Rule("mail.yahoo.com", ("Yahoo Mail",)),
    Rule("mail.proton.me", ("Proton Mail",)),
    Rule("mail.libero.it", ("Libero Mail", "@libero.it")),
    Rule("webmail.aruba.it", ("Webmail Aruba",)),
    Rule("icloud.com", ("iCloud Mail",)),
    Rule("fastmail.com", ("Fastmail",)),
    # Chat
    Rule("web.whatsapp.com", ("WhatsApp",)),
    Rule("web.telegram.org", ("Telegram",)),
    Rule("messenger.com", ("Messenger",)),
    Rule("app.slack.com", ("Slack",)),
    Rule("discord.com", ("Discord",)),
    Rule("teams.microsoft.com", ("Microsoft Teams",)),
    Rule("signal.org", ("Signal",)),
    # Passwords and money
    Rule("passwords.google.com", ("Google Password Manager", "Gestore delle password")),
    Rule("vault.bitwarden.com", ("Bitwarden",)),
    Rule("my.1password.com", ("1Password",)),
    Rule("lastpass.com", ("LastPass",)),
    Rule("paypal.com", ("PayPal",)),
    Rule("revolut.com", ("Revolut",)),
    Rule("n26.com", ("N26",)),
)


def build(allow: list[str], block: list[Rule]) -> tuple[Rule, ...]:
    allowed = [a.casefold().strip(".") for a in allow]
    # "google.com" also lifts "passwords.google.com".
    rules = [r for r in BUILTIN
             if not any(r.domain == a or r.domain.endswith("." + a) for a in allowed)]
    return tuple(rules + block)


def check(rules: tuple[Rule, ...], title: str) -> Rule | None:
    """The rule a window title falls under, or None if it can be recorded."""
    return next((r for r in rules if r.matches(title)), None)
