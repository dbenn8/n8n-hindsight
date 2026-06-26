#!/usr/bin/env python3
"""Sync the n8n-knowledge plugin's own source/docs into the Hindsight banks.

WHY THIS EXISTS
---------------
The plugin repo was retained once, ad-hoc, on 2026-05-31 while it was ~v0.3.0.
The n8n Pulse chatbot and the plugin's own recall read those snapshots, so by
v0.3.10 they were reporting a stale version, a stale install command, and stale
scale numbers ("315+ docs", "42,000+ data points"). This script makes that
refresh *repeatable*: re-retaining each file under its existing
``code-nk-<path>`` document_id REPLACES the stale memory in place (verified
behaviour: same document_id => replace, not duplicate). Run it after a release
(or on a cron) and the plugin's self-description can never drift again.

WHAT IT REFRESHES
-----------------
1. A curated set of the plugin's meaningful files (docs + architecture), wrapped
   in the exact format the original retain used so the document_ids line up.
2. One authoritative project-info memory (``project-n8n-knowledge-plugin``) with
   the current version / install reality / live scale, plus an overwrite of the
   orphan v0.3.0 project-info doc so no free-floating wrong fact survives.

It writes to BOTH banks: ``n8n`` (n8nhindsight instance — what Pulse reads) and
``portfolio`` (personal instance — Dan's dev portfolio). Keys are read from the
environment, falling back to ``portfolio/.env`` so secrets stay off the command
line.

SAFETY: defaults to --dry-run. Pass --apply to actually POST.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO = "dbenn8/n8n-knowledge"
GITHUB_BLOB = f"https://github.com/{REPO}/blob/master"
HTTP_TIMEOUT = 120
RETAIN_OK_STATUSES = (200, 201, 202)
BATCH_SIZE = 5

# Default plugin repo root: a sibling of n8n-hindsight, overridable via env/flag.
DEFAULT_REPO_ROOT = os.environ.get(
    "NK_REPO_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "n8n-knowledge")),
)

# Each target bank: (label, base_url, key_env, bank_id).
TARGETS = [
    ("n8n", "https://n8nhindsight.applikuapp.com", "N8N_HINDSIGHT_API_KEY", "n8n"),
    ("portfolio", "https://hindsight.applikuapp.com", "HINDSIGHT_API_KEY", "portfolio"),
]

# Curated files worth carrying in the knowledge base: the docs that describe the
# plugin (these are the ones that went stale) plus the core architecture files
# so "how does it work" answers stay rich. Keep this list deterministic and
# reviewable rather than walking the whole tree.
CURATED_FILES = [
    "README.md",
    ".claude-plugin/plugin.json",
    "skills/n8n-knowledge/SKILL.md",
    "PRIVACY.md",
    "hooks/auto-recall.sh",
    "hooks/lib/recall_common.sh",
    "hooks/lib/node_lookup.py",
]
# NOTE: CHANGELOG.md is deliberately NOT in CURATED_FILES. Retained as one doc it
# spans every version, and size-based chunking straddles version sections — so a
# chunk shows a "## 0.3.8" header followed by 0.3.9/0.3.10 content, and recall
# mis-attributes features to the wrong version (the dense 0.3.8 section also
# outranks the terse 0.3.10 one). Instead we split it per version below.

# Legacy free-floating project-info doc(s) to overwrite with the authoritative
# text so no stale "v0.3.0" fact survives (zero deletes — replace in place).
LEGACY_PROJECT_DOC_IDS = ["72e061f9-5bbf-4d7c-aac6-909e9c493eb1"]

# The old single-document CHANGELOG retain. Now superseded by per-version docs;
# delete it so its straddling chunks stop competing in recall.
OLD_CHANGELOG_DOC_ID = "code-nk-CHANGELOG-md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_env_file(path):
    """Populate os.environ from a .env file for any keys not already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def doc_id_for(path):
    """Match the original retain: code-nk-<path with '/' and '.' -> '-'>."""
    slug = path.replace("/", "-").replace(".", "-")
    return f"code-nk-{slug}"


def dir_tag_for(path):
    head = path.split("/")[0]
    return "root" if "/" not in path else head


def format_file_item(repo_root, path):
    full = os.path.join(repo_root, path)
    with open(full, encoding="utf-8") as f:
        content = f.read()
    url = f"{GITHUB_BLOB}/{path}"
    is_doc = path.endswith((".md", ".json"))
    tags = [
        "source:github-code",
        "project:n8n-knowledge-plugin",
        "pipeline:doc_id",
        "repo:n8n-knowledge",
        f"dir:{dir_tag_for(path)}",
        "type:code",
    ]
    if is_doc:
        tags.append("content_type:documentation")
    return {
        "document_id": doc_id_for(path),
        "content": f"n8n Knowledge Plugin source code ({REPO}): {path}\n\n```\n{content}\n```",
        "context": f"n8n-knowledge plugin codebase - {path} ({url})",
        "tags": tags,
        "metadata": {
            "url": url,
            "repo": REPO,
            "package": "n8n-knowledge",
            "filepath": path,
        },
    }


