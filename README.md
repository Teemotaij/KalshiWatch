# KalshiWatch

A scheduled GitHub Actions runner for a job whose code lives in a private repository.

- `.github/workflows/watch.yml` — starts the job; it prints nothing and writes nothing back here.
- `.github/workflows/relay.yml` — a second runner of the same kind; it writes back only bare heartbeat timestamps on
  the `heartbeat`, `hb-a` and `hb-b` branches.
- Secrets: `PRIVATE_REPO_KEY` (a read-only deploy key) and `DISPATCH_TOKEN` (a fine-grained token that
  can only send `repository_dispatch` events); `relay.yml` also passes `LIVETENNIS_API_KEY` to its job.
