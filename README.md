# KalshiWatch

A scheduled GitHub Actions runner for a job whose code lives in a private repository.

- `.github/workflows/watch.yml` — starts the job; it prints nothing and writes nothing back here.
- Secrets: `PRIVATE_REPO_KEY` (a read-only deploy key) and `DISPATCH_TOKEN` (a fine-grained token that
  can only send `repository_dispatch` events).
