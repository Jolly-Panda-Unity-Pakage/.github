# Automatic "Our Packages" list

A GitHub Actions workflow keeps the **Our Packages** section of
[`profile/README.md`](../profile/README.md) in sync with the organization's published releases.
Only the text between these markers is ever rewritten; everything else in the README is left untouched:

```
<!-- PACKAGES:START -->
<!-- PACKAGES:END -->
```

> Hand-written content elsewhere in the section (for example the *In Development* entry and
> *More Tools Coming Soon*) is **not** managed. When a hand-written package gets its first release, delete its
> hand-written entry so it is not listed twice.

## How discovery works

Workflow: [`.github/workflows/update-packages.yml`](../.github/workflows/update-packages.yml) · Script: [`scripts/update_packages.py`](../scripts/update_packages.py) (Python standard library only)

1. **`generate` job** (read-only): runs the unit tests, lists the organization's public repositories (`GET /orgs/{org}/repos`), then lists each candidate's releases (`GET /repos/{owner}/{repo}/releases`). All pages are followed. The result is saved as `packages.json`.
2. **`commit` job** (the only job with `contents: write`): checks out the default branch again, renders `packages.json` between the markers, and commits `docs: update published packages list` **only if the README changed**.

The organization is taken from `github.repository_owner`, so nothing is hard-coded.

## What qualifies a package

A repository is listed when **all** are true:

- public, and not disabled
- not archived (*policy, see below*)
- not a fork (*policy*)
- not the `.github` repository itself
- has at least one release that is published (not a draft), is not a pre-release (*policy*), and has a tag and a publish date

The listed version is the most recently **published** eligible release. Entries are sorted alphabetically by display name.
The display name comes from the repository name (`audio-import-optimizer` → *Audio Import Optimizer*); the description comes from the repository's **About** description (a missing one shows *Description coming soon.*, so set it on GitHub).

Optional: set the repository variable `REQUIRED_TOPIC` (and add `REQUIRED_TOPIC: ${{ vars.REQUIRED_TOPIC }}` to the workflow `env:`) to list only repositories carrying a topic such as `unity-package`.

## Run it manually

**Actions → Update published packages → Run workflow.** Optional toggles for that run: include pre-releases, include archived repositories, allow an empty list.

Locally (dry run on a copy is a good idea first):

```bash
export GITHUB_TOKEN=<token>   # optional for public data, but avoids the 60 requests/hour limit
python3 scripts/update_packages.py run --org <org> --readme profile/README.md
python3 -m unittest discover -s tests -v
```

## Change the policies

| Policy | Default | Make it permanent | One-off |
|---|---|---|---|
| Include pre-releases | off | Repo variable `INCLUDE_PRERELEASES=true` (Settings → Secrets and variables → Actions → *Variables*) | Run-workflow checkbox |
| Include archived repos | off | Repo variable `INCLUDE_ARCHIVED=true` | Run-workflow checkbox |
| Include forks | off | Add `INCLUDE_FORKS: "true"` to the workflow `env:` | – |

Pre-release entries are labelled *(pre-release)*. Manual checkboxes can only switch a policy **on**; to switch a permanent policy off, change or delete the variable.

## Safety behaviour

- Any API failure (after retries for 5xx / rate limits) fails the run **before** the README is modified. A partial list is never written.
- A run that would replace a non-empty list with an empty one is refused unless you pass the *allow empty list* option. This guards against a bad API response wiping the section.
- Missing or duplicated markers fail the run with a clear message. If the markers are missing but an `Our Packages` heading exists, they are inserted under that heading.
- The workflow has no `push` trigger and commits with `GITHUB_TOKEN`, so its own commit cannot start another run. Runs are serialized with a `concurrency` group.
- Descriptions and names are Markdown-escaped and truncated to 280 characters.

## Required repository settings

