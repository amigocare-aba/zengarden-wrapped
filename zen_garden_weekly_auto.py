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
import urllib.request
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
    # Try loading from .env file — but DO NOT override env vars that are already set
    # (even to empty string). This lets `SLACK_WEBHOOK_URL= python script.py` actually
    # disable the webhook for safe testing.
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())
        BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
        USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")

    if not BOT_TOKEN or not USER_TOKEN:
        print("❌ Missing Slack tokens. Set SLACK_BOT_TOKEN and SLACK_USER_TOKEN as environment variables.")
        print("   Or create a .env file with:")
        print("     SLACK_BOT_TOKEN=xoxb-...")
        print("     SLACK_USER_TOKEN=xoxp-...")
        sys.exit(1)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def post_to_slack(text):
    """Post a message to Slack via incoming webhook."""
    if not SLACK_WEBHOOK_URL:
        print("⚠️  No SLACK_WEBHOOK_URL set — skipping Slack post")
        return False
    try:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        print("📣 Posted summary to Slack")
        return True
    except Exception as e:
        print(f"⚠️  Slack post failed: {e}")
        return False


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
    for field in ["year", "weeks", "roles"]:
        if field not in config:
            errors.append(f"Missing required field: '{field}'")

    # Validate weeks array
    weeks = config.get("weeks", [])
    if not isinstance(weeks, list) or len(weeks) == 0:
        errors.append("'weeks' must be a non-empty array")
    else:
        seen_pairs = set()
        for i, week in enumerate(weeks):
            prefix = f"weeks[{i}]"

            for field in ["week_number", "start", "end", "skip"]:
                if field not in week:
                    errors.append(f"{prefix}: missing '{field}'")

            wn = week.get("week_number")
            month = week.get("month", "")
            if not isinstance(wn, int) or wn < 1:
                errors.append(f"{prefix}: 'week_number' must be a positive integer")
            else:
                pair = (month, wn)
                if pair in seen_pairs:
                    errors.append(f"{prefix}: duplicate (month={month}, week_number={wn})")
                else:
                    seen_pairs.add(pair)

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

# ── MAIN ───────────────────────────────────────────────────────────

