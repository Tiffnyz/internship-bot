import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

import requests

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.environ["DISCORD_USER_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
# UTC offset for recap times (e.g., -4 for EDT, -5 for EST)
TIMEZONE_OFFSET = int(os.environ.get("TIMEZONE_OFFSET", "-5"))

# Priority companies (case-insensitive matching)
PRIORITY_COMPANIES = {
    # SWE
    "meta", "apple", "amazon", "netflix", "google", "microsoft", "nvidia", "tesla",
    "linkedin", "bytedance", "tiktok", "stripe", "databricks", "snowflake", "airbnb",
    "uber", "doordash", "palantir", "anduril", "spacex", "coinbase", "robinhood",
    "block", "shopify", "pinterest", "snap", "spotify", "reddit", "roblox", "discord",
    "salesforce", "oracle", "figma", "notion", "canva", "plaid", "ramp", "rippling",
    # ML
    "openai", "anthropic", "google deepmind", "deepmind", "meta ai", "xai", "mistral",
    "mistral ai", "cohere", "nvidia research", "microsoft ai", "apple aiml",
    "amazon agi", "tesla ai", "hugging face", "huggingface", "perplexity", "scale ai",
    "scale", "runway", "midjourney", "stability ai", "luma", "pika", "character.ai",
    "character ai", "adept", "waymo", "zoox", "aurora", "wayve", "figure", "1x",
    "skild", "physical intelligence",
    # Quant
    "jane street", "citadel securities", "citadel", "hudson river trading", "hrt",
    "jump trading", "two sigma securities", "two sigma", "tower research",
    "tower research capital", "optiver", "imc trading", "imc", "susquehanna", "sig",
    "drw", "xtx markets", "xtx", "virtu financial", "virtu", "flow traders",
    "five rings", "headlands technologies", "headlands", "akuna capital", "akuna",
    "old mission capital", "old mission", "belvedere", "wolverine",
    "chicago trading company", "ctc", "d. e. shaw", "de shaw", "d.e. shaw",
    "renaissance technologies", "rentec", "pdt partners", "millennium",
    "point72", "cubist", "balyasny", "aqr capital", "aqr", "bridgewater",
    "g-research", "qube research", "squarepoint",
}


def is_priority(company_name):
    """Check if a company is in the priority list."""
    name = company_name.lower().strip()
    # Direct match
    if name in PRIORITY_COMPANIES:
        return True
    # Partial match (e.g., "Meta Platforms" matches "meta")
    for p in PRIORITY_COMPANIES:
        if p in name or name in p:
            return True
    return False


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
        "repo": "speedyapply/2026-SWE-College-Jobs",
        "label": "SWE College Jobs",
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
    """Extract new internship entries from diff. Handles both markdown and HTML tables."""
    rows = []
    current_file = ""
    file_labels = {}
    # Buffer for multi-line HTML table rows (<tr>...</tr>)
    current_tr = ""
    in_tr = False
    has_added_content = False

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith("--- "):
            continue

        if allowed_files and current_file not in allowed_files:
            continue
        if not allowed_files and not current_file.endswith(".md"):
            continue

        # For HTML rows: <tr> can be a context line (space prefix) or added line (+)
        # We need to track the full row and check if it has any added content
        is_added = line.startswith("+")
        is_context = line.startswith(" ")
        is_removed = line.startswith("-")

        # Skip removed lines
        if is_removed:
            continue

        # Strip the prefix to get content
        if is_added:
            clean = line[1:].strip()
        elif is_context:
            clean = line[1:].strip()
        else:
            # Hunk headers etc — reset state
            in_tr = False
            current_tr = ""
            has_added_content = False
            continue

        # HTML table format (Simplify repos)
        if "<tr>" in clean:
            in_tr = True
            current_tr = ""
            has_added_content = False
            if is_added:
                has_added_content = True
            continue

        if in_tr:
            current_tr += " " + clean
            if is_added:
                has_added_content = True
            if "</tr>" in clean:
                in_tr = False
                if not has_added_content:
                    current_tr = ""
                    continue
                # Skip sub-rows and header rows
                if "↳" in current_tr or "Company" in current_tr:
                    current_tr = ""
                    continue
                # Must have at least a company and role
                tds = re.findall(r'<td[^>]*>(.*?)</td>', current_tr)
                if len(tds) >= 2 and not should_skip_row(current_tr):
                    rows.append(current_tr)
                    file_labels[current_tr] = current_file
                current_tr = ""
            continue

        # Markdown pipe table format (speedyapply)
        if is_added and clean.startswith("|") and clean.count("|") >= 3:
            if "---" in clean or "Company" in clean or "↳" in clean:
                continue
            if should_skip_row(clean):
                continue
            rows.append(clean)
            file_labels[clean] = current_file

    return rows, file_labels


def should_skip_row(row):
    """Return True if this row should be filtered out (PhD, grad-level, or old)."""
    row_lower = row.lower()

    # Skip PhD and graduate-level positions
    phd_keywords = ["phd", "ph.d", "doctoral", "postdoc", "post-doc",
                    "graduate research", "grad research", "masters", "master's"]
    for kw in phd_keywords:
        if kw in row_lower:
            return True

    # Skip international (non-US) locations
    intl_indicators = ["canada", ", ca$", ", uk", ", uk,", "united kingdom",
                       "london, uk", "cambridge, uk", "ireland", "germany",
                       "india", "singapore", "japan", "australia", "france",
                       "netherlands", "switzerland", "brazil", "mexico",
                       "israel", "china", "korea", "taiwan", "hong kong",
                       ", on,", ", on ", ", bc,", ", bc ", ", ab,", ", qc,",
                       "toronto", "montreal", "vancouver", "waterloo",
                       "ontario", "quebec", "british columbia", "alberta"]
    # Check location cell specifically
    loc_text = ""
    if "<td>" in row:
        tds = re.findall(r'<td[^>]*>(.*?)</td>', row)
        if len(tds) >= 3:
            loc_text = re.sub(r'<[^>]+>', '', tds[2]).lower().strip()
    else:
        cells = [c.strip() for c in row.split("|")[1:-1]]
        if len(cells) >= 3:
            loc_text = cells[2].lower().strip()

    if loc_text:
        for indicator in intl_indicators:
            if indicator in loc_text:
                return True
        # If location doesn't mention a US state/city, check for ", XX" country codes
        # that aren't US states
        non_us = re.search(r', ([A-Z]{2})(?:\s|$|,|\|)', row)
        if non_us:
            code = non_us.group(1)
            us_states = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
                        "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
                        "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
                        "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
                        "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
                        "DC"}
            if code not in us_states and code != "US":
                return True

    # Check age — skip anything older than 7 days
    # Simplify format: <td>1mo</td>, <td>25d</td>, <td>0d</td>
    age_match = re.search(r'(\d+)(mo|d|wk)', row_lower)
    if age_match:
        num = int(age_match.group(1))
        unit = age_match.group(2)
        if unit == "mo":
            return True  # Months old — skip
        if unit == "wk" and num >= 1:
            return True  # 1+ weeks old — skip
        if unit == "d" and num > 7:
            return True  # More than 7 days — skip

    # speedyapply format: | 40d | or | 4d |
    pipe_age = re.findall(r'\|\s*(\d+)(d|mo|wk)\s*\|', row_lower)
    for num_str, unit in pipe_age:
        num = int(num_str)
        if unit == "mo":
            return True
        if unit == "wk" and num >= 1:
            return True
        if unit == "d" and num > 7:
            return True

    return False


