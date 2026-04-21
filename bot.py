import json
import os
import re
import time
from pathlib import Path

import requests

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.environ["DISCORD_USER_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # Optional, raises rate limit
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))  # seconds
REPO = "pittcsc/Summer2026-Internships"

SEEN_FILE = Path(__file__).parent / "seen_commits.json"


def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(list(seen)))


def github_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def get_recent_commits():
    """Fetch recent commits from the repo's default branch."""
    url = f"https://api.github.com/repos/{REPO}/commits"
    resp = requests.get(url, headers=github_headers(), params={"per_page": 10}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_commit_diff(sha):
    """Fetch the diff for a specific commit."""
    url = f"https://api.github.com/repos/{REPO}/commits/{sha}"
    headers = github_headers()
    headers["Accept"] = "application/vnd.github.v3.diff"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return ""
    return resp.text


def parse_added_rows(diff_text):
    """Extract added markdown table rows from a diff."""
    rows = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            clean = line[1:].strip()
            if clean.startswith("|") and clean.count("|") >= 3:
                if "---" in clean or "Company" in clean or "↳" in clean:
                    continue
                rows.append(clean)
    return rows


def format_row(row):
    """Parse a markdown table row into a readable string."""
    cells = [c.strip() for c in row.split("|")[1:-1]]
    if len(cells) < 2:
        return row

    company = cells[0]
    role = cells[1] if len(cells) > 1 else ""
    location = cells[2] if len(cells) > 2 else ""

    # Extract hyperlinks
    link_match = re.search(r"\[.*?\]\((.*?)\)", row)
    link = link_match.group(1) if link_match else ""

    # Clean markdown from company name
    company_clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", company).strip()

    text = f"**{company_clean}**"
    if role:
        role_clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", role).strip()
        text += f" — {role_clean}"
    if location:
        text += f" ({location})"
    if link:
        text += f"\n🔗 {link}"
    return text


def send_discord(content):
    """Send message to Discord via webhook."""
    # Discord has a 2000 char limit
    if len(content) > 1900:
        content = content[:1900] + "\n..."
    payload = {"content": content}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


def process_commit(sha, message):
    """Process a single commit: fetch diff, parse rows, notify."""
    diff = get_commit_diff(sha)
    rows = parse_added_rows(diff)

    if not rows:
        return

    mention = f"<@{DISCORD_USER_ID}>"
    header = f"{mention} **New internship(s) posted!**\n\n"

    body = ""
    for row in rows[:8]:
        body += format_row(row) + "\n\n"

    if len(rows) > 8:
        body += f"_...and {len(rows) - 8} more_\n"

    commit_url = f"https://github.com/{REPO}/commit/{sha}"
    body += f"\n[View commit]({commit_url})"

    send_discord(header + body)
    print(f"  → Notified: {len(rows)} new posting(s)")


def poll():
    """Main polling loop."""
    seen = load_seen()
    print(f"Internship bot started. Polling {REPO} every {POLL_INTERVAL}s...")
    print(f"Tracking {len(seen)} previously seen commits.")

    # On first run, mark all current commits as seen (don't spam on startup)
    if not seen:
        print("First run — marking existing commits as seen...")
        try:
            commits = get_recent_commits()
            seen = {c["sha"] for c in commits}
            save_seen(seen)
            print(f"Marked {len(seen)} commits as seen.")
        except Exception as e:
            print(f"Error on first run: {e}")

    while True:
        try:
            commits = get_recent_commits()
            new_commits = [c for c in commits if c["sha"] not in seen]

            if new_commits:
                print(f"Found {len(new_commits)} new commit(s)")
                # Process oldest first
                for commit in reversed(new_commits):
                    sha = commit["sha"]
                    message = commit["commit"]["message"]
                    print(f"  Processing: {message[:60]}")
                    process_commit(sha, message)
                    seen.add(sha)

                save_seen(seen)

                # Keep seen set from growing forever
                if len(seen) > 500:
                    current_shas = {c["sha"] for c in commits}
                    seen = current_shas
                    save_seen(seen)

        except requests.exceptions.RequestException as e:
            print(f"Request error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll()
