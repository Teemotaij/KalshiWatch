# KalshiWatch

Polls Kalshi's **public** market-data API once a minute for the tennis markets listed in
`watchlist.json`, logs per-minute tape medians to `ticks.csv`, and fires a
`repository_dispatch` event at a private repository when simple tape conditions are met.
All decisions, credentials and order flow live in the private repository; this repo holds
no API keys and reads nothing that is not public on kalshi.com.

- `watch.py` — the whole watcher; stdlib only.
- `watchlist.json` — written by the private repository; which tickers to follow.
- `ticks.csv` / `state.json` — the public tape log and the fired-event flags.
- Secret `DISPATCH_TOKEN` — a fine-grained PAT that can only send dispatch events.