- **Settings → Actions → General → Workflow permissions:** normally nothing to change, because the `commit` job declares `contents: write` itself. If the push is rejected with `403`, check whether an organization policy restricts what workflows may request.
- **Branch protection / rulesets on the default branch:** if pushes must go through pull requests, `GITHUB_TOKEN` cannot push and the commit step fails. Either allow the GitHub Actions bot to bypass the rule, or change the commit job to open a pull request.
- **Actions must be enabled** for this repository. GitHub disables scheduled workflows in a public repository after about 60 days without repository activity; if the daily run stops, re-enable it from the Actions tab (a manual run or any commit is enough to keep it alive).
- No secrets are required for the daily schedule: public repository and release data is readable with the built-in `GITHUB_TOKEN`.

## Optional: update immediately after a release

The workflow already listens for a `repository_dispatch` event of type `package-release-published`. The daily schedule keeps working independently if you never use this.

`GITHUB_TOKEN` of one repository **cannot** trigger workflows in another, so each package repository needs a credential allowed to call the dispatch API on this repository. Pick one:

**Recommended – GitHub App** (no personal token, short-lived):
1. Create an org-owned GitHub App with only **Repository permissions → Contents: Read and write** (that is what the dispatch endpoint requires), and install it **only on the `.github` repository**.
2. Store the App ID as an organization variable `PROFILE_UPDATER_APP_ID` and the private key as an organization secret `PROFILE_UPDATER_APP_KEY`, shared with the package repositories.

**Simpler – fine-grained personal access token:** owner = the organization, *Only select repositories* → `.github`, permission **Contents: Read and write**, short expiry. Save it as an organization secret `PROFILE_DISPATCH_TOKEN`, shared only with the package repositories. Prefer a dedicated machine account over a personal account, and rotate it before it expires.

Then add this workflow to **each package repository** (token variant shown; for the App, first add a step with `actions/create-github-app-token`, pinned to a commit SHA, and use its output instead):

```yaml
name: Notify organization profile
on:
  release:
    types: [published, unpublished, deleted]
permissions: {}
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger profile README update
        env:
          GH_TOKEN: ${{ secrets.PROFILE_DISPATCH_TOKEN }}
          ORG: ${{ github.repository_owner }}
        run: |
          curl --fail-with-body -sS -X POST \
            -H "Authorization: Bearer $GH_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$ORG/.github/dispatches" \
            -d '{"event_type":"package-release-published"}'
```

Notes: `repository_dispatch` workflows always run from the default branch. The event payload is ignored; every run re-discovers everything, so a forged or duplicate event can at worst cause an extra (harmless) run. Releases created by a workflow using `GITHUB_TOKEN` do not fire `release` events; create them with a PAT/App token, or call the dispatch step directly in that workflow.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `HTTP 401` | The token is missing, expired or revoked (only relevant for local runs or a custom token). |
| `HTTP 403` … *rate limit* | Local run without a token. The script retries short waits itself; otherwise export `GITHUB_TOKEN` or wait for the reset. |
| `HTTP 403` … *Resource not accessible by integration* | A job lacks a needed permission, or the organization restricts the default `GITHUB_TOKEN`. Check Settings → Actions → General. |
| `HTTP 404` on the org repos call | Wrong organization name, or the workflow runs somewhere other than the organization's `.github` repo. |
| Push step: `protected branch hook declined` / `GH006` | Default-branch protection blocks the bot. See *Required repository settings*. |
| `Refusing to replace an existing package list with an empty one` | No eligible repositories were found. Check policies/releases; if intended, run manually with *allow empty list*. |
| `Found N START and M END markers` | The markers were edited by hand. Restore exactly one of each. |
| A repository is missing | It is private, archived, a fork, has only draft/pre-release releases, or has no release. A release must be *published*, not saved as a draft. |
| Release published but README unchanged | Wait for the next run, start one manually, or set up the optional dispatch above. |
| Scheduled runs stopped | See the 60-day inactivity note above. |
