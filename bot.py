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
    in_added_tr = False

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue

        if allowed_files and current_file not in allowed_files:
            continue
        if not allowed_files and not current_file.endswith(".md"):
            continue

        if not line.startswith("+") or line.startswith("+++"):
            in_added_tr = False
            current_tr = ""
            continue

        clean = line[1:].strip()

        # HTML table format (Simplify repos)
        if "<tr>" in clean:
            in_added_tr = True
            current_tr = ""
            continue
        if in_added_tr:
            current_tr += " " + clean
            if "</tr>" in clean:
                in_added_tr = False
                # Skip header rows, sub-rows, and inactive/closed entries
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
        if clean.startswith("|") and clean.count("|") >= 3:
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

    text = f"**{company_clean}**"
    if role_clean:
        text += f" — {role_clean}"
    if location_clean:
        text += f" ({location_clean})"
    if apply_link:
        text += f"\n🔗 **Apply:** {apply_link}"
    else:
        text += "\n🔒 Application closed"
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

    # Extract company names for the top line
    company_names = []
    for row in rows:
        if "<td>" in row:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row)
            if tds:
                company_names.append(extract_company_name(tds[0]))
        else:
            cells = [c.strip() for c in row.split("|")[1:-1]]
            if cells:
                company_names.append(extract_company_name(cells[0]))

    # Company names first, bolded, so they show in mobile notification preview
    names_str = ", ".join(dict.fromkeys(company_names))  # dedupe, preserve order
    header = f"**{names_str}**\n**New internship(s)!** [{label}]\n\n"

    # Group by source file
    sections = {}
    for row in rows:
        source = file_labels.get(row, "unknown")
        source_label = file_to_label(source)
        sections.setdefault(source_label, []).append(row)

    body = ""
    for section, section_rows in sections.items():
        if len(sections) > 1:
            body += f"__**{section}**__\n"
        for row in section_rows[:8]:
            body += format_row(row) + "\n\n"
        if len(section_rows) > 8:
            body += f"_...and {len(section_rows) - 8} more_\n\n"

    repo_url = f"https://github.com/{repo}"
    commit_url = f"{repo_url}/commit/{sha}"
    body += f"📂 [Repo]({repo_url}) | [View commit]({commit_url})\n{mention}"

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
