# wsj27-project-api — git workflow

Currently only one maintainer (Håkan). Two long-lived branches, no PRs yet
(deliberately deferred — see "Later" below; don't assume they're in place).
Work is tracked on the shared WSJ27 GitHub project; how agents use it is in the
`github-project` memory of the WSJ27 parent store, imported by `../CLAUDE.md`.

## Branch model

The one rule everything else follows from: **`dev` is never promoted.** Code
reaches `main` one feature branch at a time, never as "whatever is in `dev`".

- **`main`** — production line. Receives only feature branches that were
  chosen for a release, and `hotfix/...` branches. **Anything that lands on
  `main` is released at once:** pushed, and tagged with the next `vX.Y.Z` (see
  "Release" below). `main` is never left ahead of `origin/main` or ahead of its
  latest tag.
- **`dev`** — the integration point, where Campfire meets the API. Everything
  that is built and testable goes here as early as possible, so Joakim can build
  against the real API, demonstrations run in dev, and the API docs there are
  live. Every push to `dev` makes CI publish `:dev`. In the dev environment,
  ArgoCD Image Updater notices the new image and rolls it out within a few
  minutes. Nothing else is needed, so no `rollout restart`.
  - `dev` only ever receives **merges**: of feature branches, and of `main`
    after a release. **Never commit directly on `dev`**, not even a small fix.
    A fix to a feature goes on that feature's branch, which is merged into `dev`
    again. A commit made on `dev` can't reach `main` without dragging everything
    under it along.
  - Taking something back out of `dev` (it isn't wanted after all, or must wait)
    is a `git revert` of its merge commit **on `dev` only**. That revert never
    reaches `main`, because `dev` is never promoted. If the feature returns
    later, revert the revert; merging the branch again would do nothing.
- **`feat/<issue>-<slug>`** (or `fix/`) — for example `feat/16-patrol-write`.
  The issue number in the name is what lets a release go from "issues #9 and
  #22" to branches. Cut from `main`, so each feature can be released on its own.
  Merged into `dev` as soon as it's worth trying there, then again each time it
  gets more commits. **Keep the branch until it's released**: it is what gets
  merged into `main`.
- **Work that depends on unreleased work** — branch from the branch it depends
  on, not from `main` or `dev`, and say so in the issue's notes ("Needs #1").
  The dependency then shows in both places, and it's clear at release time that
  the second can't go without the first. If the dependency was not intended,
  that's the moment to cut it loose, not at release time.
- **A large piece of work spread over several issues** (like the case API) may
  share one branch. It is then released as a whole: every issue on it goes to
  staging together, so only put issues on it that can wait for each other.
- **`hotfix/<issue>-<slug>`** — cut from `main`, for changes that must reach
  prod without waiting on other work (e.g. an authorization fix). Merged into
  `main` and released, then `main` is merged into `dev`.

**Changing existing behaviour.** A new endpoint or field that nothing calls yet
is safe to release ahead of Campfire. A change to something that already exists
(a renamed role, a response shape, a removed field) is not. Name such a change in
the issue's notes and tell Joakim before it's merged into `dev`, since it reaches
him there first.

Prod runs the version pinned in `../wsj27-infra/k8s/prod/wsj27-project-api.yaml`.
ArgoCD applies whatever is on wsj27-infra's `origin/main`. **Pushing a prod pin
in wsj27-infra is always Håkan's manual step. Agents never push it.** Pushing to
`dev`, or even to `main`, never touches prod by itself.

## Release

A release is a list of issues. Each maps to its branch, and **only branches that
are already in `dev` can be released**, since that's where they were tried:

```bash
git fetch --all --tags
git branch -a --merged origin/dev --no-merged origin/main   # what dev has that main lacks
git merge-base --is-ancestor feat/16-patrol-write origin/dev && echo in dev
```

The first command also lists branches without an issue that are waiting to go
out (see the `no-issue-changes-ride-along` memory). Releasing them is a question
for Håkan, not automatic.

Merging or committing to `main` happens only when Håkan asks for it. Once he
has, these steps are part of the same job and need no separate go-ahead:

```bash
git checkout main && git pull --ff-only
git merge --no-edit feat/9-cases           # one merge per released branch,
git merge --no-edit feat/22-adult-names    # dependencies first
# run the tests
git push origin main
git describe --tags --abbrev=0            # last release, e.g. v1.1.0
git tag -a v1.2.0 -m v1.2.0               # next version, on the commit just pushed
git push origin v1.2.0
gh run watch                              # CI publishes :v1.2.0
git checkout dev && git pull
git merge --no-edit main && git push origin dev   # dev always contains main
```

Pick the version from the commits since the last tag. Any `feat` means a minor
bump, and only fixes or chores mean a patch bump. A major bump needs Håkan to
say so.

A released branch can be deleted once its tag is pushed.

Staging, the prod pin, and the board are in the `release-to-staging` skill:
staging gets the tag first, and prod gets the same pin once staging accepted it.
For a prod pin, change only the image tag in `k8s/prod/wsj27-project-api.yaml`,
and commit it on wsj27-infra's `main` with a one-line message
(`chore(k8s): bump wsj27-project-api to v1.2.0`). **Do not push it.** Tell Håkan
the commit is ready. His push is what rolls prod.

## Recipes

**Start a feature:**
```bash
git checkout main && git pull
git checkout -b feat/16-patrol-write
# ...commit...
```

**Start one that needs an unreleased feature:**
```bash
git checkout feat/1-scout-db && git pull
git checkout -b feat/2-avatar
# ...commit... and write "Needs #1" in #2's notes
```

**Bring it into dev, and again after every new commit on it:**
```bash
git checkout dev && git pull
git merge --no-edit feat/16-patrol-write
git push origin dev
```
CI publishes `:dev`, and the dev environment picks it up by itself.

**Take it back out of dev:**
```bash
git checkout dev && git pull
git log --merges --oneline --grep 'feat/16-patrol-write'   # find its merges
git revert -m 1 <merge>          # newest first, if it was merged more than once
git push origin dev
```
When it returns: `git revert <that revert>` on `dev`, then merge the branch for
anything newer.

**Hotfix straight to prod:**
```bash
git checkout main && git pull
git checkout -b hotfix/40-auth-thing
# ...commit...
git checkout main
git merge --no-edit hotfix/40-auth-thing
```
Run "Release", which ends by merging `main` into `dev`.

## Later (not yet in place)

- Switch `feat/... → main` merges to GitHub PRs once things stabilize — no CI
  change needed, `pull_request` builds already run. The branch model above
  already has the shape PRs need: one feature branch per merge into `main`.