def extract_apply_link(text):
    """Extract the actual job application URL from a row."""
    # Look for <a href="..."><img ... alt="Apply"> (Simplify and speedyapply format)
    apply_match = re.search(r'<a href="([^"]+)"[^>]*>\s*<img[^>]*alt="Apply"', text)
    if apply_match:
        return apply_match.group(1)
    # Fallback: markdown link format [Apply](url)
    md_match = re.search(r'\[Apply\]\((.*?)\)', text)
    if md_match:
        return md_match.group(1)
    return ""


def extract_company_name(text):
    """Extract company name from HTML or markdown."""
    # HTML: <strong><a href="...">Company</a></strong> or <a href="..."><strong>Company</strong></a>
    html_match = re.search(r'<(?:strong|a)[^>]*>(?:<(?:strong|a)[^>]*>)?([^<]+)', text)
    if html_match:
        return html_match.group(1).strip()
    # Markdown: [Company](url)
    md_match = re.search(r'\[([^\]]+)\]', text)
    if md_match:
        return md_match.group(1).strip()
    # Strip all HTML tags
    return re.sub(r'<[^>]+>', '', text).strip()


def format_row(row):
    # Handle both pipe-delimited markdown and HTML <td> rows
    if "<td>" in row:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row)
    else:
        cells = [c.strip() for c in row.split("|")[1:-1]]

    if len(cells) < 2:
        return row

    company = cells[0]
    role = cells[1] if len(cells) > 1 else ""
    location = cells[2] if len(cells) > 2 else ""

    apply_link = extract_apply_link(row)
    company_clean = extract_company_name(company)

    # Clean role of HTML
    role_clean = re.sub(r'<[^>]+>', '', role).strip()
    # Clean location of HTML
    location_clean = re.sub(r'<[^>]+>', '', location).strip().replace("<br>", ", ")

    priority = is_priority(company_clean)

    if priority:
        text = f"⭐ **{company_clean}** — {role_clean}"
        if location_clean:
            text += f" ({location_clean})"
        if apply_link:
            text += f"\n🔗 Apply: <{apply_link}>"
        else:
            text += "\n🔒 Application closed"
    else:
        text = f"{company_clean} — {role_clean}"
        if location_clean:
            text += f" ({location_clean})"
        if apply_link:
            text += f" — <{apply_link}>"
        else:
            text += " — 🔒 closed"

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
    payload = {"content": content, "flags": 4}
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

    # Extract company names and sort priority first
    def get_company(row):
        if "<td>" in row:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row)
            return extract_company_name(tds[0]) if tds else ""
        else:
            cells = [c.strip() for c in row.split("|")[1:-1]]
            return extract_company_name(cells[0]) if cells else ""

    company_names = [get_company(r) for r in rows]
    priority_names = [n for n in company_names if is_priority(n)]
    other_names = [n for n in company_names if not is_priority(n)]

    # Show priority names first in header
    all_names = list(dict.fromkeys(priority_names + other_names))
    names_str = ", ".join(all_names)
    header = f"**{names_str}**\n**New internship(s)!** [{label}]\n\n"

    # Sort rows: priority first, then others
    priority_rows = [(r, fl) for r, fl in zip(rows, [file_labels.get(r, "unknown") for r in rows]) if is_priority(get_company(r))]
    other_rows = [(r, fl) for r, fl in zip(rows, [file_labels.get(r, "unknown") for r in rows]) if not is_priority(get_company(r))]
    sorted_entries = priority_rows + other_rows

    body = ""
    for row, source in sorted_entries[:12]:
        body += format_row(row) + "\n"
        # Add extra spacing for priority
        if is_priority(get_company(row)):
            body += "\n"

    if len(sorted_entries) > 12:
        body += f"_...and {len(sorted_entries) - 12} more_\n"

    repo_url = f"https://github.com/{repo}"
    commit_url = f"{repo_url}/commit/{sha}"
    body += f"📂 [Repo]({repo_url}) | [View commit]({commit_url})\n{mention}"

    send_discord(header + body)
    print(f"  → Notified: {len(rows)} new posting(s) from {repo}")


