# Quizify

**Turn quiz documents in any format into clean, structured data, then run them as proctored online exams.**

Quizify reads quizzes the way people actually write them: Word docs, PDFs, spreadsheets, web pages, plain text, even photos. It works out the numbering style, options, and answer key, then exports everything to **Excel, CSV, or JSON**. An admin panel, question bank, student portal, and live-proctored exam mode make it a small, self-hosted assessment platform.

---

## Features

- **Reads many formats.** `.docx`, `.doc`, `.pdf`, `.xlsx`, `.xls`, `.csv`, `.tsv`, `.json`, `.html`, `.xml`, `.rtf`, plain text, and images (OCR through Tesseract).
- **Adapts to each document.** It profiles the document's own conventions (question numbering, option labels, answer markers) instead of expecting one fixed template.
- **Detects question types.** MCQ, multi-select, True/False, numeric, fill-in-the-blank, short answer, and matching.
- **Optional AI assist with failover.** You can plug in OpenRouter, Groq, Gemini, Cerebras, Mistral, Anthropic, DeepSeek, Together, any OpenAI-compatible endpoint, or a local Ollama model. If one provider fails, the next is tried. The app works fully offline when no provider is set.
- **Exports** to Excel, CSV, or JSON.
- **Admin panel** (`/admin`): users, roles, settings, AI providers, conversion jobs, question bank, corrections, and audit log.
- **Online exams.** Build exams from parsed quizzes or the question bank, share them by link or assign them to students, schedule them, and grade objective questions automatically.
- **Student portal** (`/student`): a separate login where students see only the exams assigned to them.
- **Live proctoring.** Webcam snapshots, plus WebRTC live video over WebSockets.

## Tech stack

Python 3.10+ · Flask · SQLite · flask-sock (WebSockets) · openpyxl · python-docx · pdfplumber

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/satendragoswamii/Quizify.git
cd Quizify

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure (optional; every setting has a sensible default)
cp .env.example .env
# edit .env and add any API keys you want to use

# 5. Run
python app.py
```

Open **http://127.0.0.1:5000**.

### First-run setup

1. Visit **http://127.0.0.1:5000/admin**. On first run you'll be asked to create the administrator account.
2. The SQLite database (`quizify.db`) is created automatically. It is git-ignored, so each install starts with its own empty database.
3. Optionally add AI provider keys under **Admin → Providers**, or in `.env`.

---

## Configuration

Settings come from three layers, applied in this order:
**admin panel (stored in DB) → environment / `.env` → built-in default**.

A setting saved in the admin panel takes effect immediately, with no restart.

| Variable | Purpose | Default |
|---|---|---|
| `QUIZ_SECRET_KEY` | Flask session signing key. **Set this in production.** | auto-generated |
| `QUIZ_DB_PATH` | Location of the SQLite database | `./quizify.db` |
| `QUIZ_SECURE_COOKIES` | Send cookies only over HTTPS | `false` |
| `QUIZ_SESSION_HOURS` | Login session length | `12` |
| `QUIZ_MAX_UPLOAD_MB` | Upload size limit | `25` |
| `QUIZ_AI_PROVIDERS` | Provider order for failover | all configured |
| `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, … | AI provider keys | none |
| `QUIZ_ENABLE_OLLAMA`, `OLLAMA_HOST` | Local AI through Ollama | off |
| `QUIZ_PROXY_HOPS` | Number of trusted reverse proxies | `0` |
| `MESSENGERX_*` | Optional in-app help chat companion | none |

[`.env.example`](.env.example) lists every setting.

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Production deployment

The built-in server is for development only. Use a WSGI server:

```bash
pip install gunicorn
gunicorn --workers 4 --threads 4 --timeout 300 --bind 0.0.0.0:8000 wsgi:application
```

On Windows, use Waitress:

```bash
pip install waitress
waitress-serve --threads 8 --listen 0.0.0.0:8000 wsgi:application
```

Notes:
- Keep `--timeout` high. AI-assisted conversions can take minutes.
- Behind nginx or Caddy, set `QUIZ_PROXY_HOPS=1` and `QUIZ_SECURE_COOKIES=true`.
- Set `FLASK_DEBUG=0` if you run `app.py` directly anywhere public.
- If a deployment misbehaves, run `python diagnose.py` (or `python diagnose.py /some/path`) on the server for a full environment report and the real traceback.

---

## Image OCR (optional)

To read quizzes from images, install Tesseract and the Python bindings:

```bash
brew install tesseract              # macOS  (Ubuntu: sudo apt install tesseract-ocr)
pip install pytesseract pillow
```

---

## Running tests

The tests use a throwaway database and make no network calls.

```bash
python tests/test_quizify.py
python tests/test_admin.py
python tests/test_providers.py
```

---

## Project structure

```
Quizify/
├── app.py                 # HTTP layer: web UI + /api routes
├── wsgi.py                # Production WSGI entry point
├── diagnose.py            # Deployment diagnostics
├── requirements.txt
├── .env.example           # Configuration template (copy to .env)
├── quizify/
│   ├── extractors.py      # file / paste → text blocks
│   ├── engine.py          # style profiling + question segmentation
│   ├── analysis.py        # question types, answers, validation
│   ├── ai.py, providers.py# multi-provider AI assist with failover
│   ├── export.py          # xlsx / csv / json output
│   ├── bank.py            # question bank
│   ├── exams.py           # online exams + auto-grading
│   ├── student.py         # student portal blueprint
│   ├── signaling.py       # WebRTC signaling for live proctoring
│   └── admin/             # admin panel: auth, DB, routes
├── templates/             # Jinja2 templates (main, admin, student, exam)
├── static/                # CSS / JS
└── tests/
```

---

## Security notes

- `.env`, every `*.db` file, uploads, and deployment bundles are **git-ignored**. Never commit them.
- The admin panel stores provider API keys in the database. Treat `quizify.db` as a secret, and back it up privately.
- Always set a strong `QUIZ_SECRET_KEY` and serve over HTTPS in production.
