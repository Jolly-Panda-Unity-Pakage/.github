"""Tests for scripts/update_packages.py.

Uses a real local HTTP server that mimics the GitHub REST API (including
`Link: rel="next"` pagination, 5xx and rate-limit responses), so the HTTP,
pagination and retry code is exercised end to end. Run with:

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update_packages as up  # noqa: E402

ORG = "acme"
PAGE_SIZE = 2  # tiny pages so pagination is always exercised


def repo(name, **kw):
    base = {
        "name": name, "html_url": f"https://github.com/{ORG}/{name}", "description": f"{name} description",
        "private": False, "visibility": "public", "archived": False, "fork": False, "disabled": False,
        "topics": [], "owner": {"login": ORG},
    }
    base.update(kw)
    return base


def rel(tag, published="2026-01-01T00:00:00Z", **kw):
    base = {
        "tag_name": tag, "html_url": f"https://github.com/{ORG}/x/releases/tag/{tag}",
        "draft": False, "prerelease": False, "published_at": published,
    }
    base.update(kw)
    return base


class FakeGitHub:
    """Programmable fake API. `routes` maps path -> list of items or callable(handler)->(status, body, headers)."""

    def __init__(self):
        self.routes, self.hits = {}, {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                outer.hits[parsed.path] = outer.hits.get(parsed.path, 0) + 1
                route = outer.routes.get(parsed.path)
                if route is None:
                    return self._send(404, {"message": "Not Found"})
                if callable(route):
                    status, body, headers = route(outer.hits[parsed.path])
                    return self._send(status, body, headers)
                page = int(parse_qs(parsed.query).get("page", ["1"])[0])
                chunk = route[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
                headers = {}
                if page * PAGE_SIZE < len(route):
                    headers["Link"] = f'<{outer.url}{parsed.path}?page={page + 1}>; rel="next"'
                self._send(200, chunk, headers)

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def client(self, **kw):
        return up.GitHubClient("token", self.url, sleep=lambda s: None, **kw)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.addCleanup(self.api.close)
        r = self.api.routes
        r[f"/orgs/{ORG}/repos"] = [
            repo("zeta-tool"),                                    # eligible (many release pages)
            repo("audio-import-optimizer", description=None),     # eligible, missing description
            repo("no-releases"),                                  # no releases
            repo("only-draft"),                                   # draft only
            repo("only-prerelease"),                              # prerelease only
            repo("old-archived", archived=True),                  # archived
            repo("a-fork", fork=True),                            # fork
            repo(".github"),                                      # profile repo itself
            repo("secret", private=True, visibility="private"),   # private
            repo("MixedCase_pkg"),                                # eligible, odd name
        ]
        r[f"/repos/{ORG}/zeta-tool/releases"] = (
            [rel(f"v0.{i}", f"2026-01-{i + 1:02d}T00:00:00Z") for i in range(5)]
            + [rel("v9-draft", "2026-03-01T00:00:00Z", draft=True), rel("v2.0.0-rc1", "2026-04-01T00:00:00Z", prerelease=True),
               rel("v1.0.0", "2026-02-01T00:00:00Z")]
        )
        r[f"/repos/{ORG}/audio-import-optimizer/releases"] = [rel("v1.2.0")]
        r[f"/repos/{ORG}/no-releases/releases"] = []
        r[f"/repos/{ORG}/only-draft/releases"] = [rel("v1", draft=True, published_at=None)]
        r[f"/repos/{ORG}/only-prerelease/releases"] = [rel("v1-beta", prerelease=True)]
        r[f"/repos/{ORG}/MixedCase_pkg/releases"] = [rel("release/1.0 beta")]
        for gone in ("old-archived", "a-fork", ".github", "secret"):
            r[f"/repos/{ORG}/{gone}/releases"] = [rel("v1")]  # would be eligible if the repo were

    def run_discovery(self, **policy):
        with contextlib.redirect_stdout(io.StringIO()):
            return up.discover_packages(self.api.client(), ORG, up.Policy(**policy))

    def test_filters_sorting_and_latest_release(self):
        pkgs = self.run_discovery()
        self.assertEqual([p["name"] for p in pkgs], ["audio-import-optimizer", "MixedCase_pkg", "zeta-tool"])
        self.assertEqual([p["display_name"] for p in pkgs], ["Audio Import Optimizer", "MixedCase Pkg", "Zeta Tool"])
        zeta = pkgs[2]
        self.assertEqual(zeta["tag"], "v1.0.0")  # not the draft, not the newer prerelease, found on a later page
        self.assertGreater(self.api.hits[f"/repos/{ORG}/zeta-tool/releases"], 1)   # release pagination
        self.assertGreater(self.api.hits[f"/orgs/{ORG}/repos"], 1)                 # repo pagination
        self.assertNotIn(f"/repos/{ORG}/old-archived/releases", self.api.hits)     # never queried

    def test_policy_flags(self):
        names = lambda **kw: {p["name"] for p in self.run_discovery(**kw)}
        self.assertIn("only-prerelease", names(include_prereleases=True))
        self.assertEqual(
            [p["tag"] for p in self.run_discovery(include_prereleases=True) if p["name"] == "zeta-tool"], ["v2.0.0-rc1"]
        )
        self.assertIn("old-archived", names(include_archived=True))
        self.assertIn("a-fork", names(include_forks=True))
        self.assertNotIn("secret", names(include_archived=True, include_forks=True))  # private never listed

    def test_required_topic(self):
        self.api.routes[f"/orgs/{ORG}/repos"][0]["topics"] = ["Unity"]
        self.assertEqual([p["name"] for p in self.run_discovery(required_topic="unity")], ["zeta-tool"])

    def test_missing_description_fallback(self):
        pkg = [p for p in self.run_discovery() if p["name"] == "audio-import-optimizer"][0]
        self.assertEqual(pkg["description"], "")
        self.assertIn(up.MISSING_DESCRIPTION, "\n".join(up.render_entry(pkg)))


class ApiFailureTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.addCleanup(self.api.close)

    def discover(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return up.discover_packages(self.api.client(), ORG, up.Policy())

    def test_transient_5xx_is_retried(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (502, {"message": "bad gateway"}, {}) if n < 3 else (200, [], {})
        self.assertEqual(self.discover(), [])
        self.assertEqual(self.api.hits[f"/orgs/{ORG}/repos"], 3)

    def test_persistent_5xx_raises(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (500, {"message": "boom"}, {})
        with self.assertRaisesRegex(up.UpdateError, "HTTP 500"):
            self.discover()

    def test_rate_limit_retries_then_succeeds(self):
        limited = {"x-ratelimit-remaining": "0", "retry-after": "1"}
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (403, {"message": "rate limit exceeded"}, limited) if n == 1 else (200, [], {})
        self.assertEqual(self.discover(), [])

    def test_rate_limit_with_long_wait_fails(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (403, {"message": "rate limit"}, {"retry-after": "3600"})
        with self.assertRaisesRegex(up.UpdateError, "HTTP 403"):
            self.discover()

    def test_auth_and_not_found_are_not_retried(self):
        for status in (401, 404):
            self.api.hits.clear()
            self.api.routes[f"/orgs/{ORG}/repos"] = lambda n, s=status: (s, {"message": "nope"}, {})
            with self.assertRaisesRegex(up.UpdateError, f"HTTP {status}"):
                self.discover()
            self.assertEqual(self.api.hits[f"/orgs/{ORG}/repos"], 1)

    def test_release_failure_aborts_whole_run(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = [repo("a"), repo("b")]
        self.api.routes[f"/repos/{ORG}/a/releases"] = [rel("v1")]
        self.api.routes[f"/repos/{ORG}/b/releases"] = lambda n: (500, {"message": "boom"}, {})
        with self.assertRaises(up.UpdateError):  # no partial list is ever returned
            self.discover()

    def test_non_list_response_is_rejected(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (200, {"message": "surprise"}, {})
        with self.assertRaisesRegex(up.UpdateError, "Expected a JSON list"):
            self.discover()

    def test_pagination_link_to_foreign_host_is_refused(self):
        self.api.routes[f"/orgs/{ORG}/repos"] = lambda n: (200, [], {"Link": '<https://evil.example/x>; rel="next"'})
        with self.assertRaisesRegex(up.UpdateError, "unexpected host"):
            self.discover()


README = (
    "# Title\n\nIntro with trailing spaces  \n\n## 🚀 Our Packages\n\n"
    "{BLOCK}\n\n### 🎵 Hand written\n\nKeep me.\n\n---\n\n## Other\n\n<b>html</b>\n"
)
PKG_A = {"name": "a", "display_name": "Alpha", "description": "Alpha *tool*", "repo_url": f"https://github.com/{ORG}/a",
         "tag": "v1.0.0", "release_url": f"https://github.com/{ORG}/a/releases/tag/v1.0.0"}
PKG_B = dict(PKG_A, name="b", display_name="Beta", description="", tag="v2_0", release_url=f"https://github.com/{ORG}/b/releases/tag/v2_0",
             repo_url=f"https://github.com/{ORG}/b")


def readme(block=None, eol="\n"):
    text = README.replace("{BLOCK}", block if block is not None else f"{up.START}\n{up.END}")
    return text.replace("\n", eol)


class ReadmeTests(unittest.TestCase):
    def test_idempotent(self):
        once = up.apply_packages(readme(), [PKG_A, PKG_B])
        twice = up.apply_packages(once, [PKG_A, PKG_B])
        self.assertEqual(once, twice)

    def test_outside_markers_untouched_lf_and_crlf(self):
        for eol in ("\n", "\r\n"):
            src = readme(eol=eol)
            out = up.apply_packages(src, [PKG_A, PKG_B])
            s, e = src.index(up.START), src.index(up.END) + len(up.END)
            os_, oe = out.index(up.START), out.index(up.END) + len(up.END)
            self.assertEqual(out[:os_], src[:s])
            self.assertEqual(out[oe:], src[e:])
            body = out[os_:oe]
            self.assertEqual(body.count("\n"), body.count("\r\n") if eol == "\r\n" else body.count("\n"))
            self.assertNotIn("\r\r", out)
            if eol == "\n":
                self.assertNotIn("\r", out)

    def test_render_content(self):
        out = up.apply_packages(readme(), [PKG_A, PKG_B])
        self.assertIn("### 📦 [Alpha](https://github.com/acme/a)", out)
        self.assertIn(r"Alpha \*tool\*", out)                      # escaped
        self.assertIn("[v2\\_0](https://github.com/acme/b/releases/tag/v2_0)", out)
        self.assertIn("archive/refs/tags/v1.0.0.zip", out)
        self.assertIn(up.MISSING_DESCRIPTION, out)

    def test_empty_message(self):
        self.assertIn(up.EMPTY_MESSAGE, up.apply_packages(readme(), []))

    def test_refuses_to_erase_existing_list(self):
        filled = up.apply_packages(readme(), [PKG_A])
        with self.assertRaisesRegex(up.UpdateError, "empty one"):
            up.apply_packages(filled, [])
        self.assertIn(up.EMPTY_MESSAGE, up.apply_packages(filled, [], allow_empty=True))

    def test_markers_inserted_when_missing_without_losing_content(self):
        src = "# T\n\n## 🚀 Our Packages\n\n### Manual entry\n\nText\n"
        out = up.apply_packages(src, [PKG_A])
        self.assertTrue(out.startswith("# T\n\n## 🚀 Our Packages\n\n" + up.START))
        self.assertTrue(out.endswith(up.END + "\n\n### Manual entry\n\nText\n"))
        crlf = up.apply_packages(src.replace("\n", "\r\n"), [PKG_A])
        self.assertNotIn("\r\r", crlf)
        self.assertEqual(crlf.replace("\r\n", "\n"), out)

    def test_marker_problems_fail_safely(self):
        for bad in (f"## Our Packages\n{up.START}\n", f"{up.START}\n{up.START}\n{up.END}\n", f"{up.END}\n{up.START}\n", "# nothing here\n"):
            with self.assertRaises(up.UpdateError):
                up.apply_packages(bad, [PKG_A])

    def test_escaping(self):
        nasty = "<script>alert(1)</script> [x](http://e) `c` | a_b\n# h\n- item"
        escaped = up.md_escape(nasty)
        self.assertNotIn("\n", escaped)
        for raw in ("<script>", "[x](", "`c`"):
            self.assertNotIn(raw, escaped)
        self.assertEqual(up.md_escape("- starts like a list"), "\\- starts like a list")
        self.assertEqual(up.md_escape("1. numbered"), "1\\. numbered")
        with self.assertRaises(up.UpdateError):
            up.safe_url("javascript:alert(1)")
        self.assertNotIn(")", up.safe_url("https://x/a(b)c"))
        self.assertLessEqual(len(up.clean_description("x" * 1000)), up.MAX_DESCRIPTION_CHARS)

    def test_real_profile_readme(self):
        src = (ROOT / "profile" / "README.md").read_bytes().decode("utf-8")
        self.assertEqual(src.count(up.START), 1)
        out = up.apply_packages(src, [PKG_A, PKG_B])
        s, e = src.index(up.START), src.index(up.END) + len(up.END)
        o, oe = out.index(up.START), out.index(up.END) + len(up.END)
        self.assertEqual((out[:o], out[oe:]), (src[:s], src[e:]))
        self.assertEqual(up.apply_packages(out, [PKG_A, PKG_B]), out)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.addCleanup(self.api.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.readme = self.dir / "README.md"
        self.readme.write_bytes(readme().encode())
        env = {"GITHUB_TOKEN": "t", "GITHUB_API_URL": self.api.url}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = unittest.mock.patch.object(up.time, "sleep", lambda s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        for var in ("INCLUDE_PRERELEASES", "INCLUDE_ARCHIVED", "ALLOW_EMPTY_LIST", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(var, None)
        self.api.routes[f"/orgs/{ORG}/repos"] = [repo("a"), repo("b"), repo("c")]
        self.api.routes[f"/repos/{ORG}/a/releases"] = [rel("v1")]
        self.api.routes[f"/repos/{ORG}/b/releases"] = []
        self.api.routes[f"/repos/{ORG}/c/releases"] = [rel("v3")]

    def main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return up.main(list(args))

    def test_fetch_then_apply_twice_no_diff(self):
        data = self.dir / "p.json"
        self.assertEqual(self.main("fetch", "--org", ORG, "--out", str(data)), 0)
        self.assertEqual(self.main("apply", "--data", str(data), "--readme", str(self.readme)), 0)
        first = self.readme.read_bytes()
        self.assertIn(b"### \xf0\x9f\x93\xa6 [A](", first)
        self.assertEqual(self.main("apply", "--data", str(data), "--readme", str(self.readme)), 0)
        self.assertEqual(self.readme.read_bytes(), first)
        self.assertEqual(self.main("run", "--org", ORG, "--readme", str(self.readme)), 0)  # fresh API call, same result
        self.assertEqual(self.readme.read_bytes(), first)

    def test_api_failure_leaves_readme_untouched_and_exits_nonzero(self):
        self.assertEqual(self.main("run", "--org", ORG, "--readme", str(self.readme)), 0)
        before = self.readme.read_bytes()
        self.api.routes[f"/repos/{ORG}/c/releases"] = lambda n: (500, {"message": "boom"}, {})
        self.assertEqual(self.main("run", "--org", ORG, "--readme", str(self.readme)), 1)
        self.assertEqual(self.readme.read_bytes(), before)

    def test_empty_org_does_not_wipe_existing_list(self):
        self.main("run", "--org", ORG, "--readme", str(self.readme))
        before = self.readme.read_bytes()
        self.api.routes[f"/orgs/{ORG}/repos"] = []  # e.g. a bad response that happens to look valid
        self.assertEqual(self.main("run", "--org", ORG, "--readme", str(self.readme)), 1)
        self.assertEqual(self.readme.read_bytes(), before)
        self.assertEqual(self.main("run", "--org", ORG, "--readme", str(self.readme), "--allow-empty"), 0)
        self.assertIn(up.EMPTY_MESSAGE.encode(), self.readme.read_bytes())

    def test_malformed_data_file_rejected(self):
        bad = self.dir / "bad.json"
        bad.write_text('{"packages": [{"name": "x"}]}')
        before = self.readme.read_bytes()
        self.assertEqual(self.main("apply", "--data", str(bad), "--readme", str(self.readme)), 1)
        self.assertEqual(self.readme.read_bytes(), before)


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