def fetch_current_listings(repo_config):
    """Fetch the current README and extract all 0d entries."""
    repo = repo_config["repo"]
    files = repo_config["files"] or ["README.md"]
    entries = []

    for filename in files:
        url = f"https://raw.githubusercontent.com/{repo}/main/{filename}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 404:
                # Try dev branch (Simplify uses dev)
                url = f"https://raw.githubusercontent.com/{repo}/dev/{filename}"
                resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                continue

            content = resp.text
            # Parse HTML table rows
            in_tr = False
            current_tr = ""
            for line in content.splitlines():
                line = line.strip()
                if "<tr>" in line:
                    in_tr = True
                    current_tr = ""
                    continue
                if in_tr:
                    current_tr += " " + line
                    if "</tr>" in line:
                        in_tr = False
                        # Only include 0d entries (posted today)
                        if "<td>0d</td>" in current_tr:
                            if "↳" not in current_tr and "Company" not in current_tr:
                                if not should_skip_row(current_tr):
                                    entries.append((current_tr, repo_config["label"]))
                        current_tr = ""

            # Also handle markdown pipe tables
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("|") and line.count("|") >= 3:
                    if "---" in line or "Company" in line or "↳" in line:
                        continue
                    # Check for 0d or 1d age
                    if "| 0d |" in line or "| 1d |" in line:
                        if not should_skip_row(line):
                            entries.append((line, repo_config["label"]))

        except requests.RequestException as e:
            print(f"  Recap fetch error for {repo}/{filename}: {e}")

    return entries