def _current_month_label(today, config):
    """Find the month whose start/end date range includes `today`."""
    for label, info in config.get("months", {}).items():
        try:
            start = datetime.strptime(info["start"], "%Y-%m-%d").date()
            end = datetime.strptime(info["end"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if start <= today <= end:
            return label
    return None


def _cap_name(nm):
    return " ".join(w.capitalize() if w[0].islower() else w for w in nm.split())


def _process_messages_for_points(bot_client, user_client, channel_name, oldest_ts, latest_ts, role_map):
    """Fetch messages in [oldest_ts, latest_ts) and compute per-user points (1/2/5 rules).
    Returns dict of scores list + aggregate counts.
    """
    channel_id = get_channel_id(bot_client, channel_name)
    users = get_users(user_client)

    points = defaultdict(int)
    breakdown = defaultdict(lambda: {"posts": 0, "comments": 0, "group_activities": 0})
    group_activity_threads = set()

    messages = get_all_messages(bot_client, channel_id, oldest_ts, latest_ts)

    # Pass 1: top-level messages
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
            group_activity_threads.add(msg.get("ts"))
            points[uid] += 5
            breakdown[uid]["group_activities"] += 1
            for m_uid in valid_mentions:
                points[m_uid] += 5
                breakdown[m_uid]["group_activities"] += 1
        else:
            points[uid] += 1
            breakdown[uid]["posts"] += 1

    # Pass 2: thread replies (unique per thread per replier, skip group threads)
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
            if ruid == parent_uid or ruid in credited:
                continue
            points[ruid] += 2
            breakdown[ruid]["comments"] += 1
            credited.add(ruid)

    # Build scores list
    scores = []
    for uid, total in sorted(points.items(), key=lambda x: -x[1]):
        nm = users[uid]
        scores.append({
            "name": nm,
            "points": total,
            "role": get_role(uid, role_map),
            "posts": breakdown[uid]["posts"],
            "comments": breakdown[uid]["comments"],
            "group_activities": breakdown[uid]["group_activities"],
        })

    return {
        "scores": scores,
        "total_points": sum(points.values()),
        "people_scored": len(scores),
        "group_activities_found": len(group_activity_threads),
        "message_count": len(messages),
    }


def run_weekly_leaderboard(args, config, target_month, today, posting_enabled):
    """Post the running-total leaderboard for `target_month` (from month start through yesterday)."""
    month_info = config["months"][target_month]
    month_start = datetime.strptime(month_info["start"], "%Y-%m-%d")
    yesterday = today - timedelta(days=1)
    # End is end-of-day yesterday (start-of-day today, exclusive)
    end_exclusive = datetime.combine(today, datetime.min.time())

    date_range_str = f"{month_start.strftime('%b %-d')} – {yesterday.strftime('%b %-d')}"
    print(f"📅 Weekly leaderboard — {target_month} · {date_range_str}\n")

    bot_client, user_client = make_clients()
    print("🔍 Fetching channel & users...")
    users_all = get_users(user_client)
    role_map = build_role_map(users_all, config.get("roles", {}))
    print(f"   Found {len(users_all)} users\n")

    print("📨 Fetching messages...")
    result = _process_messages_for_points(
        bot_client, user_client, config.get("channel", "zengarden"),
        month_start.timestamp(), end_exclusive.timestamp(), role_map
    )
    print(f"   Processed {result['message_count']} top-level messages\n")

    scores = result["scores"]
    total_pts = result["total_points"]
    people = result["people_scored"]

    print(f"📊 Top 10 for {target_month}:")
    for i, s in enumerate(scores[:10], 1):
        first = s["name"].split(" ")[0]
        print(f"   {i:>2}. {first:<20} {s['points']:>3} pts")

    month_short = target_month.split()[0]
    top3 = scores[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i]} {_cap_name(p['name'])} — {p['points']} pts" for i, p in enumerate(top3)]
    page_url = "https://amigocare-aba.github.io/zengarden-wrapped/weekly.html"

    slack_msg = (
        f"*{month_short} Progress — through {yesterday.strftime('%b %-d')}* 🌱\n\n"
        + "\n".join(lines)
        + f"\n\n{people} people · {total_pts} pts so far this month"
        + f"\n\n📊 <{page_url}|View full scoreboard>"
    )
    print("\n" + "─" * 60)
    print(slack_msg)
    print("─" * 60)

    if args.dry_run:
        print("\n🧪 DRY RUN — no files written, no post sent")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "dry_run",
            "action": "weekly",
            "month": target_month,
            "date_range": date_range_str,
            "people_scored": people,
            "total_points": total_pts,
        })
        return

    # Update weekly.json — reflect current month's cumulative
    weekly_out = {
        "month": target_month,
        "current_week": None,
        "date_range": date_range_str,
        "updated_at": today.isoformat(),
        "weeks": [],
        "cumulative": [
            {"name": s["name"], "points": s["points"], "role": s["role"], "this_week": s["points"]}
            for s in scores
        ],
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(weekly_out, f, indent=2, ensure_ascii=False)
    print(f"\n✅ JSON → {JSON_FILE}")

    # Save weekly CSV
    os.makedirs(REPORTS_DIR, exist_ok=True)
    slug = target_month.lower().replace(" ", "_")
    csv_name = f"{slug}_through_{yesterday.strftime('%b%-d').lower()}.csv"
    csv_path = os.path.join(REPORTS_DIR, csv_name)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "role", "points", "posts", "comments", "group_activities"])
        for s in scores:
            w.writerow([s["name"], s["role"], s["points"],
                        s["posts"], s["comments"], s["group_activities"]])
    print(f"📋 CSV → {csv_path}")

    # Also save to Google Drive if accessible (local Mac only)
    if os.path.isdir(GDRIVE_CSV_DIR):
        gdrive_csv = os.path.join(GDRIVE_CSV_DIR, csv_name)
        with open(gdrive_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "role", "points", "posts", "comments", "group_activities"])
            for s in scores:
                w.writerow([s["name"], s["role"], s["points"],
                            s["posts"], s["comments"], s["group_activities"]])
        print(f"📋 Google Drive CSV → {gdrive_csv}")

    # Post to Slack
    if posting_enabled:
        post_to_slack(slack_msg)
    else:
        print("\n🔇 Slack post SUPPRESSED (--no-post or --simulate-date)")

    write_status({
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "status": "success" if posting_enabled else "no_post",
        "action": "weekly",
        "month": target_month,
        "date_range": date_range_str,
        "people_scored": people,
        "total_points": total_pts,
    })
    print(f"\n✅ Done — {people} people, {total_pts} pts through {yesterday.strftime('%b %-d')}")


