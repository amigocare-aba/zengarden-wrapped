"""
Zen Garden Weekly Points — Automated Extractor
================================================
Reads weekly_config.json, pulls Slack data, calculates points, and outputs:
  - zen_garden_weekly.json  (cumulative, powers the webpage)
  - weekly_reports/weekN.csv (permanent record)
  - weekly_summary.txt      (Slack-ready text)
  - run_status.json         (audit artifact for admin page)

Points rules:
  1 pt  — Post a top-level message (not a group activity)
  2 pts — Comment on someone's post (once per thread, first reply only)
          Cannot earn comment points on group activity threads.
  5 pts — Group activity photo: post has image + @mentions.
          Poster AND each tagged person receive 5 pts.

Usage:
  python zen_garden_weekly_auto.py              # auto-detect current week
  python zen_garden_weekly_auto.py --week 2     # process specific week
  python zen_garden_weekly_auto.py --dry-run    # test without writing files
  python zen_garden_weekly_auto.py --week 2 --dry-run
"""

import os
import re
import sys
import json
import csv
import hashlib
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── PATHS ───────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "weekly_config.json")
JSON_FILE = os.path.join(SCRIPT_DIR, "zen_garden_weekly.json")
STATUS_FILE = os.path.join(SCRIPT_DIR, "run_status.json")
SUMMARY_FILE = os.path.join(SCRIPT_DIR, "weekly_summary.txt")
REPORTS_DIR = os.path.join(SCRIPT_DIR, "weekly_reports")

# Google Drive CSV path (only works locally, not in GitHub Actions)
GDRIVE_CSV_DIR = os.path.join(
    os.path.expanduser("~"),
    "Library/CloudStorage",
    "GoogleDrive-alex.arevalo@amigocareaba.com",
    "Shared drives/10 OBM/x. Zen Garden/Data - Weekly CSV"
)

# ── SECRETS ─────────────────────────────────────────────────────────
# Tokens must be set as environment variables (or GitHub Actions secrets).
# For local use: export SLACK_BOT_TOKEN=xoxb-... && export SLACK_USER_TOKEN=xoxp-...
# Or create a .env file and source it: source .env
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")

if not BOT_TOKEN or not USER_TOKEN:
    # Try loading from .env file if it exists
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()
        BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
        USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")

    if not BOT_TOKEN or not USER_TOKEN:
        print("❌ Missing Slack tokens. Set SLACK_BOT_TOKEN and SLACK_USER_TOKEN as environment variables.")
        print("   Or create a .env file with:")
        print("     SLACK_BOT_TOKEN=xoxb-...")
        print("     SLACK_USER_TOKEN=xoxp-...")
        sys.exit(1)


# ── CONFIG VALIDATION ───────────────────────────────────────────────

def load_and_validate_config():
    """Load weekly_config.json and validate all fields."""
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file not found: {CONFIG_FILE}")
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON in config: {e}")
            sys.exit(1)

    errors = []

    # Required top-level fields
    for field in ["month", "year", "weeks", "roles"]:
        if field not in config:
            errors.append(f"Missing required field: '{field}'")

    # Validate weeks array
    weeks = config.get("weeks", [])
    if not isinstance(weeks, list) or len(weeks) == 0:
        errors.append("'weeks' must be a non-empty array")
    else:
        seen_numbers = set()
        for i, week in enumerate(weeks):
            prefix = f"weeks[{i}]"

            for field in ["week_number", "start", "end", "skip"]:
                if field not in week:
                    errors.append(f"{prefix}: missing '{field}'")

            wn = week.get("week_number")
            if not isinstance(wn, int) or wn < 1:
                errors.append(f"{prefix}: 'week_number' must be a positive integer")
            elif wn in seen_numbers:
                errors.append(f"{prefix}: duplicate week_number {wn}")
            else:
                seen_numbers.add(wn)

            try:
                start = datetime.strptime(week.get("start", ""), "%Y-%m-%d")
                end = datetime.strptime(week.get("end", ""), "%Y-%m-%d")
                if start >= end:
                    errors.append(f"{prefix}: 'start' must be before 'end'")
            except ValueError:
                errors.append(f"{prefix}: dates must be YYYY-MM-DD format")

            if not isinstance(week.get("skip"), bool):
                errors.append(f"{prefix}: 'skip' must be true or false")

    # Validate roles
    roles = config.get("roles", {})
    if not isinstance(roles, dict) or len(roles) == 0:
        errors.append("'roles' must be a non-empty object")

    if errors:
        print("❌ Config validation failed:")
        for e in errors:
            print(f"   • {e}")
        sys.exit(1)

    return config


