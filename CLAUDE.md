# wsj27-project-api — git workflow

Currently only one maintainer (Håkan). Two long-lived branches, no PRs or version tags yet
(deliberately deferred — see "Later" below; don't assume they're in place).

## Branch model

- **`main`** — production line. Only receives code that's been decided ready
  for prod: a deliberate `dev` → `main` promotion, or a `hotfix/...` branch cut
  directly from `main`. Every push to `main` triggers CI
  (`.github/workflows/build-and-publish.yml`), which publishes
  `ghcr.io/scouterna/wsj27-project-api:latest` and `:<short-sha>`.
- **`dev`** — integration branch. All feature branches merge here first and
  accumulate, so multiple in-progress features can be tested together. Every
  push to `dev` triggers CI too, publishing `:dev`. The dev k8s environment
  (`wsj27-infra/envs/dev/wsj27-project-api.yaml`) tracks `:dev`.
- **`feat/...`** — cut from `dev`, merged back into `dev` with a local
  `git merge` (no PR yet) once ready to test alongside whatever else is there.
- **`hotfix/...`** — cut from `main`, for changes that must reach prod without
  waiting on whatever untested work currently sits in `dev` (e.g. an
  authorization fix). Merged into `main`, then merged *forward* into `dev` so
  the fix isn't lost or reintroduced by the next promotion.

The only two things that ever affect prod: pushing to `main` (produces a new
candidate image), and manually bumping the SHA pin in
`wsj27-infra/envs/prod/wsj27-project-api.yaml` + applying it. Pushing to `dev`
never touches prod.

## Recipes

**Start a feature:**
```bash
git checkout dev && git pull
git checkout -b feat/whatever
# ...commit...
```

**Bring it into dev for testing:**
```bash
git checkout dev && git pull
git merge feat/whatever
git push origin dev
```
CI publishes `:dev`. Like `:latest`, this is a moving tag — after a push,
`kubectl -n proj-wsj27-dev rollout restart deployment/wsj27-project-api` is
needed to actually repull it; `apply` alone won't.

**Promote to prod** (once you've decided what's in `dev` is ready):
```bash
git log main..dev --oneline      # check exactly what you're about to ship
git checkout main && git pull
git merge dev
git push origin main
```
Watch CI (`gh run list` / `gh run watch`), get the new short SHA
(`git rev-parse --short HEAD`), bump the image tag in
`wsj27-infra/envs/prod/wsj27-project-api.yaml` to that SHA, apply, and
`rollout restart` in the `proj-wsj27-prod` namespace.

**Hotfix straight to prod, bypassing untested `dev` work:**
```bash
git checkout main && git pull
git checkout -b hotfix/auth-thing
# ...commit...
git checkout main
git merge hotfix/auth-thing
git push origin main
```
Bump prod's SHA pin as above, then sync the fix back into `dev`:
```bash
git checkout dev
git merge main
git push origin dev
```

## Later (not yet in place)

- Switch `feat/... → dev` and `dev → main` merges to GitHub PRs once things
  stabilize — no CI change needed, `pull_request` builds already run.
- Start tagging releases (`v1.2.3`) on `main` instead of pinning prod to a raw
  SHA — the CI trigger for `v*.*.*` tags already exists and needs nothing
  further.
