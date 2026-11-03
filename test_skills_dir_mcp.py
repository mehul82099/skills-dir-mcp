#!/usr/bin/env python3
"""Tests for skills-dir-mcp (stdlib unittest, no network).

Spawns the server as a subprocess and speaks newline-delimited JSON-RPC
over its stdio, exactly as an MCP client would. Each test class builds
its own skills directory fixture in a temp dir.
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "skills_dir_mcp.py")

SKILLS_EXT = "io.modelcontextprotocol/skills"


def meta(version="2026-07-28", skills=True):
    caps = {"extensions": {SKILLS_EXT: {}}} if skills else {}
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": caps,
    }


def p(**kw):
    return {"_meta": meta(**kw)}


class Client:
    """A minimal MCP stdio client for tests."""

    def __init__(self, skills_dir, extra_args=None):
        args = [sys.executable, SERVER, "--skills-dir", skills_dir]
        self.proc = subprocess.Popen(
            args + (extra_args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=HERE)
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply
        raise AssertionError("no reply for %s" % method)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def write(path, text=None, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if data is not None:
        with open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


GIT_SKILL = """---
name: git-workflow
description: Follow this team's Git conventions for branching and commits
license: MIT
metadata:
  author: example-org
  version: "1.0"
---
# Git workflow

Always branch from main. See references/BRANCHING.md.
"""

PDF_SKILL = """---
name: pdf-processing
description: Extract, fill, and assemble PDF documents
metadata:
  version: "2.1.0"
allowed-tools: Bash Read
---
# PDF processing

