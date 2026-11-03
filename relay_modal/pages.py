"""The link page and message pages, the same text and look as relay/src/page.ts. Every client
string is escaped."""

from __future__ import annotations

import html
from pathlib import Path

STYLE = (Path(__file__).with_name("page_style.css")).read_text()

CSP = "; ".join(
    [
        "default-src 'none'",
        "style-src 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        # Chrome checks the redirect after the form posts against form-action.
        "form-action 'self' https: http://localhost:* http://127.0.0.1:*",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ]
)


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def shell(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap">
<style>{STYLE}</style>
</head>
<body>
<main>
<div class="brand"><h1>Muse Privacy</h1><p>Private workspace on this device</p></div>
{content}
</main>
</body>
</html>"""


def access_from(value) -> str:
    return "read" if value == "read" else "full"


def _access_choice(access: str) -> str:
    def option(value: str, title: str, text: str) -> str:
        checked = " checked" if access == value else ""
        return (
            f'<label class="choice"><input type="radio" name="access" value="{value}"{checked}>'
            f"<span><strong>{title}</strong>{text}</span></label>"
        )

    return (
        "<fieldset>\n<legend>What Muse can do on your device</legend>\n"
        + option("full", "Full access", "List, search and ask, and also start tasks, save files and start dreaming. Every result still passes the privacy checks.")
        + "\n"
        + option("read", "Read only", "List, search and ask only. Muse cannot start tasks, save files or start dreaming. You can link again later to change this.")
        + "\n</fieldset>"
    )


def pairing_page(
    *,
    client_name: str,
    client_domain: str | None,
    redirect_host: str,
    loopback: bool,
    handle: str,
    action: str,
    error: str | None = None,
    access: str = "full",
) -> str:
    origin = (
        f"Published by <strong>{esc(client_domain)}</strong>."
        if client_domain
        else "This app named itself. Muse Privacy did not check the name."
    )
    warn = (
        '<p class="note warn">Access goes to an app on this computer. Continue only if you just started linking from it.</p>'
        if loopback
        else ""
    )
    problem = f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
    invalid = ' aria-invalid="true"' if error else ""
    return shell(
        "Link your device",
        f"""<section class="card">
<h2>Link {esc(client_name)} to your device</h2>
<p class="muted">{origin} Access will go to <strong>{esc(redirect_host)}</strong>.</p>
{warn}
<p class="note">On your device, make a pairing code, then type it here. A code works once and lasts 10 minutes.</p>
<form method="post" action="{esc(action)}">
<input type="hidden" name="handle" value="{esc(handle)}">
<label for="code">Pairing code</label>
<input id="code" name="code" required maxlength="11" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" placeholder="ABCD2345" aria-describedby="code-help"{invalid} autofocus>
<p class="muted help" id="code-help">8 letters and numbers, shown on your device.</p>
{problem}
{_access_choice(access)}
<div class="actions">
<button class="primary" type="submit" name="decision" value="link">Link this device</button>
<button class="secondary" type="submit" name="decision" value="cancel" formnovalidate>Cancel</button>
</div>
</form>
</section>""",
    )


def message_page(title: str, message: str) -> str:
    return shell(title, f'<section class="card"><h2>{esc(title)}</h2><p class="muted">{esc(message)}</p></section>')