def run_monthly_recap(args, config, target_month, today, posting_enabled):
    """Run wrapped extractor for target_month, then post monthly Slack recap."""
    print(f"🌱 Monthly recap — {target_month}\n")
    print("   Running wrapped extraction...")
    import subprocess
    result = subprocess.run(
        ["python3", os.path.join(SCRIPT_DIR, "zen_garden_wrapped_auto.py"),
         "--month", target_month],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("⚠️  Wrapped extraction failed:")
        print(result.stderr)
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "wrapped_failed",
            "action": "monthly",
            "month": target_month,
            "error": result.stderr[:500],
        })
        sys.exit(1)
    print("   ✓ Wrapped data generated")

    wrapped_json_path = os.path.join(SCRIPT_DIR, "zen_garden_wrapped_data.json")
    if not os.path.exists(wrapped_json_path):
        print("⚠️  No wrapped JSON found after extraction.")
        sys.exit(1)
    with open(wrapped_json_path) as f:
        wrapped = json.load(f)

    top3 = wrapped.get("all_active", [])[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i]} {_cap_name(p['name'])} — {p['pts']} pts" for i, p in enumerate(top3)]

    wrapped_url = "https://amigocare-aba.github.io/zengarden-wrapped/index_wrapped.html"
    form_url = config.get("exchange_form_url", "")
    month_short = target_month.split()[0]
    close_dt = today + timedelta(days=7)
    close_str = close_dt.strftime("%A %B %-d")

    monthly_msg = (
        f"*{month_short} Wrapped — final monthly totals* 🌱\n\n"
        + "\n".join(lines)
        + f"\n\nSee your full {month_short} recap → <{wrapped_url}|wrapped page>"
        + "\nDon't forget to tap your name to share your results on social! 📸"
        + f"\n\n🎁 Redeem your points → <{form_url}|form>"
        + f"\nForm closes {close_str}"
    )
    print("\n" + "─" * 60)
    print(monthly_msg)
    print("─" * 60)

    if args.dry_run:
        print("\n🧪 DRY RUN — no post sent")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "dry_run",
            "action": "monthly",
            "month": target_month,
        })
        return

    if posting_enabled:
        post_to_slack(monthly_msg)
    else:
        print("\n🔇 Slack post SUPPRESSED (--no-post or --simulate-date)")

    # Reset the weekly.json for the new month — will be populated by next Sunday
    reset_json = {
        "month": None,
        "current_week": None,
        "date_range": None,
        "updated_at": today.isoformat(),
        "weeks": [],
        "cumulative": [],
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(reset_json, f, indent=2, ensure_ascii=False)
    print(f"\n♻️  Reset {JSON_FILE} for new month")

    write_status({
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "status": "success" if posting_enabled else "no_post",
        "action": "monthly",
        "month": target_month,
        "form_url": form_url,
        "wrapped_url": wrapped_url,
    })
    print(f"\n✅ Done — {target_month} recap posted")


def main():
    parser = argparse.ArgumentParser(description="Zen Garden — daily runner (weekly leaderboards + monthly recaps)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files or posting")
    parser.add_argument("--simulate-date", help="Simulate today's date as YYYY-MM-DD (auto-suppresses Slack posting)")
    parser.add_argument("--no-post", action="store_true", help="Do everything except post to Slack")
    parser.add_argument("--force-weekly", action="store_true", help="Force weekly leaderboard post (ignore day-of-week check)")
    parser.add_argument("--force-monthly", type=str, help='Force monthly recap for given month, e.g. "July 2026"')
    args = parser.parse_args()

    # Load & validate config
    config = load_and_validate_config()

    today_str = args.simulate_date if args.simulate_date else datetime.now().date().isoformat()
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    posting_enabled = not (args.no_post or args.simulate_date)

    if args.simulate_date:
        print(f"🎭 SIMULATING TODAY = {today_str}\n")

    # Determine action
    if args.force_monthly:
        action = "monthly"
        target_month = args.force_monthly
    elif args.force_weekly:
        action = "weekly"
        target_month = _current_month_label(today, config)
    elif today.day == 1:
        action = "monthly"
        prev_day = today - timedelta(days=1)
        target_month = _current_month_label(prev_day, config)
    elif today.weekday() == 6:  # Sunday
        action = "weekly"
        target_month = _current_month_label(today, config)
    else:
        print(f"📅 {today_str} is neither Sunday nor the 1st of a month — no post today.")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "no_op",
            "reason": "not_sunday_or_first",
            "date": today_str,
        })
        return

    if not target_month:
        print(f"❌ Could not resolve target month for {today_str}. Check weekly_config.json.")
        write_status({
            "last_run": datetime.now().isoformat(timespec="seconds"),
            "status": "config_error",
            "date": today_str,
        })
        sys.exit(1)

    print(f"📅 {today_str} → {action.upper()} for {target_month}\n")

    if action == "monthly":
        run_monthly_recap(args, config, target_month, today, posting_enabled)
    else:
        run_weekly_leaderboard(args, config, target_month, today, posting_enabled)


if __name__ == "__main__":
    main()
