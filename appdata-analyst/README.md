# Rounds App Data Analyst

A Slack chatbot that answers natural-language questions about mobile app performance data for apps acquired by Rounds. It generates SQL, queries the analytics Postgres database, and renders responses as formatted tables, charts, and follow-up buttons — all inline in Slack threads.

## What it does

- Answers questions like "top 5 apps by revenue last quarter" or "iOS vs Android installs since January"
- Writes and executes SQL against the analytics database autonomously
- Returns structured responses: prose summaries, sortable tables, line/bar/pie charts (uploaded as PNG), and suggested follow-up buttons
- Maintains conversation context within a thread — follow-ups like "what about by platform?" just work
- Traces every agent call to Langfuse: input, output, tool calls with their SQL, and token usage

## Architecture

```
User message → Slack → Bolt listener
  → interim ":mag: Looking that up..." (background thread)
  → Pydantic AI ReAct loop
      → execute_sql (sqlglot validation → psycopg3 pool → Postgres)
      → LLM composes AnalyticsResponse (table / chart / prose / buttons)
  → composer renders blocks + uploads charts
  → chat_update replaces interim message
  → conversation history saved to in-memory store
```

**Key files:**

| File | Role |
|---|---|
| `agent/agent.py` | System prompt, model selection, `run_agent()` |
| `agent/tools/execute_sql.py` | SQL validation, hard cap, retry logic |
| `agent/response_model.py` | `AnalyticsResponse` structured output schema |
| `formatting/composer.py` | Slack block assembly and chart uploads |
| `observability/langfuse.py` | Langfuse tracing — spans per tool call, token counts |
| `thread_context/store.py` | In-memory conversation history, keyed by thread |

## Future actions

### P1

1. Langfuse v2 does not receive security updates. Migrate to v3.
2. Fix bug where arguments to plot generation is improper. Idea: give more examples to LLM on how to generate arguments for plot.
3. Groq offers better ZDR and lower prices than Anthropic. Investigate if open-weights models on groq are good enough for our tasks.
4. Add concurrency protection features so we can increase the number of parallel workers.
5. Persist conversation history. So a server restart will not refresh the conversation history.

### P2
6. Explore a common style for plots, and apply that. It should improve readability.
7. Implement the context protection plan.
8. Update the bot's welcome buttons to match the Data Analyst intent. They are currently leftovers from the starter template.

## Prerequisites

- **Python 3.11+**
- **Docker + Docker Compose** — for the analytics database and Langfuse
- **A Slack app** installed to your workspace (see [Slack Setup](#slack-setup) below)
- **One AI provider key** — Anthropic (`claude-sonnet-4-6` recommended), Groq (`llama-3.3-70b-versatile`), or OpenAI (`gpt-5.4`); OpenAI not recommended because of lack of signed zero data retention agreement.

## Setup

### 1. Start the infrastructure

From the repo root (where `docker-compose.yml` lives):

```sh
docker compose up -d
```

This starts three containers:

| Container | Purpose | Port |
|---|---|---|
| `ai-engineer-db` | Analytics Postgres, seeded with 50 apps × 2 years of daily metrics | 5432 |
| `langfuse-db` | Langfuse's own Postgres | 5433 |
| `langfuse` | Langfuse web UI | 3000 |

Wait until all three are healthy:

```sh
docker compose ps
```

### 2. Configure environment variables

```sh
cp .env.sample .env
```

Open `.env` and fill in the required values — see the sections below for where to get each one.

### 3. Install Python dependencies

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Slack Setup

The bot runs in **Socket Mode** using a bot token and an app-level token — no public URL or ngrok needed.

### Create the Slack app

1. Go to [https://api.slack.com/apps/new](https://api.slack.com/apps/new) and choose **From an app manifest**
2. Select your workspace
3. Paste the contents of [`manifest.json`](./manifest.json) (JSON tab) and click **Next**
4. Review and click **Create**, then **Install to Workspace → Allow**

### Get the Bot Token

1. In your app settings, go to **OAuth & Permissions**
2. Copy the **Bot User OAuth Token** (`xoxb-...`)
3. Add it to `.env`:

```
SLACK_BOT_TOKEN=xoxb-...
```

### Get the App-Level Token

Socket Mode requires an additional app-level token:

1. Go to **Basic Information → App-Level Tokens**
2. Click **Generate Token and Scopes**, give it a name, and add the `connections:write` scope
3. Copy the token (`xapp-...`)
4. Add it to `.env`:

```
SLACK_APP_TOKEN=xapp-...
```

---

## Langfuse Setup

Langfuse traces every agent call — inputs, outputs, SQL tool calls, and token costs.

1. Open [http://localhost:3000](http://localhost:3000) and create an account
2. Create an organization and a project
3. Go to **Project Settings → API Keys** and click **Create new API keys**
4. Copy both keys into `.env` immediately — the secret key is shown only once:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
```

> The Docker stack runs Langfuse v2 and the Python SDK is pinned to `langfuse>=2,<3` to match. Do not upgrade the SDK without also upgrading the server image.

---

## AI Provider Setup

Set **one** provider key and its matching model variable. If multiple keys are present, priority is **Groq → Anthropic → OpenAI**.

### Anthropic (recommended)

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=anthropic:claude-sonnet-4-6
```

Get a key from [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).

### Groq

```
GROQ_API_KEY=gsk_...
GROQ_MODEL=groq:llama-3.3-70b-versatile
```

Get a key from [console.groq.com/keys](https://console.groq.com/keys).

### OpenAI

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=openai:gpt-4.1
```

Get a key from [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

---

## Running the App

```sh
python3 app.py
```

You should see:

```
⚡️ Bolt app is running!
```

The bot is now live and connected to Slack via Socket Mode.

---

## Using the Bot

**Direct message** — Open a DM with the bot and ask a question. The bot replies in a thread and remembers context across follow-ups within that thread.

**Channel @mention** — Invite the bot to a channel (`/invite @appdata-analyst`), then @mention it with your question. It replies in a thread to keep the channel clean.

**Example queries:**

| Query | Expected response |
|---|---|
| `how many apps do we have?` | Prose: "50 apps" |
| `top 5 apps by total revenue in March 2026` | Table + bar chart + follow-up buttons |
| `chart revenue for Paint since Jan 2025` | Line chart (weekly aggregated) |
| `iOS vs Android revenue last quarter` | Multi-series line chart |
| `installs by platform last month` | Pie chart |
| `which apps had the biggest UA spend last month?` | Table with delta column |
| `which apps are doing well?` | Clarifying question + suggestion buttons |
| _(follow-up in same thread)_ `what about by platform?` | Uses full thread context |

---

## Observability

Open [http://localhost:3000](http://localhost:3000) → **Tracing** after sending a message. Each trace shows:

- The user's input
- The final structured response
- One span per tool call (`execute_sql`) with the SQL and returned data
- Token counts: input, output, cache read/write

---

## Linting

```sh
ruff check
ruff format
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `RuntimeError: No AI provider configured` | Set `*_API_KEY` + matching `*_MODEL` in `.env` |
| `invalid_auth` from Slack | Check `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are correct and the app is installed |
| Bot doesn't respond | Check `docker compose ps` — `ai-engineer-db` must be healthy |
| No traces in Langfuse | Verify `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` in `.env`; restart the app after editing |
| `missing_scope` on chart upload | Re-install the app — `files:write` must be in the manifest before installation |
| Charts appear as file attachments | Expected in developer sandboxes — charts are posted as threaded PNG files |


