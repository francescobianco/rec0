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
    domain: str                                    # site, or app name for desktop apps
    titles: tuple[str, ...] = field(default_factory=tuple)
    classes: tuple[str, ...] = ()                  # desktop apps: window classes (WM_CLASS)

    def matches(self, title: str, wm_class: str = "") -> bool:
        if self.classes:
            # Whole class names (or their prefix: "thunderbird_thunderbird"), never a
            # substring: "wire" must not catch Wireshark.
            names = wm_class.casefold().split()
            # Wayland and Flatpak: the class is an app id; its last part names the app
            # ("org.mozilla.Thunderbird", "com.slack.Slack").
            names += [n.rsplit(".", 1)[1] for n in names if "." in n.strip(".")]
            return any(n == c or n.startswith((c + "_", c + "-", c + ".")) or (" " in c and c in wm_class.casefold())
                       for c in (x.casefold() for x in self.classes) for n in names)
        t = title.casefold()
        return self.domain.casefold() in t or any(k.casefold() in t for k in self.titles)


def app(name: str, *classes: str) -> Rule:
    """A desktop application recognised by its window class, whatever it shows."""
    return Rule(name, (), classes)


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
    # Chat and messaging
    Rule("web.whatsapp.com", ("WhatsApp",)),
    Rule("web.telegram.org", ("Telegram",)),
    Rule("messenger.com", ("Messenger",)),
    Rule("facebook.com/messages", ("Chats | Facebook", "Messenger | Facebook")),
    Rule("instagram.com/direct", ("Instagram • Chats", "Inbox • Direct", "• Direct")),
    Rule("app.slack.com", ("Slack",)),
    Rule("discord.com", ("Discord",)),
    Rule("teams.microsoft.com", ("Microsoft Teams",)),
    Rule("signal.org", ("Signal",)),
    Rule("chat.google.com", ("Google Chat",)),
    Rule("messages.google.com", ("Google Messages", "Messages for web")),
    Rule("linkedin.com/messaging", ("Messaging | LinkedIn",)),
    Rule("x.com/messages", ("Messages / X", "Direct Messages")),
    Rule("web.skype.com", ("Skype",)),
    Rule("app.element.io", ("Element",)),
    Rule("web.wechat.com", ("WeChat",)),
    Rule("line.me", ("LINE",)),
    Rule("web.threema.ch", ("Threema Web",)),
    Rule("app.wire.com", ("Wire",)),
    Rule("chat.reddit.com", ("Reddit Chat",)),
    Rule("web.snapchat.com", ("Snapchat",)),
    Rule("viber.com", ("Viber",)),
    Rule("mattermost.com", ("Mattermost",)),
    Rule("rocket.chat", ("Rocket.Chat",)),
    Rule("zulipchat.com", ("Zulip",)),
    # Desktop messaging apps (any window of theirs, whatever the title)
    app("teams", "teams-for-linux", "microsoft teams", "msteams"),
    app("skype", "skypeforlinux", "skype"),
    app("slack", "slack"),
    app("discord", "discord", "vesktop", "webcord"),
    app("telegram", "telegramdesktop", "telegram-desktop", "org.telegram.desktop"),
    app("signal", "signal", "signal desktop"),
    app("whatsapp", "whatsapp-for-linux", "whatsapp", "zapzap", "whatsie", "wasistlos"),
    app("element", "element"),
    app("zoom", "zoom"),
    app("viber", "viber"),
    app("wire", "wire"),
    app("mattermost", "mattermost"),
    app("rocketchat", "rocket.chat"),
    app("zulip", "zulip"),
    app("caprine", "caprine"),
    app("ferdium", "ferdium", "franz", "rambox", "station"),
    app("fractal", "org.gnome.fractal", "fractal"),
    app("polari", "org.gnome.polari"),
    # Desktop mail clients
    app("thunderbird", "thunderbird", "mail"),
    app("evolution", "evolution", "org.gnome.evolution"),
    app("geary", "geary", "org.gnome.geary"),
    app("mailspring", "mailspring"),
    app("betterbird", "betterbird"),
    # Password managers
    app("keepassxc", "keepassxc", "org.keepassxc.keepassxc"),
    app("bitwarden", "bitwarden"),
    app("1password", "1password"),
    app("seahorse", "seahorse", "org.gnome.seahorse.application"),
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


def check(rules: tuple[Rule, ...], title: str, wm_class: str = "") -> Rule | None:
    """The rule a window falls under, or None if it can be recorded."""
    return next((r for r in rules if r.matches(title, wm_class)), None)
