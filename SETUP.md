# Setup

Get the Governed Text-to-SQL agent running on your computer. Pick **one** of the two ways below.

You need:

- **Python 3.10 – 3.14**
- **PostgreSQL 15 or newer**: Docker, a local install, or a hosted one such as Supabase
- **A model**, optional: the app runs without one (offline mode), but you need one to ask questions in your own words

---

## Way 1: Docker (quickest)

Needs [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/aiuser789/blueprint-governed-text-to-sql.git
```

```bash
cd blueprint-governed-text-to-sql
```

```bash
docker compose up --build
```

Open **http://localhost:8000**. The database, the security rules and the sample retail data are set up for you. To use a model, see [step 4](#4-choose-a-model) below and restart Docker.

---

## Way 2: Python on your machine

### 1. Get the code

```bash
git clone https://github.com/aiuser789/blueprint-governed-text-to-sql.git
```

```bash
cd blueprint-governed-text-to-sql
```

### 2. Install

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

On Windows, run `.venv\Scripts\activate` instead.

```bash
python -m pip install -e ".[dev,llm]"
```

Using a separate `.venv` for this project avoids clashes with packages you already have installed.

### 3. Add your settings

```bash
cp .env.example .env
```

Open `.env` and set the database connection.

**Local PostgreSQL** (a database you can be admin of):

```
DATABASE_URL=postgresql://postgres:<password>@localhost:5432/<database>
```

**Supabase**: copy the values from *Project Settings → Database → Connect → Session pooler*:

```
DATABASE_URL=postgresql://postgres.<project-ref>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Use the **Session pooler (port 5432)**, not the Transaction pooler (6543).

**Passwords go in their own file.** Create `.password.env` next to `.env`. Git ignores it, so it is never uploaded:

```
DATABASE_PASSWORD=<your database password>
AGENT_DB_PASSWORD=<any random text, 16+ characters>
```

`AGENT_DB_PASSWORD` is for the read-only login the app creates for itself. You never type it anywhere else. To make one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### 4. Choose a model

Pick one and add its lines to `.env`. You can change it any time.

| Option | Add to `.env` | Notes |
|---|---|---|
| **None** (offline mode) | nothing | Answers only the built-in retail example questions. Good for a first look. |
| **Ollama** (free, runs on your machine) | `MODEL_BACKEND=ollama`<br>`OLLAMA_MODEL=qwen2.5-coder:7b` | Install from [ollama.com](https://ollama.com), then run `ollama pull qwen2.5-coder:7b` (about 4.7 GB). Nothing leaves your computer. |
| **Anthropic** | `MODEL_BACKEND=anthropic` | Put `ANTHROPIC_API_KEY=sk-ant-...` in `.password.env`. |
| **OpenAI or compatible** | `MODEL_BACKEND=openai`<br>`OPENAI_MODEL=<model name>` | Put `OPENAI_API_KEY=...` in `.password.env`. Add `OPENAI_BASE_URL=` for other providers. |

### 5. Set up the database

```bash
sql-agent migrate
```

This creates the app's logins, security rules and views, and loads the synthetic retail data. It takes a few seconds locally, or about a minute on Supabase. It is safe to run again at any time.

### 6. Check everything

```bash
sql-agent viewer
```

You should see `[ok] database` and `[ok] model`.

### 7. Start the app

```bash
python start.py
```

This starts the app and opens **http://localhost:8000** in your browser. Press **Ctrl+C** to stop it. To open on a particular data set as a particular person:

```bash
python start.py --data retail --user priya
```

---

## Optional: add your own tables

To ask questions about your own schema in the same database (for example `banking`):

```bash
sql-agent dataset propose banking
```

Open `datasets/banking.json` and check two things:

- **`pii`:** every column holding personal data is marked.
- **`rows`:** which roles may see which rows. By default only the analyst and the privacy officer see rows.

Then apply it:

```bash
sql-agent dataset apply banking
```

Restart the app. **Banking** now appears in the **Data** menu. The README explains the options in that file.

---

## Troubleshooting

| You see | Do this |
|---|---|
| `address already in use` | Another copy is running. `python start.py` picks the next free port, or stop the old one with `lsof -ti tcp:8000 \| xargs kill`. |
| `password authentication failed for user "postgres…"` | Check `DATABASE_PASSWORD` in `.password.env`, or the password in `DATABASE_URL`. |
| `password authentication failed for user "nl2sql_agent…"` | `AGENT_DB_PASSWORD` changed since the last setup. Run `sql-agent migrate` again. |
| `AGENT_DB_PASSWORD … 16+ characters` | Set a long random `AGENT_DB_PASSWORD` in `.password.env` (see step 3). |
| `Could not reach the API` in the browser | The app stopped. Start it again with `python start.py`. |
| `requires a different Python` | Use Python 3.10 – 3.14. |
| pip reports conflicts with other packages | Make sure the `.venv` is active (the prompt shows `(.venv)`), then install again. |
| zsh shows `quote>` | A pasted line had a quote or `#` comment. Press Ctrl+C and paste the commands one at a time. |
| Ollama answers time out | The first question loads the model and can be slow; ask again. Check the model with `ollama list`. |
| `[warn] SAMPLE_DATA is no longer read` | Rename `SAMPLE_DATA=` to `RETAIL_DATA=` in `.env`. |

Still stuck? Run `sql-agent viewer` and include its output in an issue. It never prints passwords.