def send_recap():
    """Send a 12-hour recap of all recent internships."""
    print(f"[{time.strftime('%H:%M:%S')}] Generating recap...")
    all_entries = []
    for repo_config in REPOS:
        entries = fetch_current_listings(repo_config)
        all_entries.extend(entries)

    mention = f"<@{DISCORD_USER_ID}>"
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    now = datetime.now(tz)
    time_str = now.strftime("%I:%M %p %Z")

    if not all_entries:
        content = f"**12-Hour Recap** ({time_str})\nNo new internships posted recently.\n{mention}"
        send_discord(content)
        print("  Recap sent (no new entries)")
        return

    # Group by source label
    by_source = {}
    for row, label in all_entries:
        by_source.setdefault(label, []).append(row)

    header = f"**12-Hour Recap** ({time_str}) — {len(all_entries)} posting(s)\n\n"
    body = ""

    for label, rows_list in by_source.items():
        if len(by_source) > 1:
            body += f"__**{label}**__\n"
        for row in rows_list[:15]:
            body += format_row(row) + "\n\n"
        if len(rows_list) > 15:
            body += f"_...and {len(rows_list) - 15} more_\n\n"

    body += mention

    # Discord has 2000 char limit — split into multiple messages if needed
    full = header + body
    if len(full) <= 1900:
        send_discord(full)
    else:
        # Send header + as many rows as fit
        send_discord(header + body[:1850] + f"\n...\n{mention}")

    print(f"  Recap sent: {len(all_entries)} entries")


def poll():
    seen = load_seen()
    print(f"Internship bot started. Polling {len(REPOS)} repos every {POLL_INTERVAL}s...")
    print(f"Repos: {', '.join(r['repo'] for r in REPOS)}")

    # On startup, always mark all current commits as seen to avoid re-notifying
    # (Railway's filesystem is ephemeral — seen_commits.json is lost on redeploy)
    for repo_config in REPOS:
        repo = repo_config["repo"]
        print(f"Syncing {repo} — marking current commits as seen...")
        try:
            commits = get_recent_commits(repo)
            existing = set(seen.get(repo, []))
            existing.update(c["sha"] for c in commits)
            seen[repo] = list(existing)[-100:]
            print(f"  Tracking {len(seen[repo])} commits.")
        except Exception as e:
            print(f"  Error: {e}")
            if repo not in seen:
                seen[repo] = []
    save_seen(seen)

    last_recap_hour = None

    while True:
        # Check if it's recap time (10am or 10pm local time)
        tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
        now = datetime.now(tz)
        current_hour = now.hour
        if current_hour in (10, 22) and last_recap_hour != current_hour:
            last_recap_hour = current_hour
            try:
                send_recap()
            except Exception as e:
                print(f"Recap error: {e}")
        elif current_hour not in (10, 22):
            last_recap_hour = None  # Reset so next 10/22 triggers

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
                        # Mark as seen BEFORE processing so we never retry on failure
                        seen_set.add(sha)
                        try:
                            process_commit(repo_config, sha)
                        except Exception as e:
                            print(f"  Error processing {sha[:8]}: {e}")

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
