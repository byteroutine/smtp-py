# smtp-py

Send email from Python in one line. A tiny batteries-included SMTP client with
rate limiting, automatic retries, and a local send log — plus a CLI and a
small REST API when you need them.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

```python
from smtppy import send_email

send_email(
    to="friend@example.com",
    subject="Hello from smtp-py",
    body="It works!",
)
```

That's it. Credentials come from your `.env` file automatically.

## Install

**Via pip:**

```bash
pip install bytesmtp
```

**Via git clone:**

```bash
git clone https://github.com/byteroutine/smtp-py.git
cd smtp-py
pip install -e .
```

(The distribution name on PyPI is `smtppy` because `smtp-py` was already
taken there. The import name is `smtppy`, the command is `smtp-py`.)

## Configure

Create a `.env` file next to your script (or named `.env.smtpserver` —
`.env` wins if both exist):

```bash
cp .env.example .env
```

```ini
SMTP_HOST=mail.example.com
SMTP_PORT=465
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=your-password-here
SMTP_FROM_EMAIL=you@example.com
SMTP_FROM_NAME=Your Name
SMTP_USE_TLS=false
SMTP_USE_SSL=true
```

smtppy reads the file from your current working directory automatically. No
config code needed. You can also configure in code:

```python
from smtppy import configure, send_email

configure(
    smtp_host="mail.example.com",
    smtp_username="you@example.com",
    smtp_password="secret",
    smtp_from_email="you@example.com",
)
send_email(to="a@example.com", subject="Hi", body="Hello")
```

## Usage

### Send one email

```python
from smtppy import send_email

result = send_email(
    to="friend@example.com",
    subject="Hello",
    body="Plain text body",
)
print(result)  # {"ok": True, "sent": 1, "failed": 0, "total": 1}
```

HTML, multiple recipients, or both:

```python
send_email(
    to=["a@example.com", "b@example.com"],
    subject="Hello",
    body="Fallback text",
    html="<h1>Hello</h1><p>Rich body</p>",
)
```

### Inside async code

```python
from smtppy import send_email_async

result = await send_email_async(to="a@example.com", subject="Hi", body="Hello")
```

### Send bulk

```python
from smtppy import send_bulk

def progress(done, total, recipient, ok):
    print(f"{done}/{total} {recipient} {'sent' if ok else 'failed'}")

report = send_bulk(
    ["a@example.com", "b@example.com"],
    subject="Newsletter",
    body="Hello everyone",
    on_progress=progress,
)
```

Every send is rate-limited (5/sec by default), retried with exponential
backoff on transient SMTP errors (1s, 2s, 4s, 8s), and logged to a local
SQLite database (`smtppy.db`).

### CLI

After installing, the `smtp-py` command is available:

```bash
smtp-py            # interactive menu (opens with an animated banner)
smtp-py send --to a@example.com --subject "Hi" --body "Hello"
smtp-py serve      # start the REST API on 127.0.0.1:8000
smtp-py stats      # send statistics
smtp-py recent --limit 20
smtp-py retry-failed
smtp-py diagnose   # check your .env configuration
```

The menu opens with a short neon radar animation, and the header plays a
quick shimmer sting each time it appears (Ctrl+C skips it any time).
Set `SMTPY_NO_BANNER=1` to disable all animation permanently.

Or without installing: `python -m smtppy` from a clone.

### Sending from the menu

Option 1 walks you through two steps:

1. **Recipients** — type them manually, or pick option 2 to auto-load
   `emails.txt` from the current folder (one address per line; see
   `emails.txt.example`).
2. **Content** — type the subject and body manually, or pick option 3 to
   auto-load `writemail.txt` from the current folder:

```ini
SUBJECT: Hello from CVBinder
BODY:
Hi there,

Lines under BODY: are sent as the plain-text version.

HTML:
<h1>Hi there,</h1>
<p>Everything under <b>HTML:</b> is sent as the HTML version.</p>
```

`writemail.txt` holds only the message content — recipients always come
from step 1, so one compose file works for any audience.

### REST API

```bash
smtp-py serve
```

`POST /api/send` with a bearer token (`API_BEARER_TOKEN` in your env file):

```bash
curl -X POST http://127.0.0.1:8000/api/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to": "a@example.com", "subject": "Hi", "text": "Hello"}'
```

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `SMTP_HOST` | *(required)* | SMTP server hostname |
| `SMTP_PORT` | `465` | SMTP port |
| `SMTP_USERNAME` | *(required)* | Login username (usually the full email) |
| `SMTP_PASSWORD` | *(required)* | Login password / app password |
| `SMTP_FROM_EMAIL` | *(required)* | From address |
| `SMTP_FROM_NAME` | | From display name |
| `SMTP_USE_TLS` | `false` | STARTTLS (port 587) |
| `SMTP_USE_SSL` | `true` | Implicit SSL (port 465) |
| `SMTP_TIMEOUT_SECONDS` | `30` | Connection timeout |
| `RATE_LIMIT_PER_SECOND` | `5.0` | Max sends per second |
| `RATE_LIMIT_BURST` | `5` | Bucket size |
| `MAX_RETRIES` | `4` | Retries on transient errors |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | First retry delay (doubles each time) |
| `DATABASE_PATH` | `smtppy.db` | SQLite send log |
| `API_BEARER_TOKEN` | | Required for `serve` / API access (min 32 chars) |

## Development

```bash
git clone https://github.com/byteroutine/smtp-py.git
cd smtp-py
pip install -e ".[dev]" 2>/dev/null || pip install -e .
pip install pytest
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE). Use it anywhere, no strings attached.