def parse_changelog(repo_root):
    """Split CHANGELOG.md into (version, header, body) per ``## `` section.

    Skips empty sections (e.g. a bare ``## Unreleased``). The version is the
    first ``X.Y.Z`` token in the header; sections without one keep the header
    slug (lower-cased) as their version key.
    """
    path = os.path.join(repo_root, "CHANGELOG.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    sections, header, body = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if header is not None:
                sections.append((header, "\n".join(body).strip()))
            header, body = line[3:].strip(), []
        elif header is not None:
            body.append(line)
    if header is not None:
        sections.append((header, "\n".join(body).strip()))

    out = []
    for hdr, txt in sections:
        if not txt:
            continue
        m = re.search(r"\d+\.\d+\.\d+", hdr)
        if not m:
            # Skip non-release sections (e.g. "## Unreleased") — they describe
            # unshipped changes and would mislead recall about the current build.
            continue
        out.append((m.group(0), hdr, txt))
    return out


def changelog_items(repo_root):
    """One retain item per CHANGELOG version, so version attribution is clean."""
    url = f"{GITHUB_BLOB}/CHANGELOG.md"
    items = []
    for version, header, body in parse_changelog(repo_root):
        vslug = version.replace(".", "-")
        items.append({
            "document_id": f"code-nk-CHANGELOG-{vslug}",
            "content": (
                f"n8n Knowledge Plugin ({REPO}) CHANGELOG — v{version} "
                f"(release section '{header}'). Every item below belongs to "
                f"v{version} specifically:\n\n{body}"
            ),
            "context": f"n8n-knowledge plugin CHANGELOG v{version} ({url})",
            "tags": [
                "source:github-code", "project:n8n-knowledge-plugin", "pipeline:doc_id",
                "repo:n8n-knowledge", "dir:root", "type:code",
                "content_type:changelog", f"version:{version}",
            ],
            "metadata": {
                "url": url, "repo": REPO, "package": "n8n-knowledge",
                "filepath": "CHANGELOG.md", "version": version,
            },
        })
    return items


def read_version(repo_root):
    with open(os.path.join(repo_root, ".claude-plugin", "plugin.json")) as f:
        return json.load(f)["version"]


def authoritative_text(version, stats):
    """One accurate, self-contained project-info memory."""
    mem = stats.get("total_nodes")
    links = stats.get("total_links")
    docs = stats.get("total_documents")
    scale = ""
    if mem:
        scale = (
            f" The knowledge base currently holds ~{mem:,} graph memories "
            f"({links:,} links across {docs:,} source documents) spanning n8n's "
            f"official docs, GitHub issues/PRs (with state), community-forum "
            f"solutions, release notes, and node specs."
        )
    return (
        f"The n8n Knowledge Plugin (github.com/{REPO}) is a free, open-source "
        f"Claude Code plugin by Dan Bennett, currently v{version}. It auto-recalls "
        f"curated n8n knowledge — official docs, GitHub issues with status, "
        f"community solutions, and per-operation node specs — directly in-editor "
        f"in ~1 second via bash hooks and curl, with no MCP server, daemon, or API "
        f"key required. Architecture: UserPromptSubmit/PostToolUse hooks detect "
        f"n8n context, query the shared Hindsight n8n bank over the keyless public "
        f"recall endpoint, and inject confidence-scored, source-linked results. "
        f"It also surfaces node-tagged 'gotcha' recall (known bugs for the exact "
        f"nodes you're using) and runs AI-generated workflows through a real n8n "
        f"validation engine (a self-hostable validator microservice, or a local "
        f"n8n-mcp install) before you import them.{scale} "
        f"INSTALL (current, public): in Claude Code run `/plugin marketplace add "
        f"https://github.com/{REPO}`, then `/plugin install n8n-knowledge@n8n-knowledge` "
        f"and `/reload-plugins` — no API keys, no config, no MCP server. The plugin has "
        f"been benchmarked head-to-head against the n8n-mcp MCP server on a "
        f"128-prompt eval suite scored by the real n8n validator: competitive "
        f"validity at materially lower cost and fewer tool turns. "
        f"When answering a question about this plugin (the n8n Knowledge Plugin / "
        f"n8n-knowledge), do NOT mention n8n's own internal Claude Code tooling in "
        f"the n8n-io/n8n repo — the `n8n:dev-flow` orchestrator, the `n8n:autodev-*` "
        f"agents (PR #31897), the `n8n-cli` skill, or the workflow-builder "
        f"SKILL.md. They are completely unrelated to this plugin and only confuse "
        f"a user who is trying to learn about it."
    )


def project_info_items(version, stats):
    text = authoritative_text(version, stats)
    base_meta = {"url": f"https://github.com/{REPO}", "repo": REPO, "package": "n8n-knowledge"}
    items = [{
        "document_id": "project-n8n-knowledge-plugin",
        "content": text,
        "context": f"n8n Knowledge Plugin — project overview ({REPO})",
        "tags": ["source:plugin", "type:project-info", "project:n8n-knowledge-plugin",
                 "content_type:documentation"],
        "metadata": base_meta,
    }]
    # Overwrite each legacy free-floating doc with the same authoritative text so
    # the stale "v0.3.0" fact is replaced, not left dangling (zero deletes).
    for did in LEGACY_PROJECT_DOC_IDS:
        items.append({
            "document_id": did,
            "content": text,
            "context": f"n8n Knowledge Plugin — project overview ({REPO})",
            "tags": ["type:project-info", "project:n8n-knowledge-plugin", "source:internal"],
            "metadata": base_meta,
        })
    return items


def fetch_live_stats():
    """Best-effort current scale from the public stats endpoint (n8n bank)."""
    try:
        url = "https://n8nhindsight.applikuapp.com/public/stats"
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        print(f"  (stats fetch failed, scale line omitted: {e})", file=sys.stderr)
        return {}


def retain_batch(items, base_url, key, bank_id):
    payload = json.dumps({"items": items, "async": True}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{base_url}/v1/default/banks/{bank_id}/memories",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status in RETAIN_OK_STATUSES
    except Exception as e:
        print(f"  RETAIN ERROR ({bank_id}): {e}", file=sys.stderr)
        return False


def delete_document(doc_id, base_url, key, bank_id):
    """DELETE one document by id. Returns True on success, True-ish on 404
    (already gone is fine), False on other errors."""
    req = urllib.request.Request(
        f"{base_url}/v1/default/banks/{bank_id}/documents/{urllib.parse.quote(doc_id, safe='')}",
        headers={"Authorization": f"Bearer {key}"}, method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status in (200, 202, 204)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  ({bank_id}) {doc_id} already absent (404)")
            return True
        print(f"  DELETE ERROR ({bank_id}/{doc_id}): {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  DELETE ERROR ({bank_id}/{doc_id}): {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=DEFAULT_REPO_ROOT,
                    help="Path to the n8n-knowledge repo (default: sibling dir or $NK_REPO_ROOT)")
    ap.add_argument("--env-file", default=os.path.join(
        os.path.dirname(__file__), "..", "..", "portfolio", ".env"),
        help="Fallback .env for HINDSIGHT keys (default: ../../portfolio/.env)")
    ap.add_argument("--banks", default="n8n,portfolio",
                    help="Comma-separated subset of target banks (default: both)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually POST (default is a dry run that only prints)")
    args = ap.parse_args()

    load_env_file(os.path.abspath(args.env_file))
    repo_root = os.path.abspath(args.repo_root)
    if not os.path.isdir(repo_root):
        sys.exit(f"repo root not found: {repo_root}")

    version = read_version(repo_root)
    stats = fetch_live_stats()
    print(f"Plugin version: {version} | live n8n-bank memories: {stats.get('total_nodes', '?')}")

    file_items = [format_file_item(repo_root, p) for p in CURATED_FILES]
    cl_items = changelog_items(repo_root)
    proj_items = project_info_items(version, stats)
    items = file_items + cl_items + proj_items
    print(f"Prepared {len(items)} items ({len(file_items)} files + "
          f"{len(cl_items)} per-version changelog + {len(proj_items)} project-info).")
    for it in items:
        first = it["content"].splitlines()[0]
        print(f"  - {it['document_id']:<45} {first[:70]}")
    print(f"Will DELETE superseded single-doc changelog: {OLD_CHANGELOG_DOC_ID}")

    selected = [b.strip() for b in args.banks.split(",") if b.strip()]
    targets = [t for t in TARGETS if t[0] in selected]

    if not args.apply:
        print("\nDRY RUN — no writes. Re-run with --apply to retain into:",
              ", ".join(t[0] for t in targets))
        return

    for label, base_url, key_env, bank_id in targets:
        key = os.environ.get(key_env, "")
        if not key:
            print(f"\n[{label}] SKIP — {key_env} not set", file=sys.stderr)
            continue
        print(f"\n[{label}] retaining {len(items)} items -> {base_url} bank={bank_id}")
        ok = fail = 0
        for i in range(0, len(items), BATCH_SIZE):
            batch = items[i:i + BATCH_SIZE]
            if retain_batch(batch, base_url, key, bank_id):
                ok += len(batch)
            else:
                fail += len(batch)
            print(f"  batch {i // BATCH_SIZE + 1}: ok={ok} fail={fail}")
        print(f"[{label}] done: {ok} ok, {fail} failed")
        # Remove the superseded single-doc changelog so its straddling chunks
        # stop competing with the per-version docs we just wrote.
        if delete_document(OLD_CHANGELOG_DOC_ID, base_url, key, bank_id):
            print(f"[{label}] deleted {OLD_CHANGELOG_DOC_ID}")
        else:
            print(f"[{label}] FAILED to delete {OLD_CHANGELOG_DOC_ID}", file=sys.stderr)


if __name__ == "__main__":
    main()
