"""The link page and message pages, the same text and look as relay/src/page.ts. Every client
string is escaped."""

from __future__ import annotations

import html

STYLE = """
:root {
  --bg: #FFFFFF; --bg-side: #F1F3F6; --surface: #FFFFFF; --border: #E3E6EB;
  --text: #1C1E21; --text-2: #65676B; --accent: #1D63ED; --accent-soft: #D9E4FA;
  --wait: #E8A317; --danger: #D93025;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0F1115; --bg-side: #16191F; --surface: #1B1F26; --border: #2A2F38;
    --text: #E8EAED; --text-2: #A0A6B0; --accent: #4C8DFF; --accent-soft: #1E2B45;
    --wait: #F2B640; --danger: #F07167;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; background: var(--bg-side); color: var(--text);
  font: 15px/1.5 Inter, "Optimistic Text", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  display: flex; align-items: center; justify-content: center; padding: 48px 16px;
}
main { width: 100%; max-width: 480px; }
.brand { margin-bottom: 24px; }
.brand h1 { margin: 0; font-size: 32px; font-weight: 600; letter-spacing: -0.01em; }
.brand p { margin: 4px 0 0; color: var(--text-2); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 24px; }
.card h2 { margin: 0 0 8px; font-size: 20px; font-weight: 600; }
.muted { color: var(--text-2); font-size: 13px; margin: 0 0 16px; }
.note { margin: 0 0 16px; padding: 8px 16px; border-radius: 8px; background: var(--bg-side); font-size: 13px; }
.note.warn { border-left: 3px solid var(--wait); }
label { display: block; font-weight: 600; margin: 16px 0 8px; }
input[name=code] {
  width: 100%; padding: 12px 16px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); text-transform: uppercase;
  font: 600 24px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: 0.2em;
}
input[name=code]:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
.muted.help { margin: 8px 0 0; }
fieldset { border: 0; padding: 0; margin: 16px 0 0; }
legend { font-weight: 600; margin: 0 0 8px; padding: 0; }
label.choice {
  display: flex; gap: 12px; align-items: flex-start; font-weight: 400; margin: 0 0 8px;
  padding: 12px 16px; border: 1px solid var(--border); border-radius: 8px; cursor: pointer;
}
label.choice:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); }
label.choice input { margin: 4px 0 0; accent-color: var(--accent); }
label.choice strong { display: block; font-weight: 600; }
label.choice span { color: var(--text-2); font-size: 13px; }
.error { color: var(--danger); margin: 8px 0 0; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 24px; }
button { border: 0; border-radius: 999px; padding: 10px 20px; font: inherit; font-weight: 600; cursor: pointer; }
button.primary { background: var(--accent); color: #FFFFFF; }
button.secondary { background: var(--bg-side); color: var(--text); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""

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
