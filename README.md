# Data-Analyst Telegram Bot

An LLM agent (OpenAI Responses API, `web_search` + custom `fetch_url`/`run_python`
tools) that answers data-analysis questions sent over Telegram - MOSPI and
similar public-dataset questions - and replies with exactly one JSON object.

## How it works

- `bot.py` - Telegram entrypoint (`python-telegram-bot`). Webhook mode when
  `RENDER_EXTERNAL_URL` is set (i.e. on Render), long-polling otherwise (local dev).
- `agent.py` - the tool-calling loop against OpenAI's Responses API.
- `tools.py` - `fetch_url` (download a dataset) and `run_python` (sandboxed
  subprocess with pandas/numpy) - the two custom function tools.
- `logger.py` - appends one JSON line per turn to `logs/run.jsonl` in this repo
  via the GitHub Contents API, so it stays public at:
  `https://raw.githubusercontent.com/<owner>/<repo>/main/logs/run.jsonl`.

The bot always replies to every incoming message (required so multi-turn
grading exchanges don't time out), but only replies with JSON when the
message itself asks for a JSON-only reply; otherwise it sends a short
acknowledgement. If a requested JSON template includes a `log_url` key, the
bot fills in the real log URL above automatically.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OPENAI_API_KEY, GH_TOKEN, GITHUB_REPO (TELEGRAM_BOT_TOKEN not needed for test_agent.py)
```

Test the agent directly, no Telegram needed:

```bash
python test_agent.py "Which state has the highest maternal mortality rate based on MOSPI data? Reply with ONLY this JSON object and nothing else: {\"answer\": {\"state\": \"<state name>\"}, \"log_url\": \"<public wget-able URL to your agent's JSONL log>\"}"
```

Multi-turn example (same chat, in order):

```bash
python test_agent.py --chat demo \
  "Here is some context: ..." \
  "Now, based on that, reply with ONLY {\"answer\": 42}"
```

Run the real bot locally (long-polling):

```bash
python bot.py
```

## Deploying to Render (free tier)

1. Push this repo to GitHub (public).
2. In Render: New -> Blueprint -> pick this repo (uses `render.yaml`).
3. Fill in the four secret env vars in Render's dashboard:
   `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `GH_TOKEN`, `GITHUB_REPO`.
4. Deploy. Render sets `RENDER_EXTERNAL_URL` automatically, so `bot.py` picks
   webhook mode and registers itself with Telegram on startup - no manual
   webhook call needed.

Note: Render's free web services sleep after ~15 min of no HTTP traffic and
cold-start on the next request (Telegram retries failed webhook deliveries,
so a slow cold start is usually recoverable, but for best reliability during
grading consider an external uptime pinger (e.g. UptimeRobot, free) hitting
the Render URL every ~10 minutes to keep the instance warm.

## Registering

- GitHub repo URL: this repo's public URL.
- Telegram bot username: whatever you set with `@BotFather` (must end in `bot`).
