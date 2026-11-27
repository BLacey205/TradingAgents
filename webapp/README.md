# TradingAgents web app

A phone-first page for running TradingAgents and reading the results.

- Type a ticker (`NVDA`, `BTC-USD`) or paste a quote-page link from Yahoo Finance,
  Robinhood, TradingView, Google Finance, CNBC, StockTwits, Coinbase or CoinGecko.
- The page shows the team's progress (analysts, debate, trader, risk team, final
  decision), then the rating, confidence, decision and every report.
- Follow-up questions are answered from that saved analysis, one quick model call each.
- Each ticker is analyzed once per day unless you ask for a fresh run. Recent
  analyses are listed.
- A note on every result says how often the ratings were right in testing (from
  the research lab's ledger).

It uses only the Python standard library on top of the `tradingagents` package.

## Run it on a computer

```sh
pip install .
export GOOGLE_API_KEY=...                     # or another provider's key
export TRADINGAGENTS_LLM_PROVIDER=google
export TRADINGAGENTS_DEEP_THINK_LLM=gemini-3.1-flash-lite
export TRADINGAGENTS_QUICK_THINK_LLM=gemini-3.1-flash-lite
export WEBAPP_PASSCODE=pick-a-long-passcode
python -m webapp --host 0.0.0.0 --port 8000
```

Open `http://<computer's address>:8000` on the phone (same Wi-Fi), enter the
passcode, then use Safari's Share → **Add to Home Screen** to get an app icon.

| Variable | Purpose |
|---|---|
| `WEBAPP_PASSCODE` | Required whenever the server is reachable from the internet. Every analysis spends the provider's quota or credits. |
| `WEBAPP_DATA_DIR` | Where saved analyses go (default `~/.tradingagents/web`). Use a persistent disk on a host. |
| `PORT`, `HOST` | Listen address (defaults `8000`, `127.0.0.1`). |
| `TRADINGAGENTS_*` | The usual model and provider settings. |

## Host it on Render (free)

`render.yaml` at the repo root is a Render Blueprint for this app. A test analysis
peaked at about 190 MB of memory, inside the free plan's 512 MB.

1. Sign in at render.com with GitHub and allow access to this repository.
2. **New → Blueprint**, pick the repository; Render reads `render.yaml`.
3. Fill in the two secrets it asks for: `GOOGLE_API_KEY` and `WEBAPP_PASSCODE`.
4. **Apply**. The first build takes a few minutes; the app's address ends in
   `.onrender.com`.

On the free plan the service sleeps after 15 idle minutes (the next visit takes
about a minute to wake it), and saved analyses are lost when it restarts.

## Host it elsewhere

Any host that runs a Docker container or a Python web service works. Use the repo's
`Dockerfile` with the start command `python -m webapp --host 0.0.0.0`, and set the
provider key, `WEBAPP_PASSCODE` and the model variables as the host's secrets.

Things to know:

- Analyses take under a minute to several minutes. Hosts that cut requests short
  are fine because the page polls; hosts that put an idle service to sleep will
  wake it on the next visit.
- Jobs in progress live in memory, so a restart drops a running analysis.
  Finished ones persist only if `WEBAPP_DATA_DIR` is on a persistent disk.
- One analysis runs at a time, so a free-tier key isn't flooded.
