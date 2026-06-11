#!/usr/bin/env python3
"""
Incremental sync of n8n community forum content to Hindsight.
Fetches topics updated since last sync across all categories.

State tracked in SYNC_STATE_FILE (default: /data/sync-community-state.json).
"""
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://127.0.0.1:8889")
HINDSIGHT_KEY = os.environ.get("HINDSIGHT_API_TENANT_API_KEY", "")
BANK_ID = "n8n"
BASE_URL = "https://community.n8n.io"
STATE_FILE = os.environ.get("SYNC_COMMUNITY_STATE_FILE", "/data/sync-community-state.json")

CATEGORIES = [
    {"id": 12, "slug": "questions", "tag": "category:questions"},
    {"id": 5, "slug": "feature-requests", "tag": "category:feature-requests"},
    {"id": 36, "slug": "help-me-build-my-workflow", "tag": "category:help-me-build"},
    {"id": 15, "slug": "built-with-n8n", "tag": "category:built-with-n8n"},
    {"id": 28, "slug": "tutorials", "tag": "category:tutorials"},
    {"id": 17, "slug": "getting-started-with-n8n", "tag": "category:getting-started"},
    {"id": 11, "slug": "announcements", "tag": "category:announcements"},
]


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip = True
        elif tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def get_text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def strip_html(html):
    p = HTMLStripper()
    p.feed(html or "")
    return p.get_text()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def discourse_get(path):
    url = f"{BASE_URL}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def fetch_recent_topics(category, since_timestamp):
    """Fetch topics from a category that were bumped/updated after since_timestamp."""
    slug = category["slug"]
    cat_id = category["id"]
    topics = []
    page = 0

    while True:
        data = discourse_get(f"c/{slug}/{cat_id}.json?page={page}")
        if not data:
            break

        page_topics = data.get("topic_list", {}).get("topics", [])
        if not page_topics:
            break

        found_old = False
        for t in page_topics:
            if t.get("pinned"):
                continue
            bumped = t.get("bumped_at", t.get("last_posted_at", ""))
            if since_timestamp and bumped <= since_timestamp:
                found_old = True
                continue
            topics.append(t)

        if found_old or not data.get("topic_list", {}).get("more_topics_url"):
            break
        page += 1
        time.sleep(0.5)

    return topics


def fetch_topic_content(topic_id):
    data = discourse_get(f"t/{topic_id}.json")
    if not data:
        return None
    posts = data.get("post_stream", {}).get("posts", [])
    if not posts:
        return None
    first_post = posts[0]
    text = strip_html(first_post.get("cooked", ""))
    accepted = None
    for post in posts:
        if post.get("accepted_answer"):
            accepted = strip_html(post.get("cooked", ""))
            break
    return {"text": text, "accepted_answer": accepted, "username": first_post.get("username", "")}


def format_topic(t, content_data, category_tag):
    url = f"{BASE_URL}/t/{t.get('slug', '')}/{t['id']}"
    content = f"n8n Community: {t['title']}\n\n{content_data['text']}"
    if content_data.get("accepted_answer"):
        content += f"\n\nAccepted Answer:\n{content_data['accepted_answer']}"
    content = content[:5000]

    tags_list = ["type:community-post", "source:discourse", category_tag]
    if t.get("has_accepted_answer"):
        tags_list.append("outcome:solved")
    else:
        tags_list.append("outcome:unsolved")
    for tag in t.get("tags", []):
        tag_name = tag["name"] if isinstance(tag, dict) else tag
        tags_list.append(f"tag:{tag_name}")

    metadata = {
        "url": url,
        "topic_id": str(t["id"]),
        "views": str(t.get("views", 0)),
        "has_accepted_answer": str(t.get("has_accepted_answer", False)),
        "username": content_data.get("username", ""),
    }
    if t.get("vote_count"):
        metadata["vote_count"] = str(t["vote_count"])
    if t.get("like_count"):
        metadata["like_count"] = str(t["like_count"])

    return {
        "document_id": f"community-{t['id']}",
        "content": content,
        "context": f"community.n8n.io - {t['title']} ({url})",
        "tags": tags_list + ["pipeline:doc_id"],
        "metadata": metadata,
    }


def retain_batch(items):
    payload = json.dumps({"items": items, "async": True}).encode()
    headers = {"Authorization": f"Bearer {HINDSIGHT_KEY}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{HINDSIGHT_URL}/v1/default/banks/{BANK_ID}/memories",
        data=payload, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status in (200, 201, 202)
    except Exception as e:
        print(f"  RETAIN ERROR: {e}", file=sys.stderr, flush=True)
        return False


def main():
    if not HINDSIGHT_KEY:
        print("ERROR: HINDSIGHT_API_TENANT_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    full_run = "--full" in sys.argv
    since = None if full_run else state.get("last_sync")
    sync_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    total_retained = 0

    for cat in CATEGORIES:
        print(f"\n{cat['slug']}:", flush=True)
        topics = fetch_recent_topics(cat, since)
        print(f"  {len(topics)} new/updated topics", flush=True)

        batch = []
        for t in topics:
            content_data = fetch_topic_content(t["id"])
            if not content_data or not content_data["text"]:
                continue

            item = format_topic(t, content_data, cat["tag"])
            batch.append(item)

            if len(batch) >= 5:
                if retain_batch(batch):
                    total_retained += len(batch)
                batch = []
            time.sleep(0.3)

        if batch:
            if retain_batch(batch):
                total_retained += len(batch)

    state["last_sync"] = sync_start
    state["last_run"] = sync_start
    state["last_count"] = total_retained
    state["total_synced"] = state.get("total_synced", 0) + total_retained
    save_state(state)

    print(f"\nDone: {total_retained} community topics retained", flush=True)


if __name__ == "__main__":
    main()
