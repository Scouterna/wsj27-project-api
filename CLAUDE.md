# wsj27-project-api — git workflow

Currently only one maintainer (Håkan). Two long-lived branches, no PRs yet
(deliberately deferred — see "Later" below; don't assume they're in place).
Work is tracked on the shared WSJ27 GitHub project; how agents use it is in the
`github-project` memory of the WSJ27 parent store, imported by `../CLAUDE.md`.

## Branch model

- **`main`** — production line. Only receives code that's been decided ready
  for prod: a deliberate `dev` → `main` promotion, or a `hotfix/...` branch cut
  directly from `main`. **Anything that lands on `main` is released at once:**
  pushed, and tagged with the next `vX.Y.Z` (see "Release" below). `main` is
  never left ahead of `origin/main` or ahead of its latest tag.
- **`dev`** — integration branch. All feature branches merge here first and
  accumulate, so multiple in-progress features can be tested together. Every
  push to `dev` makes CI publish `:dev`. In the dev environment, ArgoCD Image Updater
  notices the new image and rolls it out within a few minutes. Nothing else is
  needed, so no `rollout restart`.
- **`feat/...`** — cut from `main` (not `dev`, so each feature's history
  stays independent of whatever else is currently in `dev` and can be
  promoted to prod on its own later), merged into `dev` with a local
  `git merge` (no PR yet) once ready to test alongside whatever else is there.
- **`hotfix/...`** — cut from `main`, for changes that must reach prod without
  waiting on whatever untested work currently sits in `dev` (e.g. an
  authorization fix). Merged into `main`, then merged *forward* into `dev` so
  the fix isn't lost or reintroduced by the next promotion.

Prod runs the version pinned in `../wsj27-infra/k8s/prod/wsj27-project-api.yaml`.
ArgoCD applies whatever is on wsj27-infra's `origin/main`. **Pushing wsj27-infra is
always Håkan's manual step. Agents never push it.** Pushing to `dev`, or even
to `main`, never touches prod by itself.

## Release

Merging or committing to `main` happens only when Håkan asks for it. Once he
has, these steps are part of the same job and need no separate go-ahead:

```bash
git push origin main
git describe --tags --abbrev=0            # last release, e.g. v1.1.0
git tag -a v1.2.0 -m v1.2.0               # next version, on the commit just pushed
git push origin v1.2.0
gh run watch                              # CI publishes :v1.2.0
```

Pick the version from the commits since the last tag. Any `feat` means a minor
bump, and only fixes or chores mean a patch bump. A major bump needs Håkan to
say so.

Then pin it for prod in `../wsj27-infra`. Change only the image tag in
`k8s/prod/wsj27-project-api.yaml`, and commit it on wsj27-infra's `main` with a
one-line message (`chore(k8s): bump wsj27-project-api to v1.2.0`). **Do not push
wsj27-infra.** Tell Håkan the commit is ready. His push is what rolls prod.

Finally, close the issues this release completes and set them to **In Prod** on
the project.

## Recipes

**Start a feature:**
```bash
git checkout main && git pull
git checkout -b feat/whatever
# ...commit...
```

**Bring it into dev for testing:**
```bash
git checkout dev && git pull
git merge feat/whatever
git push origin dev
```
CI publishes `:dev`, and the dev environment picks it up by itself.

**Promote to prod** (once you've decided what's in `dev` is ready):
```bash
git log main..dev --oneline      # check exactly what you're about to ship
git checkout main && git pull
git merge dev
```
Then run "Release" above.

**Promote a single feature, not everything in dev:**
If `dev` also has other features that aren't decided yet, don't merge `dev`
wholesale — merge just that feature's own branch into `main` instead:
```bash
git checkout main && git pull
git merge feat/whatever
```
Then run "Release". This only stays clean because the feature
was branched off `main` (per "Start a feature" above), not off `dev`'s tip —
if it had been branched off `dev` after other features already landed there,
its history would include their commits too, and only `git cherry-pick` of
its individual commits (and only if it doesn't actually depend on their code)
would separate it out. Keep a feature's branch around until it's been
promoted, in case you need to do this.

**Hotfix straight to prod, bypassing untested `dev` work:**
```bash
git checkout main && git pull
git checkout -b hotfix/auth-thing
# ...commit...
git checkout main
git merge hotfix/auth-thing
```
Run "Release", then sync the fix back into `dev`:
```bash
git checkout dev
git merge main
git push origin dev
```

## Later (not yet in place)

- Switch `feat/... → dev` and `dev → main` merges to GitHub PRs once things
  stabilize — no CI change needed, `pull_request` builds already run.