def config_hash(config):
    """Generate a short hash of the config for audit purposes."""
    raw = json.dumps(config, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ── SLACK API HELPERS ──────────────────────────────────────────────

def make_clients():
    return WebClient(token=BOT_TOKEN), WebClient(token=USER_TOKEN)


def get_channel_id(bot_client, name):
    cursor = None
    while True:
        result = bot_client.conversations_list(
            types="public_channel", limit=200, cursor=cursor
        )
        for ch in result["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise Exception(f"Channel '#{name}' not found.")


def get_users(user_client):
    result = user_client.users_list()
    users = {}
    for u in result["members"]:
        if not u["is_bot"] and not u["deleted"] and u["id"] != "USLACKBOT":
            users[u["id"]] = u.get("real_name") or u.get("name")
    return users


def get_all_messages(bot_client, channel_id, oldest_ts, latest_ts):
    """Fetch ALL messages where start <= ts < end."""
    messages = []
    cursor = None
    while True:
        result = bot_client.conversations_history(
            channel=channel_id,
            oldest=str(oldest_ts),
            latest=str(latest_ts),
            limit=200,
            cursor=cursor
        )
        messages.extend(result["messages"])
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def get_replies(bot_client, channel_id, thread_ts, oldest_ts, latest_ts):
    """Fetch all replies in a thread within the date window."""
    try:
        result = bot_client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            oldest=str(oldest_ts),
            latest=str(latest_ts),
            limit=200
        )
        return result["messages"][1:]  # skip parent
    except SlackApiError:
        return []


# ── HELPERS ────────────────────────────────────────────────────────

def extract_mentions(text):
    """Extract user IDs from Slack @mentions like <@U12345>."""
    return re.findall(r'<@(U[A-Z0-9]+)>', text or '')


def has_image(msg):
    """Check if a message has an image attachment."""
    for f in msg.get("files", []):
        if f.get("mimetype", "").startswith("image/"):
            return True
    return False


def build_role_map(users, roles_config):
    """Build uid->role mapping from config roles and Slack user list."""
    role_map = {}
    for role_name, names in roles_config.items():
        for rname in names:
            for uid, uname in users.items():
                if uname.lower() == rname.lower():
                    role_map[uid] = role_name
    return role_map


def get_role(uid, role_map):
    return role_map.get(uid, "RBT")


# ── WRITE STATUS ──────────────────────────────────────────────────

def write_status(status_data):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2, ensure_ascii=False)


