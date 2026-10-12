#!/usr/bin/env python3
"""Keep the "Our Packages" section of the organization profile README up to date.

The script discovers public, non-archived, non-fork repositories in a GitHub
organization that have at least one published (non-draft, non-prerelease)
GitHub Release, and rewrites ONLY the text between these markers:

    <!-- PACKAGES:START -->
    <!-- PACKAGES:END -->

Everything outside the markers is left byte-for-byte untouched (including the
file's line-ending style).

Sub-commands
    fetch   Query the GitHub REST API and write the package list to a JSON file.
    apply   Render a JSON package list into the README managed section.
    run     fetch + apply in one step (handy for local use).

Design notes
    * Standard library only - no dependencies to install or pin.
    * Fail safe: any API problem aborts the run BEFORE the README is touched.
    * Deterministic output (no timestamps), so unchanged data => no diff.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

START = "<!-- PACKAGES:START -->"
END = "<!-- PACKAGES:END -->"
GENERATED_NOTE = (
    "<!-- Generated automatically by .github/workflows/update-packages.yml "
    "- do not edit between the markers. -->"
)
EMPTY_MESSAGE = "No packages have been published yet."
MISSING_DESCRIPTION = "Description coming soon."
HEADING_RE = re.compile(r"^##[ \t]+.*Our Packages[ \t]*(?=\r?$)", re.MULTILINE)
ENTRY_RE = re.compile(r"^### 📦 \[", re.MULTILINE)  # entries produced by render_block()
MAX_DESCRIPTION_CHARS = 280
DEFAULT_README = "profile/README.md"
DEFAULT_API_URL = "https://api.github.com"
MAX_PAGES = 50  # hard stop: 50 pages * 100 items; exceeding it is treated as an error


class UpdateError(RuntimeError):
    """A condition that must abort the run without modifying the README."""


# --------------------------------------------------------------------------- #
# GitHub REST client
# --------------------------------------------------------------------------- #
class GitHubClient:
    def __init__(
        self,
        token: str | None,
        api_url: str = DEFAULT_API_URL,
        *,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 4,
        timeout: float = 30,
        max_wait: float = 60,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))  # resolved late so tests can patch it
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.max_wait = max_wait

    # -- low level ---------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jolly-panda-profile-readme-updater",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _rate_limit_wait(status: int, headers, body: str) -> float | None:
        """Seconds to wait if this response is a rate-limit response, else None."""
        remaining = headers.get("x-ratelimit-remaining")
        limited = (
            status == 429
            or headers.get("retry-after") is not None
            or (status == 403 and (remaining == "0" or "rate limit" in body.lower()))
        )
        if not limited:
            return None
        retry_after = headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        reset = headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(0.0, float(reset) - time.time()) + 1
        return 30.0

    def _explain(self, status: int, body: str, url: str) -> str:
        hint = {
            401: "Bad or expired credentials: check the token passed as GITHUB_TOKEN.",
            403: "Forbidden: missing permission or rate limit exceeded.",
            404: "Not found: check the organization name and that the token can see it.",
            422: "GitHub rejected the request parameters.",
        }.get(status, "Unexpected response from the GitHub API.")
        try:
            message = json.loads(body).get("message", "")
        except (ValueError, AttributeError):
            message = body.strip()[:200]
        return f"GitHub API returned HTTP {status} for {url}. {hint} Message: {message!r}"

    def _request(self, url: str) -> tuple[object, "urllib.response.addinfourl | object"]:
        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    raw = resp.read()
                    headers = resp.headers
                try:
                    return json.loads(raw), headers
                except ValueError as exc:
                    raise UpdateError(f"Invalid JSON from {url}: {exc}") from exc
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                wait = self._rate_limit_wait(exc.code, exc.headers, body)
                retryable = exc.code >= 500 or wait is not None
                last_error = self._explain(exc.code, body, url)
                if not retryable or attempt == self.max_attempts:
                    raise UpdateError(last_error) from exc
                delay = wait if wait is not None else 2 ** attempt
                if delay > self.max_wait:
                    raise UpdateError(f"{last_error} (would need to wait {delay:.0f}s)") from exc
                print(f"  retry {attempt}/{self.max_attempts - 1} in {delay:.0f}s: HTTP {exc.code}", file=sys.stderr)
                self._sleep(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = f"Network error while calling {url}: {exc}"
                if attempt == self.max_attempts:
                    raise UpdateError(last_error) from exc
                self._sleep(2 ** attempt)
        raise UpdateError(last_error)  # pragma: no cover

    @staticmethod
    def _next_link(headers) -> str | None:
        link = headers.get("Link") or headers.get("link") or ""
        for part in link.split(","):
            match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
            if match:
                return match.group(1)
        return None

    # -- public ------------------------------------------------------------- #
    def get_paged(self, path: str, params: dict[str, str] | None = None) -> list[dict]:
        """GET a list endpoint and follow every `rel="next"` page."""
        query = {"per_page": "100", **(params or {})}
        url: str | None = f"{self.api_url}{path}?{urllib.parse.urlencode(query)}"
        items: list[dict] = []
        pages = 0
        while url:
            # Never send the token to a host other than the configured API.
            if not url.startswith(self.api_url + "/"):
                raise UpdateError(f"Refusing to follow pagination link to unexpected host: {url}")
            pages += 1
            if pages > MAX_PAGES:
                raise UpdateError(f"More than {MAX_PAGES} pages for {path}; refusing to continue with partial data.")
            data, headers = self._request(url)
            if not isinstance(data, list):
                raise UpdateError(f"Expected a JSON list from {url}, got {type(data).__name__}.")
            items.extend(data)
            url = self._next_link(headers)
        return items


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Policy:
    include_prereleases: bool = False
    include_archived: bool = False
    include_forks: bool = False
    required_topic: str = ""  # optional: only repos carrying this topic
    exclude_repos: tuple[str, ...] = (".github",)


def repo_is_candidate(repo: dict, policy: Policy) -> bool:
    if repo.get("private") or repo.get("visibility", "public") != "public":
        return False
    if repo.get("disabled"):
        return False
    if repo.get("archived") and not policy.include_archived:
        return False
    if repo.get("fork") and not policy.include_forks:
        return False
    if repo.get("name") in policy.exclude_repos:
        return False
    if policy.required_topic and policy.required_topic.lower() not in [t.lower() for t in repo.get("topics") or []]:
        return False
    return bool(repo.get("name") and repo.get("html_url"))


def release_is_eligible(release: dict, policy: Policy) -> bool:
    if release.get("draft"):
        return False
    if release.get("prerelease") and not policy.include_prereleases:
        return False
    # "valid published release": has a tag, a public page, and a publish date.
    return bool(release.get("tag_name") and release.get("html_url") and release.get("published_at"))


def display_name(repo_name: str) -> str:
    """Readable name derived from the repository name (`audio-import_optimizer` -> `Audio Import Optimizer`)."""
    words = [w for w in re.split(r"[-_.\s]+", repo_name) if w]
    return " ".join(w if any(c.isupper() for c in w) else w[:1].upper() + w[1:] for w in words) or repo_name


def discover_packages(client: GitHubClient, org: str, policy: Policy) -> list[dict]:
    org_q = urllib.parse.quote(org, safe="")
    repos = client.get_paged(f"/orgs/{org_q}/repos", {"type": "public", "sort": "full_name"})
    print(f"Found {len(repos)} public repositories in {org}.")
    packages: list[dict] = []
    for repo in repos:
        if not repo_is_candidate(repo, policy):
            continue
        owner = (repo.get("owner") or {}).get("login") or org
        releases = client.get_paged(
            f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo['name'], safe='')}/releases"
        )
        eligible = [r for r in releases if release_is_eligible(r, policy)]
        if not eligible:
            print(f"  - {repo['name']}: no eligible release, skipped")
            continue
        latest = max(eligible, key=lambda r: r["published_at"])  # ISO-8601 sorts chronologically
        packages.append(
            {
                "name": repo["name"],
                "display_name": display_name(repo["name"]),
                "description": (repo.get("description") or "").strip(),
                "repo_url": repo["html_url"],
                "tag": latest["tag_name"],
                "release_url": latest["html_url"],
                "prerelease": bool(latest.get("prerelease")),
            }
        )
        print(f"  + {repo['name']}: {latest['tag_name']}")
    packages.sort(key=lambda p: (p["display_name"].casefold(), p["name"].casefold()))
    return packages


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~#!])")


def md_escape(text: str) -> str:
    """Escape text so it renders literally in GitHub-flavored Markdown (single line)."""
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("&", "&amp;")
    text = _MD_SPECIAL.sub(r"\\\1", text)
    # Block-level triggers at the start of a line.
    text = re.sub(r"^([-+]) ", r"\\\1 ", text)
    text = re.sub(r"^(\d+)([.)]) ", r"\1\\\2 ", text)
    return text


def safe_url(url: str) -> str:
    """Make a URL safe inside a Markdown link destination; reject non-http(s)."""
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        raise UpdateError(f"Refusing non-http(s) URL in package data: {url!r}")
    return re.sub(r"[ ()<>\"]", lambda m: f"%{ord(m.group()):02X}", url)


def clean_description(description: str) -> str:
    description = re.sub(r"\s+", " ", description or "").strip()
    if not description:
        return MISSING_DESCRIPTION
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return md_escape(description)


def render_entry(pkg: dict) -> list[str]:
    repo_url = safe_url(pkg["repo_url"])
    release_url = safe_url(pkg["release_url"])
    zip_url = safe_url(f"{pkg['repo_url'].rstrip('/')}/archive/refs/tags/{urllib.parse.quote(pkg['tag'], safe='/')}.zip")
    version = md_escape(pkg["tag"])
    suffix = " (pre-release)" if pkg.get("prerelease") else ""
    return [
        f"### 📦 [{md_escape(pkg['display_name'])}]({repo_url})",
        "",
        clean_description(pkg.get("description", "")),
        "",
        f"**Latest release:** [{version}]({release_url}){suffix} · [Download source (.zip)]({zip_url})",
    ]


def render_block(packages: Iterable[dict], eol: str = "\n") -> str:
    lines = [START, GENERATED_NOTE, ""]
    packages = list(packages)
    if not packages:
        lines.append(EMPTY_MESSAGE)
    for index, pkg in enumerate(packages):
        if index:
            lines.append("")
        lines.extend(render_entry(pkg))
    lines.append(END)
    return eol.join(lines)


# --------------------------------------------------------------------------- #
# README editing
# --------------------------------------------------------------------------- #
def detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") / 2 else "\n"


def ensure_markers(text: str, eol: str) -> str:
    """Return `text` with exactly one START/END pair, inserting them under the heading if absent."""
    starts, ends = text.count(START), text.count(END)
    if starts == 1 and ends == 1:
        if text.index(START) > text.index(END):
            raise UpdateError("PACKAGES:END appears before PACKAGES:START in the README.")
        return text
    if starts or ends:
        raise UpdateError(
            f"Found {starts} START and {ends} END markers; expected exactly one of each. Fix the README manually."
        )
    match = HEADING_RE.search(text)
    if not match:
        raise UpdateError('No markers and no "Our Packages" heading found; cannot choose where to insert the section.')
    insertion = f"{eol}{eol}{START}{eol}{END}"
    return text[: match.end()] + insertion + text[match.end():]


def apply_packages(text: str, packages: list[dict], *, allow_empty: bool = False) -> str:
    eol = detect_eol(text)
    text = ensure_markers(text, eol)
    s, e = text.index(START), text.index(END) + len(END)
    existing = text[s:e]
    if not packages and ENTRY_RE.search(existing) and not allow_empty:
        raise UpdateError(
            "Refusing to replace an existing package list with an empty one. If this is intended "
            "(e.g. every package was archived), re-run with allow_empty / ALLOW_EMPTY_LIST=true."
        )
    return text[:s] + render_block(packages, eol) + text[e:]


def write_if_changed(path: Path, new_text: str, old_text: str) -> bool:
    if new_text == old_text:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".readme-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


# --------------------------------------------------------------------------- #
# Data file validation (the JSON travels between workflow jobs)
# --------------------------------------------------------------------------- #
_REQUIRED = ("name", "display_name", "repo_url", "tag", "release_url")


def load_packages(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Cannot read package data from {path}: {exc}") from exc
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise UpdateError(f'{path} must contain an object with a "packages" list.')
    for pkg in packages:
        if not isinstance(pkg, dict) or any(not isinstance(pkg.get(k), str) or not pkg[k] for k in _REQUIRED):
            raise UpdateError(f"Malformed package entry in {path}: {pkg!r}")
        if not isinstance(pkg.get("description", ""), str):
            raise UpdateError(f"Malformed description in {path}: {pkg!r}")
    return packages


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def policy_from_env() -> Policy:
    return Policy(
        include_prereleases=env_bool("INCLUDE_PRERELEASES"),
        include_archived=env_bool("INCLUDE_ARCHIVED"),
        include_forks=env_bool("INCLUDE_FORKS"),
        required_topic=os.environ.get("REQUIRED_TOPIC", "").strip(),
    )


def do_fetch(org: str, out: Path) -> list[dict]:
    client = GitHubClient(os.environ.get("GITHUB_TOKEN") or None, os.environ.get("GITHUB_API_URL", DEFAULT_API_URL))
    if not client.token:
        print("Warning: GITHUB_TOKEN not set; using unauthenticated (low) rate limits.", file=sys.stderr)
    packages = discover_packages(client, org, policy_from_env())
    out.write_text(json.dumps({"org": org, "packages": packages}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(packages)} package(s) to {out}.")
    return packages


def do_apply(readme: Path, packages: list[dict], allow_empty: bool) -> bool:
    old = readme.read_bytes().decode("utf-8")
    new = apply_packages(old, packages, allow_empty=allow_empty)
    changed = write_if_changed(readme, new, old)
    print(f"{readme}: {'updated' if changed else 'already up to date'} ({len(packages)} package(s)).")
    return changed


def write_step_summary(packages: list[dict]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = ["### Published packages found", ""]
    lines += [f"- `{p['name']}` {p['tag']}" for p in packages] or ["_none_"]
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def org_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--org", default=os.environ.get("GITHUB_REPOSITORY_OWNER"), help="Organization login.")

    def readme_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--readme", type=Path, default=Path(DEFAULT_README))
        p.add_argument(
            "--allow-empty", action="store_true", default=env_bool("ALLOW_EMPTY_LIST"),
            help="Permit replacing a non-empty list with an empty one.",
        )

    fetch = sub.add_parser("fetch", help="Query the API and write a JSON package list.")
    org_arg(fetch)
    fetch.add_argument("--out", type=Path, default=Path("packages.json"))

    apply_ = sub.add_parser("apply", help="Render a JSON package list into the README.")
    apply_.add_argument("--data", type=Path, default=Path("packages.json"))
    readme_args(apply_)

    run = sub.add_parser("run", help="fetch + apply in one step.")
    org_arg(run)
    readme_args(run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command in ("fetch", "run") and not args.org:
            raise UpdateError("Organization not set: pass --org or set GITHUB_REPOSITORY_OWNER.")
        if args.command == "fetch":
            write_step_summary(do_fetch(args.org, args.out))
        elif args.command == "apply":
            do_apply(args.readme, load_packages(args.data), args.allow_empty)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                packages = do_fetch(args.org, Path(tmp) / "packages.json")
            write_step_summary(packages)
            do_apply(args.readme, packages, args.allow_empty)
    except UpdateError as exc:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=Package list update failed::{str(exc).splitlines()[0]}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
