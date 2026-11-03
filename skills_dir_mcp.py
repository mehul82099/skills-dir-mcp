#!/usr/bin/env python3
"""skills-dir-mcp: serve a directory of Agent Skills over MCP.

Zero-dependency, single-file MCP server (stdio, newline-delimited
JSON-RPC) implementing the MCP Skills extension
(io.modelcontextprotocol/skills, SEP-2640) on the stateless 2026-07-28
core protocol (SEP-2575).

Each skill is a directory containing a SKILL.md with YAML frontmatter
(name, description, ...), per the Agent Skills specification. Every
file of a skill is exposed as an MCP resource under the skill:// URI
scheme; skills/list enumerates the catalog with SHA-256 digests and
sizes, skills/get returns one skill's entry by URI, and
resources/directory/read lists a directory's direct children.

Stdlib only. Python 3.9+.
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import urllib.parse

PROTOCOL_CURRENT = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18",
                      "2025-03-26", "2024-11-05"]
SKILLS_EXT = "io.modelcontextprotocol/skills"
SERVER_INFO = {"name": "skills-dir-mcp", "version": "1.0.0"}

E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603
E_MISSING_CLIENT_CAP = -32021  # spec 2026-07-28 error table
E_UNSUPPORTED_VERSION = -32022

MAX_RESOURCES_PER_SKILL = 512        # SEP-2640 limits
MAX_SKILL_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FRONTMATTER_LINES = 1024

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".py": "text/x-python",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
    ".txt": "text/plain",
    ".sh": "text/x-shellscript",
}


def warn(msg):
    sys.stderr.write("skills-dir-mcp: %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Minimal YAML frontmatter parser (stdlib subset).
#
# Supports the YAML used by Agent Skills frontmatter: flat and nested
# mappings by 2-space indentation, plain/quoted scalars, null/bool/int/
# float, block lists of scalars ("- item"), inline lists ("[a, b]"),
# and literal/folded block scalars (| and >). Anything outside this
# subset raises ValueError and the skill is skipped with a warning.
# ---------------------------------------------------------------------------

def _parse_scalar(raw):
    raw = raw.strip()
    if raw == "" or raw in ("~", "null", "Null", "NULL"):
        return None
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):
            raise ValueError("unterminated double-quoted scalar")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("bad double-quoted scalar: %s" % exc)
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise ValueError("unterminated single-quoted scalar")
        return raw[1:-1].replace("''", "'")
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise ValueError("unterminated inline list")
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline(inner)]
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _split_inline(inner):
    parts, depth, buf, quote = [], 0, [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


class _YamlLines:
    def __init__(self, text):
        self.rows = []
        for raw in text.split("\n"):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            if raw[:indent].replace(" ", ""):
                raise ValueError("tabs are not supported in frontmatter")
            self.rows.append((indent, raw.strip()))
        self.pos = 0

    def peek(self):
        return self.rows[self.pos] if self.pos < len(self.rows) else None

    def next(self):
        row = self.rows[self.pos]
        self.pos += 1
        return row


def _parse_block(yl, indent):
    row = yl.peek()
    if row is None or row[0] < indent:
        return None
    if row[1].startswith("- ") or row[1] == "-":
        return _parse_list(yl, indent)
    return _parse_map(yl, indent)


def _parse_block_scalar(yl, indent, folded):
    chunks = []
    while True:
        row = yl.peek()
        if row is None or row[0] <= indent:
            break
        yl.next()
        chunks.append(row[1] if folded else row[1])
    sep = " " if folded else "\n"
    return sep.join(chunks) + ("\n" if not folded else "")


def _parse_map(yl, indent):
    out = {}
    while True:
        row = yl.peek()
        if row is None or row[0] < indent:
            return out
        if row[0] > indent:
            raise ValueError("unexpected indentation at %r" % (row[1],))
        line = row[1]
        if line.startswith("- "):
            raise ValueError("mixed list and mapping at %r" % (line,))
        m = re.match(r"^([^:]+):\s*(.*)$", line)
        if not m:
            raise ValueError("expected 'key: value' at %r" % (line,))
        key = _parse_scalar(m.group(1))
        if not isinstance(key, str):
            raise ValueError("mapping keys must be strings")
        value = m.group(2)
        yl.next()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            out[key] = _parse_block_scalar(yl, indent, value.startswith(">"))
        elif value == "":
            nxt = yl.peek()
            if nxt is not None and nxt[0] > indent:
                out[key] = _parse_block(yl, nxt[0])
            else:
                out[key] = None
        else:
            out[key] = _parse_scalar(value)


def _parse_list(yl, indent):
    out = []
    while True:
        row = yl.peek()
        if row is None or row[0] < indent or not row[1].startswith("-"):
            return out
        if row[0] > indent:
            raise ValueError("unexpected indentation at %r" % (row[1],))
        item = row[1][1:].strip()
        yl.next()
        if item == "":
            nxt = yl.peek()
            if nxt is not None and nxt[0] > indent:
                out.append(_parse_block(yl, nxt[0]))
            else:
                out.append(None)
        else:
            out.append(_parse_scalar(item))


def parse_frontmatter(text):
    """Return (frontmatter_dict, body). Raises ValueError."""
    if not text.startswith("---"):
        raise ValueError("SKILL.md does not start with YAML frontmatter")
    lines = text.split("\n")
    if lines[0].strip() != "---":
        raise ValueError("frontmatter must open with a bare '---' line")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
        if i > MAX_FRONTMATTER_LINES:
            raise ValueError("frontmatter exceeds %d lines"
                             % MAX_FRONTMATTER_LINES)
    if end is None:
        raise ValueError("frontmatter is not closed with '---'")
    yl = _YamlLines("\n".join(lines[1:end]))
    data = _parse_map(yl, 0) if yl.peek() is not None else {}
    if yl.peek() is not None:
        raise ValueError("unparsed frontmatter content at %r"
                         % (yl.peek()[1],))
    if not isinstance(data, dict):
        raise ValueError("frontmatter must be a mapping")
    body = "\n".join(lines[end + 1:])
    return data, body


# ---------------------------------------------------------------------------
# Skill catalog
# ---------------------------------------------------------------------------

def is_valid_name(name):
    return isinstance(name, str) and len(name) <= 64 and NAME_RE.match(name)


class DigestCache:
    def __init__(self):
        self.cache = {}

    def digest(self, path):
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        hit = self.cache.get(key)
        if hit:
            return hit
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        value = "sha256:%s" % h.hexdigest()
        if len(self.cache) > 4096:
            self.cache.clear()
        self.cache[key] = value
        return value


class Skill:
    def __init__(self, root, skill_path, frontmatter, files):
        self.root = root                # absolute filesystem root
        self.skill_path = skill_path    # e.g. "acme/billing/refunds"
        self.frontmatter = frontmatter
        self.files = files              # [(relpath, abspath, size, digest)]

    @property
    def name(self):
        return self.frontmatter.get("name")


class Catalog:
    """A live, rescan-on-request view of the skills directory."""

    def __init__(self, root, scheme, digests):
        self.root = root
        self.scheme = scheme
        self.digests = digests
        self.skills = []          # valid skills, sorted by skill_path
        self.file_index = {}      # uri -> (abspath, size, digest)
        self.dir_index = set()    # directory URIs (skill dirs + subdirs)

    def uri_for(self, *parts):
        segments = []
        for part in parts:
            if part:
                segments.extend(urllib.parse.quote(seg, safe="")
                                for seg in part.split("/"))
        return "%s://%s" % (self.scheme, "/".join(segments))

    def scan(self):
        root_real = os.path.realpath(self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            if "SKILL.md" not in filenames:
                continue
            rel = os.path.relpath(dirpath, self.root)
            if rel == ".":
                warn("skipping SKILL.md at skills root: a skill must live "
                     "in a named subdirectory")
                continue
            skill_path = rel.replace(os.sep, "/")
            self._add_skill(root_real, dirpath, skill_path)
        self.skills.sort(key=lambda s: s.skill_path)

    def _add_skill(self, root_real, dirpath, skill_path):
        md_path = os.path.join(dirpath, "SKILL.md")
        try:
            with open(md_path, "r", encoding="utf-8") as fh:
                text = fh.read()
            frontmatter, _body = parse_frontmatter(text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            warn("skipping %s: %s" % (skill_path, exc))
            return
        name = frontmatter.get("name")
        final_segment = skill_path.split("/")[-1]
        if name != final_segment:
            warn("skipping %s: frontmatter name %r must equal the final "
                 "path segment (Agent Skills spec)" % (skill_path, name))
            return
        if not is_valid_name(name):
            warn("skipping %s: name %r violates Agent Skills naming rules "
                 "(1-64 chars, lowercase a-z/0-9/hyphens, no leading, "
                 "trailing or consecutive hyphens)" % (skill_path, name))
            return
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            warn("skipping %s: frontmatter description is required"
                 % skill_path)
            return
        files = []
        total = 0
        for base, dirnames, filenames in os.walk(dirpath):
            dirnames.sort()
            for fn in sorted(filenames):
                ap = os.path.join(base, fn)
                try:
                    real = os.path.realpath(ap)
                    st = os.stat(real)
                except OSError:
                    continue
                if not real.startswith(root_real + os.sep) and real != root_real:
                    warn("skipping file escaping the skills root: %s" % ap)
                    continue
                if not os.path.isfile(real):
                    continue
                # relpath from the unresolved walk path: on Windows,
                # realpath can resolve 8.3 short names in dirpath, and a
                # relpath across those forms wrongly starts with "..".
                relpath = os.path.relpath(ap, dirpath).replace(os.sep, "/")
                if relpath.startswith(".."):
                    continue
                size = st.st_size
                total += size
                files.append((relpath, real, size,
                              self.digests.digest(real)))
        if len(files) > MAX_RESOURCES_PER_SKILL:
            warn("skipping %s: %d files exceeds the %d-resource limit"
                 % (skill_path, len(files), MAX_RESOURCES_PER_SKILL))
            return
        if total > MAX_SKILL_TOTAL_BYTES:
            warn("skipping %s: %d bytes exceeds the 16 MiB per-skill limit"
                 % (skill_path, total))
            return
        skill = Skill(dirpath, skill_path, frontmatter, files)
        self.skills.append(skill)
        self.dir_index.add(self.uri_for(skill_path))
        for relpath, real, size, digest in files:
            uri = self.uri_for(skill_path, relpath)
            self.file_index[uri] = (real, size, digest)
            parts = relpath.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                self.dir_index.add(
                    self.uri_for(skill_path, "/".join(parts[:i])))

    def entry(self, skill):
        resources = [{"uri": self.uri_for(skill.skill_path, relpath),
                      "digest": digest, "size": size}
                     for relpath, _real, size, digest in skill.files]
        return {"uri": self.uri_for(skill.skill_path, "SKILL.md"),
                "frontmatter": skill.frontmatter,
                "resources": resources}

    def find_skill_by_md_uri(self, uri):
        for skill in self.skills:
            if self.uri_for(skill.skill_path, "SKILL.md") == uri:
                return skill
        return None


def guess_mime(path, is_text):
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    guessed, _enc = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "text/plain" if is_text else "application/octet-stream"


def _paged(items, cursor, page_size):
    start = 0
    if cursor is not None:
        try:
            start = int(cursor)
        except (TypeError, ValueError):
            raise ValueError("invalid cursor")
        if start < 0 or start > len(items):
            raise ValueError("invalid cursor")
    page = items[start:start + page_size]
    nxt = start + page_size
    return page, (str(nxt) if nxt < len(items) else None)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.digests = DigestCache()
        self.legacy_version = None

    # -- helpers -----------------------------------------------------------

    def _meta(self):
        return {"io.modelcontextprotocol/serverInfo": SERVER_INFO}

    def _ok(self, rid, result):
        result = dict(result)
        result["resultType"] = "complete"
        result["_meta"] = self._meta()
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(self, rid, code, message, data=None):
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    def _request_version(self, msg):
        meta = (msg.get("params") or {}).get("_meta") or {}
        return meta.get("io.modelcontextprotocol/protocolVersion")

    def _client_declares_skills(self, msg):
        meta = (msg.get("params") or {}).get("_meta") or {}
        caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        return SKILLS_EXT in (caps.get("extensions") or {})

    def _effective_version(self, msg):
        return self._request_version(msg) or self.legacy_version

    def _skills_enabled(self, msg):
        """The extension is defined only under 2026-07-28 and only for
        clients declaring it in per-request capabilities (SEP-2133)."""
        return (self._effective_version(msg) == PROTOCOL_CURRENT
                and self._client_declares_skills(msg))

    def _missing_cap_error(self, rid):
        return self._err(rid, E_MISSING_CLIENT_CAP,
                         "Missing required client capability",
                         {"requiredCapabilities":
                          {"extensions": {SKILLS_EXT: {}}}})

    def _check_version(self, msg):
        version = self._request_version(msg)
        if version is None:
            if self.legacy_version is not None:
                return None
            if msg.get("method") in ("server/discover", "initialize"):
                return None
            return self._err(msg.get("id"), E_INVALID_PARAMS,
                             "Missing io.modelcontextprotocol/"
                             "protocolVersion in request _meta")
        if version not in SUPPORTED_VERSIONS:
            return self._err(msg.get("id"), E_UNSUPPORTED_VERSION,
                             "Unsupported protocol version",
                             {"supported": SUPPORTED_VERSIONS,
                              "requested": version})
        return None

    def _catalog(self):
        catalog = Catalog(self.cfg.skills_dir, self.cfg.scheme, self.digests)
        catalog.scan()
        return catalog

    def _parse_uri(self, uri):
        """Return list of decoded path segments, or None if not ours."""
        if not isinstance(uri, str):
            return None
        prefix = "%s://" % self.cfg.scheme
        if not uri.startswith(prefix):
            return None
        rest = uri[len(prefix):]
        if not rest or "\\" in rest or "#" in rest or "?" in rest:
            return None
        try:
            segments = [urllib.parse.unquote(p) for p in rest.split("/")]
        except Exception:
            return None
        for seg in segments:
            if seg in ("", ".", "..") or "/" in seg or os.sep in seg:
                return None
            if os.altsep and os.altsep in seg:
                return None
        return segments

    # -- methods -----------------------------------------------------------

    def m_discover(self, rid, msg):
        return self._ok(rid, {
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": {
                "resources": {},
                "extensions": {SKILLS_EXT: {"directoryRead": True}},
            },
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Serves Agent Skills from a local directory. Call "
                "skills/list (declaring the io.modelcontextprotocol/"
                "skills extension) to enumerate the catalog with "
                "digests, skills/get for one entry by URI, "
                "resources/read for any skill:// file, and "
                "resources/directory/read to list a directory."
            ),
        })

    def m_initialize(self, rid, msg):
        """Legacy handshake for pre-2026-07-28 clients."""
        params = msg.get("params") or {}
        requested = params.get("protocolVersion", "2025-11-25")
        negotiated = (requested if requested in SUPPORTED_VERSIONS
                      else "2025-11-25")
        self.legacy_version = negotiated
        return self._ok(rid, {
            "protocolVersion": negotiated,
            "capabilities": {"resources": {}},
            "serverInfo": SERVER_INFO,
            "instructions": "Serves Agent Skills as MCP resources. "
                            "skills/list, skills/get and "
                            "resources/directory/read require protocol "
                            "version 2026-07-28 with the "
                            "io.modelcontextprotocol/skills extension.",
        })

    def m_skills_list(self, rid, msg):
        if not self._skills_enabled(msg):
            return self._missing_cap_error(rid)
        params = msg.get("params") or {}
        catalog = self._catalog()
        entries = [catalog.entry(skill) for skill in catalog.skills]
        try:
            page, next_cursor = _paged(entries, params.get("cursor"),
                                       self.cfg.page_size)
        except ValueError as exc:
            return self._err(rid, E_INVALID_PARAMS, str(exc))
        result = {"skills": page,
                  "ttlMs": self.cfg.ttl_ms,
                  "cacheScope": "public"}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return self._ok(rid, result)

    def m_skills_get(self, rid, msg):
        if not self._skills_enabled(msg):
            return self._missing_cap_error(rid)
        params = msg.get("params") or {}
        uri = params.get("uri")
        catalog = self._catalog()
        skill = (catalog.find_skill_by_md_uri(uri)
                 if isinstance(uri, str) else None)
        if skill is None:
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown skill URI: %r" % (uri,))
        return self._ok(rid, {"skill": catalog.entry(skill)})

    def _resource_object(self, catalog, uri, abspath, size):
        segments = self._parse_uri(uri)
        name = segments[-1] if segments else uri
        with open(abspath, "rb") as fh:
            head = fh.read(65536)
        is_text = True
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
        obj = {"uri": uri, "name": name,
               "mimeType": guess_mime(abspath, is_text), "size": size}
        if name == "SKILL.md":
            skill = catalog.find_skill_by_md_uri(uri)
            if skill is not None:
                obj["name"] = skill.frontmatter.get("name", name)
                obj["description"] = skill.frontmatter.get("description", "")
        return obj

    def m_resources_list(self, rid, msg):
        params = msg.get("params") or {}
        catalog = self._catalog()
        objects = [self._resource_object(catalog, uri, ap, size)
                   for uri, (ap, size, _d) in sorted(
                       catalog.file_index.items())]
        try:
            page, next_cursor = _paged(objects, params.get("cursor"),
                                       self.cfg.page_size)
        except ValueError as exc:
            return self._err(rid, E_INVALID_PARAMS, str(exc))
        result = {"resources": page,
                  "ttlMs": self.cfg.ttl_ms,
                  "cacheScope": "public"}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return self._ok(rid, result)

    def m_resources_read(self, rid, msg):
        params = msg.get("params") or {}
        uri = params.get("uri")
        catalog = self._catalog()
        hit = catalog.file_index.get(uri) if isinstance(uri, str) else None
        if hit is None:
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown resource: %r" % (uri,))
        abspath, size, _digest = hit
        try:
            with open(abspath, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._err(rid, E_INTERNAL, "Read failed: %s" % exc)
        item = {"uri": uri, "mimeType": guess_mime(abspath, True)}
        try:
            item["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            item["mimeType"] = guess_mime(abspath, False)
            item["blob"] = base64.b64encode(data).decode("ascii")
        return self._ok(rid, {"contents": [item]})

    def m_directory_read(self, rid, msg):
        if not self._skills_enabled(msg):
            return self._missing_cap_error(rid)
        params = msg.get("params") or {}
        uri = params.get("uri")
        catalog = self._catalog()
        if not isinstance(uri, str) or uri.endswith("/"):
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown directory resource: %r" % (uri,))
        segments = self._parse_uri(uri)
        if segments is None or uri not in catalog.dir_index:
            return self._err(rid, E_INVALID_PARAMS,
                             "Unknown directory resource: %r" % (uri,))
        prefix = uri + "/"
        children = []
        # direct child files
        for furi, (ap, size, _d) in sorted(catalog.file_index.items()):
            if furi.startswith(prefix):
                rest = furi[len(prefix):]
                if "/" not in rest:
                    children.append(
                        self._resource_object(catalog, furi, ap, size))
        # direct child directories
        for duri in sorted(catalog.dir_index):
            if duri.startswith(prefix):
                rest = duri[len(prefix):]
                if "/" not in rest:
                    children.append({"uri": duri,
                                     "name": duri.rsplit("/", 1)[-1],
                                     "mimeType": "inode/directory"})
        children.sort(key=lambda o: o["uri"])
        try:
            page, next_cursor = _paged(children, params.get("cursor"),
                                       self.cfg.page_size)
        except ValueError as exc:
            return self._err(rid, E_INVALID_PARAMS, str(exc))
        result = {"resources": page}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return self._ok(rid, result)

    # -- dispatch ----------------------------------------------------------

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._err(None, E_INVALID_REQUEST, "Invalid Request")
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:  # notification: never respond
            return None
        if not isinstance(method, str):
            return self._err(rid, E_INVALID_REQUEST, "Invalid Request")
        version_error = self._check_version(msg)
        if version_error is not None:
            return version_error
        handlers = {
            "server/discover": self.m_discover,
            "initialize": self.m_initialize,
            "ping": lambda r, m: self._ok(r, {}),
            "skills/list": self.m_skills_list,
            "skills/get": self.m_skills_get,
            "resources/list": self.m_resources_list,
            "resources/read": self.m_resources_read,
            "resources/directory/read": self.m_directory_read,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._err(rid, E_METHOD_NOT_FOUND,
                             "Method not found: %s" % method)
        try:
            return handler(rid, msg)
        except Exception as exc:  # last-resort protocol error
            return self._err(rid, E_INTERNAL, "Internal error: %s" % exc)

    def serve(self):
        out = sys.stdout
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = self._err(None, E_PARSE, "Parse error")
            else:
                response = self.handle(msg)
            if response is not None:
                out.write(json.dumps(response) + "\n")
                out.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="skills-dir-mcp",
        description="MCP server (stdio): serve a directory of Agent "
                    "Skills per the MCP Skills extension (SEP-2640).")
    parser.add_argument("--skills-dir", default="./skills",
                        help="Root directory containing skill folders "
                             "(default: ./skills).")
    parser.add_argument("--scheme", default="skill",
                        help="URI scheme for served resources "
                             "(default: skill).")
    parser.add_argument("--ttl-ms", type=int, default=30000,
                        help="Freshness hint (SEP-2549 ttlMs) attached to "
                             "list results, in ms (default: 30000).")
    parser.add_argument("--page-size", type=int, default=50,
                        help="Maximum entries per list page "
                             "(default: 50).")
    args = parser.parse_args(argv)
    skills_dir = os.path.abspath(args.skills_dir)
    if not os.path.isdir(skills_dir):
        warn("skills directory not found: %s (serving an empty catalog)"
             % skills_dir)
    args.skills_dir = skills_dir
    Server(args).serve()


if __name__ == "__main__":
    main()