# ── MAIN ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zen Garden Weekly Points Extractor")
    parser.add_argument("--week", type=int, help="Week number to process (auto-detect if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Calculate points but don't write files")
    args = parser.parse_args()

    # ── Load & validate config ────────────────────────────────────
    config = load_and_validate_config()
    c_hash = config_hash(config)
    channel_name = config.get("channel", "zengarden")

    # ── Determine which week to process ───────────────────────────
    today = datetime.now().date()
    week_entry = None

    if args.week:
        # Explicit week override
        for w in config["weeks"]:
            if w["week_number"] == args.week:
                week_entry = w
                break
        if not week_entry:
            print(f"❌ Week {args.week} not found in config")
            sys.exit(1)
    else:
        # Auto-detect: find most recent week whose end date has passed
        for w in sorted(config["weeks"], key=lambda x: x["week_number"], reverse=True):
            end_date = datetime.strptime(w["end"], "%Y-%m-%d").date()
            if end_date < today:
                week_entry = w
                break
        if not week_entry:
            print("❌ No completed week found. All weeks are in the future.")
            print("   Use --week N to process a specific week.")
            sys.exit(1)

    week_num = week_entry["week_number"]
    start_date = datetime.strptime(week_entry["start"], "%Y-%m-%d")
    end_date = datetime.strptime(week_entry["end"], "%Y-%m-%d")
    # Exclusive end: midnight of the day AFTER end date
    end_exclusive = end_date + timedelta(days=1)

    week_label = f"Week {week_num} ({start_date.strftime('%b %-d')} – {end_date.strftime('%b %-d')})"
    date_range = f"{start_date.strftime('%b %-d')} – {end_date.strftime('%b %-d')}"

    print(f"📅 {week_label}")
    print(f"📺 Channel: #{channel_name}")
    print(f"🔧 Config hash: {c_hash}")
    if args.dry_run:
        print("🧪 DRY RUN — no files will be written\n")
    else:
        print()

    # ── Check skip ────────────────────────────────────────────────
    if week_entry.get("skip"):
        reason = week_entry.get("skip_reason", "No reason given")
        print(f"⏭️  Week {week_num} SKIPPED: {reason}")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "skipped",
            "week_number": week_num,
            "week_label": week_label,
            "skip_reason": reason,
            "config_hash": c_hash,
        })
        return

    # ── Connect to Slack ──────────────────────────────────────────
    bot_client, user_client = make_clients()

    print("🔍 Fetching channel & users...")
    channel_id = get_channel_id(bot_client, channel_name)
    users = get_users(user_client)
    print(f"   Found {len(users)} users")

    # ── Build role map from config ────────────────────────────────
    role_map = build_role_map(users, config.get("roles", {}))

    # ── Timestamp boundaries ──────────────────────────────────────
    oldest_ts = start_date.timestamp()
    latest_ts = end_exclusive.timestamp()

    # ── Point tracking ────────────────────────────────────────────
    points = defaultdict(int)
    breakdown = defaultdict(lambda: {"posts": 0, "comments": 0, "group_activities": 0})
    group_activity_threads = set()

    # ── Fetch messages ────────────────────────────────────────────
    print("📨 Fetching messages...")
    messages = get_all_messages(bot_client, channel_id, oldest_ts, latest_ts)
    print(f"   Found {len(messages)} top-level messages\n")

    # ── PASS 1: Top-level messages ────────────────────────────────
    print("⚙️  Processing top-level messages...")
    group_count = 0
    post_count = 0

    for msg in messages:
        if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
            continue

        uid = msg.get("user")
        if not uid or uid not in users:
            continue

        text = msg.get("text", "")
        mentions = extract_mentions(text)
        photo = has_image(msg)
        valid_mentions = [m for m in mentions if m in users and m != uid]

        if photo and len(valid_mentions) > 0:
            # GROUP ACTIVITY (5 pts)
            group_activity_threads.add(msg.get("ts"))
            points[uid] += 5
            breakdown[uid]["group_activities"] += 1
            for m_uid in valid_mentions:
                points[m_uid] += 5
                breakdown[m_uid]["group_activities"] += 1
            group_count += 1
            poster = users[uid].split(" ")[0]
            tagged = [users[m].split(" ")[0] for m in valid_mentions]
            print(f"   🎯 Group activity by {poster} → tagged: {', '.join(tagged)}")
        else:
            # REGULAR POST (1 pt)
            points[uid] += 1
            breakdown[uid]["posts"] += 1
            post_count += 1

    print(f"\n   {post_count} regular posts (1 pt each)")
    print(f"   {group_count} group activities (5 pts each)\n")

    # ── PASS 2: Thread replies ────────────────────────────────────
    print("💬 Processing thread replies...")
    comment_count = 0

    for msg in messages:
        if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
            continue
        thread_ts = msg.get("ts")
        if thread_ts in group_activity_threads:
            continue
        if msg.get("reply_count", 0) == 0:
            continue

        parent_uid = msg.get("user")
        replies = get_replies(bot_client, channel_id, thread_ts, oldest_ts, latest_ts)
        credited = set()

        for reply in replies:
            ruid = reply.get("user")
            if not ruid or ruid not in users:
                continue
            if ruid == parent_uid:
                continue
            if ruid in credited:
                continue
            points[ruid] += 2
            breakdown[ruid]["comments"] += 1
            credited.add(ruid)
            comment_count += 1

    print(f"   {comment_count} unique comments scored (2 pts each)\n")

    # ── Build scores ──────────────────────────────────────────────
    print("📊 Building scoreboard...\n")

    scores = []
    for uid, total in sorted(points.items(), key=lambda x: -x[1]):
        name = users[uid]
        role = get_role(uid, role_map)
        bd = breakdown[uid]
        scores.append({
            "name": name,
            "points": total,
            "role": role,
            "posts": bd["posts"],
            "comments": bd["comments"],
            "group_activities": bd["group_activities"],
        })
        first = name.split(" ")[0]
        print(f"  {first:<20} {total:>3} pts  "
              f"(posts:{bd['posts']}  comments:{bd['comments']}  "
              f"group:{bd['group_activities']})")

    total_pts = sum(s["points"] for s in scores)

    # ── Generate Slack-ready summary ──────────────────────────────
    summary_lines = [
        f"🌿 Zen Garden Weekly Points — {week_label}",
        "",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(scores[:3]):
        medal = medals[i] if i < 3 else f"{i+1}."
        summary_lines.append(f"{medal} {s['name']} — {s['points']} pts")
    summary_lines.append("")
    summary_lines.append(f"{len(scores)} people earned points · {total_pts} total pts")
    summary_text = "\n".join(summary_lines)

    print(f"\n{'─' * 50}")
    print(summary_text)
    print(f"{'─' * 50}\n")

    # ── DRY RUN: stop here ────────────────────────────────────────
    if args.dry_run:
        print("🧪 DRY RUN complete — no files written")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "dry_run",
            "week_number": week_num,
            "week_label": week_label,
            "date_window": {
                "start": start_date.isoformat(),
                "end": end_exclusive.isoformat(),
            },
            "config_hash": c_hash,
            "people_scored": len(scores),
            "total_points": total_pts,
            "group_activities_found": group_count,
            "outputs": [],
            "errors": [],
        })
        return

    # ── WRITE OUTPUTS ─────────────────────────────────────────────
    outputs = []

    # 1. CSV to weekly_reports/ (always — works in GitHub Actions too)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    csv_name = f"week{week_num}_{start_date.strftime('%b%-d').lower()}-{end_date.strftime('%-d')}.csv"
    csv_path = os.path.join(REPORTS_DIR, csv_name)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "role", "points", "posts", "comments", "group_activities"])
        for s in scores:
            writer.writerow([s["name"], s["role"], s["points"],
                             s["posts"], s["comments"], s["group_activities"]])
    outputs.append(csv_path)
    print(f"📋 CSV → {csv_path}")

    # 1b. Also save to Google Drive if available (local only)
    gdrive_csv = None
    if os.path.isdir(GDRIVE_CSV_DIR):
        gdrive_csv = os.path.join(GDRIVE_CSV_DIR, csv_name)
        with open(gdrive_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "role", "points", "posts", "comments", "group_activities"])
            for s in scores:
                writer.writerow([s["name"], s["role"], s["points"],
                                 s["posts"], s["comments"], s["group_activities"]])
        outputs.append(gdrive_csv)
        print(f"📋 Google Drive CSV → {gdrive_csv}")

    # 2. Cumulative JSON
    existing_weeks = []
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
            existing_weeks = existing.get("weeks", [])

    # Idempotent: remove this week if re-running
    existing_weeks = [w for w in existing_weeks if w["week_number"] != week_num]
    existing_weeks.append({
        "week_number": week_num,
        "week_label": f"Week {week_num}",
        "date_range": date_range,
        "scores": scores,
    })
    existing_weeks.sort(key=lambda w: w["week_number"])

    # Sum cumulative totals
    cumulative = defaultdict(lambda: {"points": 0, "role": "RBT", "this_week": 0})
    for week in existing_weeks:
        for s in week["scores"]:
            cumulative[s["name"]]["points"] += s["points"]
            cumulative[s["name"]]["role"] = s["role"]
    latest = existing_weeks[-1]
    for s in latest["scores"]:
        cumulative[s["name"]]["this_week"] = s["points"]

    cumulative_scores = []
    for name, data in sorted(cumulative.items(), key=lambda x: -x[1]["points"]):
        cumulative_scores.append({
            "name": name,
            "points": data["points"],
            "role": data["role"],
            "this_week": data["this_week"],
        })

    output_json = {
        "month": config["month"],
        "current_week": max(w["week_number"] for w in existing_weeks),
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
        "weeks": existing_weeks,
        "cumulative": cumulative_scores,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output_json, f, indent=2, ensure_ascii=False)
    outputs.append(JSON_FILE)
    print(f"✅ JSON → {JSON_FILE}")

    # 3. Slack-ready summary text
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(summary_text + "\n")
    outputs.append(SUMMARY_FILE)
    print(f"💬 Summary → {SUMMARY_FILE}")

    # 4. Run status (audit artifact)
    write_status({
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "status": "success",
        "week_number": week_num,
        "week_label": week_label,
        "date_window": {
            "start": start_date.isoformat(),
            "end": end_exclusive.isoformat(),
        },
        "config_hash": c_hash,
        "people_scored": len(scores),
        "total_points": total_pts,
        "group_activities_found": group_count,
        "csv_path": csv_name,
        "outputs": [os.path.basename(o) for o in outputs],
        "errors": [],
    })
    print(f"📝 Status → {STATUS_FILE}")

    print(f"\n✅ Done — {len(scores)} people, {total_pts} cumulative pts")


if __name__ == "__main__":
    main()