Pick a template from templates/ and run scripts/extract.py.
"""

NESTED_SKILL = """---
name: refunds
description: Process customer refund requests per company policy
---
# Refunds
"""


def build_skills_root(tmp):
    root = os.path.join(tmp, "skills")
    write(os.path.join(root, "git-workflow", "SKILL.md"), GIT_SKILL)
    write(os.path.join(root, "git-workflow", "references", "BRANCHING.md"),
          "# Branching\n\nUse feature branches.\n")
    write(os.path.join(root, "pdf-processing", "SKILL.md"), PDF_SKILL)
    write(os.path.join(root, "pdf-processing", "scripts", "extract.py"),
          "print('extract')\n")
    write(os.path.join(root, "pdf-processing", "templates", "invoice.md"),
          "# Invoice template\n")
    write(os.path.join(root, "pdf-processing", "templates", "regional",
                       "eu-invoice.md"), "# EU invoice\n")
    write(os.path.join(root, "pdf-processing", "assets", "logo.bin"),
          data=b"\x00\x01\x02\xff binary \x00")
    write(os.path.join(root, "acme", "billing", "refunds", "SKILL.md"),
          NESTED_SKILL)
    write(os.path.join(root, "acme", "billing", "refunds", "examples",
                       "email.md"), "# Refund email\n")
    return root


class Base(unittest.TestCase):
    extra_args = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = build_skills_root(self.tmp.name)
        self.client = Client(self.root, self.extra_args)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def skills_list(self, **kw):
        reply = self.client.call("skills/list", p(**kw))
        self.assertNotIn("error", reply, reply)
        return reply["result"]


class TestDiscoveryAndListing(Base):
    def test_discover_declares_extension(self):
        reply = self.client.call("server/discover", p(skills=False))
        caps = reply["result"]["capabilities"]
        self.assertIn("resources", caps)
        ext = caps["extensions"][SKILLS_EXT]
        self.assertEqual(ext, {"directoryRead": True})
        self.assertIn("2026-07-28", reply["result"]["supportedVersions"])

    def test_skills_list_entries(self):
        result = self.skills_list()
        self.assertEqual(result["ttlMs"], 30000)
        self.assertEqual(result["cacheScope"], "public")
        uris = {s["uri"] for s in result["skills"]}
        self.assertIn("skill://git-workflow/SKILL.md", uris)
        self.assertIn("skill://pdf-processing/SKILL.md", uris)
        self.assertIn("skill://acme/billing/refunds/SKILL.md", uris)

    def test_frontmatter_verbatim(self):
        result = self.skills_list()
        entry = next(s for s in result["skills"]
                     if s["uri"] == "skill://git-workflow/SKILL.md")
        fm = entry["frontmatter"]
        self.assertEqual(fm["name"], "git-workflow")
        self.assertTrue(fm["description"].startswith("Follow this team"))
        self.assertEqual(fm["license"], "MIT")
        self.assertEqual(fm["metadata"],
                         {"author": "example-org", "version": "1.0"})

    def test_resources_digests_and_sizes(self):
        result = self.skills_list()
        entry = next(s for s in result["skills"]
                     if s["uri"] == "skill://git-workflow/SKILL.md")
        by_uri = {r["uri"]: r for r in entry["resources"]}
        self.assertEqual(set(by_uri),
                         {"skill://git-workflow/SKILL.md",
                          "skill://git-workflow/references/BRANCHING.md"})
        md = os.path.join(self.root, "git-workflow", "SKILL.md")
        with open(md, "rb") as fh:
            raw = fh.read()
        item = by_uri["skill://git-workflow/SKILL.md"]
        self.assertEqual(item["size"], len(raw))
        self.assertEqual(item["digest"],
                         "sha256:" + hashlib.sha256(raw).hexdigest())

    def test_gating_without_extension(self):
        reply = self.client.call("skills/list", p(skills=False))
        self.assertEqual(reply["error"]["code"], -32021)
        reply = self.client.call("skills/get",
                                 dict(p(skills=False),
                                      uri="skill://git-workflow/SKILL.md"))
        self.assertEqual(reply["error"]["code"], -32021)
        reply = self.client.call("resources/directory/read",
                                 dict(p(skills=False),
                                      uri="skill://git-workflow"))
        self.assertEqual(reply["error"]["code"], -32021)

    def test_gating_legacy_version(self):
        self.client.call("initialize",
                         {"protocolVersion": "2025-11-25",
                          "capabilities": {}})
        reply = self.client.call("skills/list", {})
        self.assertEqual(reply["error"]["code"], -32021)
        # base protocol still works for legacy clients
        reply = self.client.call(
            "resources/read", {"uri": "skill://git-workflow/SKILL.md"})
        self.assertNotIn("error", reply, reply)

    def test_unsupported_version(self):
        reply = self.client.call("skills/list", p(version="2099-01-01"))
        self.assertEqual(reply["error"]["code"], -32022)

    def test_missing_version_before_handshake(self):
        reply = self.client.call("skills/list", {})
        self.assertEqual(reply["error"]["code"], -32602)


class TestGet(Base):
    def test_get_returns_listing_entry(self):
        listed = {s["uri"]: s for s in self.skills_list()["skills"]}
        uri = "skill://pdf-processing/SKILL.md"
        reply = self.client.call("skills/get", dict(p(), uri=uri))
        self.assertNotIn("error", reply, reply)
        self.assertEqual(reply["result"]["skill"], listed[uri])

    def test_get_unknown_uri(self):
        reply = self.client.call(
            "skills/get", dict(p(), uri="skill://nope/SKILL.md"))
        self.assertEqual(reply["error"]["code"], -32602)
        reply = self.client.call(
            "skills/get", dict(p(), uri="skill://git-workflow"))
        self.assertEqual(reply["error"]["code"], -32602)
        reply = self.client.call(
            "skills/get", dict(p(), uri="file:///etc/passwd"))
        self.assertEqual(reply["error"]["code"], -32602)


class TestRead(Base):
    def test_read_text(self):
        reply = self.client.call(
            "resources/read", dict(p(skills=False),
                                   uri="skill://git-workflow/SKILL.md"))
        content = reply["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/markdown")
        self.assertIn("# Git workflow", content["text"])
        self.assertNotIn("blob", content)

    def test_read_binary_blob(self):
        uri = "skill://pdf-processing/assets/logo.bin"
        reply = self.client.call(
            "resources/read", dict(p(skills=False), uri=uri))
        content = reply["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "application/octet-stream")
        self.assertEqual(base64.b64decode(content["blob"]),
                         b"\x00\x01\x02\xff binary \x00")

    def test_read_unknown_and_traversal(self):
        for uri in ("skill://git-workflow/NOPE.md",
                    "skill://git-workflow/../../etc/passwd",
                    "skill://git-workflow/..%2F..%2Fetc%2Fpasswd",
                    "skill://", "https://evil.test/x", "skill://a//b",
                    "skill:///absolute"):
            reply = self.client.call("resources/read",
                                     dict(p(skills=False), uri=uri))
            self.assertEqual(reply["error"]["code"], -32602, uri)

    def test_read_supporting_file(self):
        uri = "skill://pdf-processing/scripts/extract.py"
        reply = self.client.call(
            "resources/read", dict(p(skills=False), uri=uri))
        content = reply["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/x-python")
        self.assertEqual(content["text"], "print('extract')\n")


class TestDirectoryRead(Base):
    def test_skill_root_children(self):
        reply = self.client.call("resources/directory/read",
                                 dict(p(), uri="skill://pdf-processing"))
        children = {r["uri"]: r for r in reply["result"]["resources"]}
        self.assertEqual(
            set(children),
            {"skill://pdf-processing/SKILL.md",
             "skill://pdf-processing/scripts",
             "skill://pdf-processing/templates",
             "skill://pdf-processing/assets"})
        self.assertEqual(children["skill://pdf-processing/scripts"]
                         ["mimeType"], "inode/directory")
        self.assertEqual(children["skill://pdf-processing/SKILL.md"]
                         ["mimeType"], "text/markdown")

    def test_subdirectory_children(self):
        reply = self.client.call(
            "resources/directory/read",
            dict(p(), uri="skill://pdf-processing/templates"))
        children = {r["uri"]: r for r in reply["result"]["resources"]}
        self.assertEqual(
            set(children),
            {"skill://pdf-processing/templates/invoice.md",
             "skill://pdf-processing/templates/regional"})
        self.assertEqual(children["skill://pdf-processing/templates/regional"]
                         ["mimeType"], "inode/directory")

    def test_not_a_directory(self):
        reply = self.client.call(
            "resources/directory/read",
            dict(p(), uri="skill://pdf-processing/SKILL.md"))
        self.assertEqual(reply["error"]["code"], -32602)
        reply = self.client.call(
            "resources/directory/read", dict(p(), uri="skill://missing"))
        self.assertEqual(reply["error"]["code"], -32602)
        reply = self.client.call(
            "resources/directory/read",
            dict(p(), uri="skill://pdf-processing/"))
        self.assertEqual(reply["error"]["code"], -32602)


class TestResourcesList(Base):
    def test_lists_all_files(self):
        reply = self.client.call("resources/list", p(skills=False))
        uris = {r["uri"] for r in reply["result"]["resources"]}
        self.assertIn("skill://acme/billing/refunds/examples/email.md", uris)
        self.assertIn("skill://pdf-processing/assets/logo.bin", uris)
        self.assertEqual(reply["result"]["cacheScope"], "public")

    def test_skill_md_resource_metadata(self):
        reply = self.client.call("resources/list", p(skills=False))
        by_uri = {r["uri"]: r for r in reply["result"]["resources"]}
        md = by_uri["skill://git-workflow/SKILL.md"]
        self.assertEqual(md["name"], "git-workflow")
        self.assertEqual(md["description"],
                         "Follow this team's Git conventions for "
                         "branching and commits")
        self.assertEqual(md["mimeType"], "text/markdown")


class TestPagination(unittest.TestCase):
    def test_cursor_pagination(self):
        tmp = tempfile.TemporaryDirectory()
        root = build_skills_root(tmp.name)
        with Client(root, ["--page-size", "2"]) as client:
            seen, cursor = [], None
            for _ in range(5):
                params = p()
                if cursor is not None:
                    params["cursor"] = cursor
                reply = client.call("skills/list", params)
                result = reply["result"]
                seen.extend(s["uri"] for s in result["skills"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
            self.assertEqual(len(seen), 3)
            self.assertEqual(len(set(seen)), 3)
        tmp.cleanup()

    def test_invalid_cursor(self):
        tmp = tempfile.TemporaryDirectory()
        root = build_skills_root(tmp.name)
        with Client(root) as client:
            reply = client.call("skills/list", dict(p(), cursor="bogus"))
            self.assertEqual(reply["error"]["code"], -32602)
        tmp.cleanup()


class TestInvalidSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "skills")
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.close()
        self.tmp.cleanup()

    def listed_uris(self):
        self.client = Client(self.root)
        reply = self.client.call("skills/list", p())
        return {s["uri"] for s in reply["result"]["skills"]}

    def test_name_mismatch_skipped(self):
        write(os.path.join(self.root, "good-skill", "SKILL.md"),
              "---\nname: good-skill\ndescription: ok\n---\nbody\n")
        write(os.path.join(self.root, "other-dir", "SKILL.md"),
              "---\nname: not-other-dir\ndescription: bad\n---\nbody\n")
        uris = self.listed_uris()
        self.assertIn("skill://good-skill/SKILL.md", uris)
        self.assertNotIn("skill://other-dir/SKILL.md", uris)

    def test_invalid_names_skipped(self):
        for bad in ("Bad-Name", "trail-", "-lead", "double--hyphen"):
            write(os.path.join(self.root, bad, "SKILL.md"),
                  "---\nname: %s\ndescription: x\n---\n" % bad)
        self.assertEqual(self.listed_uris(), set())

    def test_missing_description_skipped(self):
        write(os.path.join(self.root, "no-desc", "SKILL.md"),
              "---\nname: no-desc\n---\nbody\n")
        self.assertEqual(self.listed_uris(), set())

    def test_root_level_skill_skipped(self):
        write(os.path.join(self.root, "SKILL.md"),
              "---\nname: stray\ndescription: x\n---\n")
        write(os.path.join(self.root, "fine", "SKILL.md"),
              "---\nname: fine\ndescription: ok\n---\n")
        self.assertEqual(self.listed_uris(),
                         {"skill://fine/SKILL.md"})

    def test_over_resource_limit_skipped(self):
        write(os.path.join(self.root, "huge", "SKILL.md"),
              "---\nname: huge\ndescription: too many files\n---\n")
        for i in range(513):
            write(os.path.join(self.root, "huge", "f%03d.txt" % i), "x")
        self.assertEqual(self.listed_uris(), set())

    def test_malformed_frontmatter_skipped(self):
        write(os.path.join(self.root, "broken", "SKILL.md"),
              "---\nname: [unclosed\ndescription: x\n---\n")
        write(os.path.join(self.root, "nofm", "SKILL.md"),
              "no frontmatter at all\n")
        self.assertEqual(self.listed_uris(), set())

    def test_missing_dir_empty_catalog(self):
        with Client(os.path.join(self.tmp.name, "missing")) as client:
            reply = client.call("skills/list", p())
            self.assertEqual(reply["result"]["skills"], [])


class TestFrontmatterParser(unittest.TestCase):
    def parse(self, text):
        sys.path.insert(0, HERE)
        import skills_dir_mcp
        return skills_dir_mcp.parse_frontmatter(text)[0]

    def test_scalars_and_quotes(self):
        fm = self.parse("---\nname: a\ndescription: 'it''s ok'\n"
                        "license: \"quoted: value\"\ncount: 3\n"
                        "ratio: 1.5\nflag: true\nnothing: null\n---\n")
        self.assertEqual(fm["description"], "it's ok")
        self.assertEqual(fm["license"], "quoted: value")
        self.assertEqual(fm["count"], 3)
        self.assertEqual(fm["ratio"], 1.5)
        self.assertIs(fm["flag"], True)
        self.assertIsNone(fm["nothing"])

    def test_lists(self):
        fm = self.parse("---\nname: a\ntags:\n  - one\n  - two\n"
                        "inline: [x, 'y', 3]\n---\n")
        self.assertEqual(fm["tags"], ["one", "two"])
        self.assertEqual(fm["inline"], ["x", "y", 3])

    def test_block_scalar(self):
        fm = self.parse("---\nname: a\ndescription: |\n"
                        "  line one\n  line two\n---\nbody\n")
        self.assertEqual(fm["description"], "line one\nline two\n")

    def test_nested_metadata(self):
        fm = self.parse("---\nname: a\nmetadata:\n  author: org\n"
                        "  nested:\n    deep: '1'\n---\n")
        self.assertEqual(fm["metadata"],
                         {"author": "org", "nested": {"deep": "1"}})


class TestMisc(Base):
    def test_ping_and_unknown_method(self):
        reply = self.client.call("ping", p())
        self.assertNotIn("error", reply, reply)
        reply = self.client.call("skills/cancel", p())
        self.assertEqual(reply["error"]["code"], -32601)

    def test_percent_encoded_filename(self):
        write(os.path.join(self.root, "git-workflow", "my notes.md"),
              "# notes\n")
        reply = self.client.call("skills/get",
                                 dict(p(), uri="skill://git-workflow/SKILL.md"))
        uris = {r["uri"] for r in reply["result"]["skill"]["resources"]}
        encoded = "skill://git-workflow/my%20notes.md"
        self.assertIn(encoded, uris)
        reply = self.client.call("resources/read",
                                 dict(p(skills=False), uri=encoded))
        self.assertEqual(reply["result"]["contents"][0]["text"], "# notes\n")

    @unittest.skipUnless(os.name == "posix", "posix symlink test")
    def test_symlink_escape_skipped(self):
        outside = os.path.join(self.tmp.name, "secret.txt")
        write(outside, "secret\n")
        os.symlink(outside, os.path.join(self.root, "git-workflow",
                                         "leak.txt"))
        reply = self.client.call("skills/get",
                                 dict(p(), uri="skill://git-workflow/SKILL.md"))
        uris = {r["uri"] for r in reply["result"]["skill"]["resources"]}
        self.assertNotIn("skill://git-workflow/leak.txt", uris)


if __name__ == "__main__":
    unittest.main()
