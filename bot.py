import json
import os
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import requests

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.environ["DISCORD_USER_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# Repos to watch
REPOS = [
    {
        "repo": "SimplifyJobs/Summer2026-Internships",
        "label": "Summer 2026 Internships",
        "files": ["README.md", "README-Off-Season.md"],
    },
    {
        "repo": "SimplifyJobs/New-Grad-Positions",
        "label": "New Grad (SWE/Quant/PM)",
        "files": ["README.md"],
    },
    {
        "repo": "alay02/quant-internships",
        "label": "Quant Internships",
        "files": None,  # Watch all .md files
    },
]

SEEN_FILE = Path(__file__).parent / "seen_commits.json"


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen))


def github_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def get_recent_commits(repo):
    url = f"https://api.github.com/repos/{repo}/commits"
    resp = requests.get(url, headers=github_headers(), params={"per_page": 10}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_commit_diff(repo, sha):
    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    headers = github_headers()
    headers["Accept"] = "application/vnd.github.v3.diff"
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        return ""
    return resp.text


def parse_added_rows(diff_text, allowed_files=None):
    """Extract added markdown table rows. If allowed_files is set, only those files."""
    rows = []
    current_file = ""
    file_labels = {}  # row -> which file it came from

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue

        # Filter by allowed files
        if allowed_files and current_file not in allowed_files:
            continue
        # For repos with no filter, only look at .md files
        if not allowed_files and not current_file.endswith(".md"):
            continue

        if line.startswith("+") and not line.startswith("+++"):
            clean = line[1:].strip()
            if clean.startswith("|") and clean.count("|") >= 3:
                if "---" in clean or "Company" in clean or "↳" in clean:
                    continue
                rows.append(clean)
                file_labels[clean] = current_file

    return rows, file_labels


def format_row(row):
    cells = [c.strip() for c in row.split("|")[1:-1]]
    if len(cells) < 2:
        return row

    company = cells[0]
    role = cells[1] if len(cells) > 1 else ""
    location = cells[2] if len(cells) > 2 else ""

    link_match = re.search(r"\[.*?\]\((.*?)\)", row)
    link = link_match.group(1) if link_match else ""

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


def file_to_label(filename):
    """Map filename to a human-readable label."""
    if filename == "README.md":
        return "Summer 2026"
    elif "Off-Season" in filename:
        return "Off-Season"
    return filename.replace(".md", "").replace("README-", "")


def send_discord(content):
    if len(content) > 1900:
        content = content[:1900] + "\n..."
    payload = {"content": content}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()


def process_commit(repo_config, sha):
    repo = repo_config["repo"]
    label = repo_config["label"]
    allowed_files = repo_config["files"]

    diff = get_commit_diff(repo, sha)
    rows, file_labels = parse_added_rows(diff, allowed_files)

    if not rows:
        return

    mention = f"<@{DISCORD_USER_ID}>"

    # Group by source file
    sections = {}
    for row in rows:
        source = file_labels.get(row, "unknown")
        source_label = file_to_label(source)
        sections.setdefault(source_label, []).append(row)

    header = f"{mention} **New internship(s) posted!** [{label}]\n\n"
    body = ""

    for section, section_rows in sections.items():
        if len(sections) > 1:
            body += f"__**{section}**__\n"
        for row in section_rows[:8]:
            body += format_row(row) + "\n\n"
        if len(section_rows) > 8:
            body += f"_...and {len(section_rows) - 8} more_\n\n"

    commit_url = f"https://github.com/{repo}/commit/{sha}"
    body += f"[View commit]({commit_url})"

    send_discord(header + body)
    print(f"  → Notified: {len(rows)} new posting(s) from {repo}")


def poll():
    seen = load_seen()
    print(f"Internship bot started. Polling {len(REPOS)} repos every {POLL_INTERVAL}s...")
    print(f"Repos: {', '.join(r['repo'] for r in REPOS)}")

    # First run: mark existing commits as seen
    for repo_config in REPOS:
        repo = repo_config["repo"]
        if repo not in seen:
            print(f"First run for {repo} — marking existing commits as seen...")
            try:
                commits = get_recent_commits(repo)
                seen[repo] = [c["sha"] for c in commits]
                print(f"  Marked {len(seen[repo])} commits as seen.")
            except Exception as e:
                print(f"  Error: {e}")
                seen[repo] = []
    save_seen(seen)

    while True:
        for repo_config in REPOS:
            repo = repo_config["repo"]
            try:
                commits = get_recent_commits(repo)
                seen_set = set(seen.get(repo, []))
                new_commits = [c for c in commits if c["sha"] not in seen_set]

                if new_commits:
                    print(f"[{time.strftime('%H:%M:%S')}] {repo}: {len(new_commits)} new commit(s)")
                    for commit in reversed(new_commits):
                        sha = commit["sha"]
                        message = commit["commit"]["message"]
                        print(f"  Processing: {message[:60]}")
                        process_commit(repo_config, sha)
                        seen_set.add(sha)

                    # Keep last 100 shas per repo
                    seen[repo] = list(seen_set)[-100:]
                    save_seen(seen)

            except requests.exceptions.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] {repo} request error: {e}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] {repo} error: {e}")

        print(f"[{time.strftime('%H:%M:%S')}] Poll complete. Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll()
