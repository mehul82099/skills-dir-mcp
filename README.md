# skills-dir-mcp

Serve a directory of [Agent Skills](https://agentskills.io/) over MCP.
A zero-dependency, single-file Python MCP server (stdio) implementing
the MCP Skills extension (`io.modelcontextprotocol/skills`,
[SEP-2640](https://modelcontextprotocol.io/seps/2640-skills-extension),
Final) on the stateless 2026-07-28 core protocol.

Agent Skills - folders with a `SKILL.md` of structured workflow
instructions - are how agents learn repeatable tasks. Today they live
as local files, so sharing a team's skill catalog means copying files
into every checkout and every host. SEP-2640 standardizes serving them
over MCP instead: each file becomes a `skill://` resource, one
`skills/list` call hands the host the full catalog with SHA-256
digests and sizes, and the host verifies every byte it reads against
the entry it approved.

skills-dir-mcp turns any local skills folder into that server. Point
it at a directory and any MCP host that speaks the extension can
browse, verify and load your skills - one file, no dependencies, no
install step, nothing to audit but this repo.

## What it serves

```
skills/
  git-workflow/
    SKILL.md                  # name: git-workflow  (must match the folder)
    references/BRANCHING.md
  pdf-processing/
    SKILL.md
    scripts/extract.py
    templates/invoice.md
  acme/billing/refunds/       # nested paths become organizational prefixes
    SKILL.md                  # name: refunds -> skill://acme/billing/refunds
```

- `skills/list` - the catalog: each skill's `SKILL.md` URI, its
  frontmatter verbatim as JSON, and a complete manifest of its files
  with `sha256:` digests and byte sizes. Carries the SEP-2549 caching
  hints (`ttlMs`, `cacheScope`). Cursor pagination.
- `skills/get` - one skill's entry by its `SKILL.md` URI, including
  skills a partial listing might omit; `-32602` for anything else.
- `resources/read` - any skill file as a standard MCP resource; text
  as `text`, binaries as base64 `blob`.
- `resources/list` - every served file with `mimeType`, `size`, and
  frontmatter-derived `name`/`description` on `SKILL.md` resources.
- `resources/directory/read` - direct children of any directory in
  the skill namespace (`inode/directory` entries for subdirectories);
  advertised via the extension's `directoryRead` setting.

## Run it

```sh
python3 skills_dir_mcp.py --skills-dir /path/to/skills
```

Client configuration (stdio):

```json
{
  "mcpServers": {
    "team-skills": {
      "command": "python3",
      "args": ["/path/to/skills_dir_mcp.py", "--skills-dir", "/path/to/skills"]
    }
  }
}
```

Flags: `--skills-dir` (default `./skills`), `--scheme` (default
`skill`), `--ttl-ms` (default 30000), `--page-size` (default 50).

The catalog is rescanned on every request, so edits on disk are live
immediately; file digests are cached by (path, size, mtime).

## Protocol notes and honest limits

- Implements `skills/list`, `skills/get`, `resources/list`,
  `resources/read` and `resources/directory/read` per SEP-2640
  (Final), over the stateless 2026-07-28 core (SEP-2575) with
  `server/discover`; a legacy `initialize` handshake covers
  pre-2026-07-28 clients, which can read `skill://` resources but get
  `-32021` for extension methods (the extension is not defined under
  older versions).
- Skills that violate the contract are skipped with a stderr warning,
  never half-served: frontmatter `name` must equal the folder's final
  path segment and pass Agent Skills naming rules, `description` is
  required, and skills over the SEP's limits (512 files or 16 MiB
  total) are excluded.
- Frontmatter is rendered verbatim as JSON using a built-in YAML
  subset parser (flat and nested mappings, plain/quoted scalars,
  block and inline lists, `|`/`>` block scalars). Skills using YAML
  outside that subset are skipped with a warning rather than
  mis-served.
- Skill names are validated against the ASCII subset of the Agent
  Skills naming rules (lowercase `a-z`, `0-9`, single hyphens).
- Files are served with percent-encoded URIs; symlinks that resolve
  outside the skills root are skipped.
- The catalog is identical for every caller, so list results are
  marked `cacheScope: "public"`.

## Security

Skill content is instructional text aimed at a model, which makes it a
prompt-injection surface; SEP-2640's security section puts the
approval, origin-tagging and verification duties on the host. This
server's side of the bargain: per-file SHA-256 digests and sizes on
every entry so hosts can verify content and bind approvals to an exact
manifest, a strict path jail (no traversal, no symlink escapes),
read-only access to the skills directory, and no network listener -
stdio serves a single client process. Only serve skills you trust;
the digests prove consistency between listing and content, not that
the content is trustworthy.

## Landscape

SEP-2640 is recent (Final, authored by the Skills Over MCP Working
Group) and implementations are still thin on the ground: the official
Python SDK merged reference support in October 2026
([python-sdk#3485](https://github.com/modelcontextprotocol/python-sdk/pull/3485)),
the working group ships a TypeScript demo
([olaservo/skills-over-mcp-demo](https://github.com/olaservo/skills-over-mcp-demo)),
GitHub's MCP server has a demo PR open, and FastMCP's older
`SkillsProvider` predates the standard and diverges from its URI
structure (noted in the SEP's own compatibility section).
skills-dir-mcp is the same protocol with no SDK and no dependencies -
a single readable file you can drop anywhere Python 3.9+ runs.

## Tests

35 stdlib `unittest` tests drive the real server over stdio as an MCP
client: catalog listing, verbatim frontmatter, digest/size fidelity,
nested skills, directory reads, extension gating (`-32021`), version
negotiation (`-32022`), pagination, binary blobs, percent-encoded
URIs, traversal rejection, and every skip rule. CI runs them on
Ubuntu and Windows, Python 3.9 and 3.13.

```sh
python3 -m unittest -v
```

## Sources

- [SEP-2640: Skills Extension](https://modelcontextprotocol.io/seps/2640-skills-extension) (Final)
- [Agent Skills specification](https://agentskills.io/specification)
- [SEP-2133: Extensions](https://modelcontextprotocol.io/seps/2133-extensions), [SEP-2549: TTL for list results](https://modelcontextprotocol.io/seps/2549-TTL-for-list-results), [SEP-2575: Stateless MCP](https://modelcontextprotocol.io/seps/2575-stateless-mcp)
- [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [python-sdk#3485](https://github.com/modelcontextprotocol/python-sdk/pull/3485) (official SDK support, merged 2026-10-11)

Spec texts fetched 2026-11-03. Tested against the SEP's own message
examples; no third-party host was interop-tested.

## License

MIT
