import io
import os
import time
import concurrent.futures

import requests
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file, abort
from dotenv import load_dotenv

# Load Jira gateway credentials from .env (sitting next to this script).
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

api_token = os.environ.get("JIRA_API_TOKEN")
cloud_id = os.environ.get("JIRA_CLOUD_ID")
if not api_token or not cloud_id:
    raise SystemExit(
        "Missing JIRA_API_TOKEN or JIRA_CLOUD_ID in .env. "
        "Place a .env next to this script with both values set."
    )

# Atlassian gateway URL — all Jira REST calls go through here with a Bearer token.
base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_token}",
}

ALL_ASSIGNEES = "All Assignees"
ALL = "All"

# UI label -> Jira group name. Lets the dropdown show friendly text while we
# match against the actual Jira groups (note "Interns" is plural in Jira).
EMP_GROUPS = {"Permanent": "Permanent", "Intern": "Interns"}
LOC_GROUPS = {"USA": "USA", "Philippines": "Philippines"}
RELEVANT_GROUPS = set(EMP_GROUPS.values()) | set(LOC_GROUPS.values())


def fetch_user_groups(account_id):
    """Return the set of (relevant) Jira group names this user belongs to."""
    url = f"{base_url}/rest/api/3/user/groups"
    try:
        r = requests.get(url, headers=headers, params={"accountId": account_id}, timeout=30)
        if r.status_code != 200:
            return set()
        return {g.get("name") for g in r.json()} & RELEVANT_GROUPS
    except requests.exceptions.RequestException:
        return set()


def fetch_project_users(project_key="AT"):
    users = []
    start_at = 0
    url = f"{base_url}/rest/api/3/user/assignable/search"
    while True:
        params = {"project": project_key, "startAt": start_at, "maxResults": 50}
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print("Error fetching users:", r.status_code, r.text)
            break
        batch = r.json()
        if not batch:
            break
        users.extend(batch)
        if len(batch) < 50:
            break
        start_at += len(batch)
    active = sorted(
        [u for u in users if u.get("active") and u.get("accountType") == "atlassian"],
        key=lambda u: u["displayName"].lower(),
    )
    # Attach each user's group membership (employment type / location) so we
    # can filter by it. Done concurrently to keep startup reasonable.
    print(f"Loading group membership for {len(active)} users...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        group_sets = list(ex.map(lambda u: fetch_user_groups(u["accountId"]), active))
    for u, groups in zip(active, group_sets):
        u["groups"] = groups
    return active


def user_matches(user, emp_group, loc_group):
    """True if the user satisfies the selected employment-type and location
    group filters. A None filter means 'no restriction'."""
    groups = user.get("groups", set())
    if emp_group and emp_group not in groups:
        return False
    if loc_group and loc_group not in groups:
        return False
    return True


def label_for_groups(groups, group_map):
    """Map a user's group set to its friendly UI label for a category
    (employment type or location); blank if none of the groups apply."""
    for label, group_name in group_map.items():
        if group_name in groups:
            return label
    return ""


def fetch_worklogs(start_date, end_date, selected_user, allowed_ids=None):
    """Returns (worklog_rows, no_worklog_keys, totals_by_project).

    allowed_ids: optional set of author accountIds to keep. None means keep all.
    Used to apply the employment-type / location group filters when no single
    assignee is selected.
    """
    jql = f'project = AT AND worklogDate >= "{start_date}" AND worklogDate <= "{end_date}"'
    if selected_user is not None:
        jql += f' AND worklogAuthor = "{selected_user["accountId"]}"'

    search_url = f"{base_url}/rest/api/3/search/jql"
    print("Fetching issues with JQL:", jql)

    start_at = 0
    all_issues = []
    while True:
        query = {"jql": jql, "fields": ["summary", "key"], "maxResults": 50, "startAt": start_at}
        time.sleep(0.05)
        try:
            response = requests.get(search_url, headers=headers, params=query, timeout=30)
        except requests.exceptions.Timeout:
            print("Request timed out at issue", start_at)
            break
        except requests.exceptions.RequestException as e:
            print("Request failed:", e)
            break
        if response.status_code != 200:
            print("Error fetching issues:", response.status_code, response.text)
            break
        data = response.json()
        issues = data.get("issues", [])
        total = data.get("total", 0)
        if not issues:
            break
        all_issues.extend(issues)
        start_at += len(issues)
        if start_at >= total:
            break

    print(f"Total issues collected: {len(all_issues)}")

    worklog_rows = []
    no_worklog = []
    # accountId -> total logged hours in range (used by the Narc report, which
    # needs per-user totals keyed by id rather than by display name).
    hours_by_account = {}
    selected_account_id = selected_user["accountId"] if selected_user else None
    # accountId -> group set, so each worklog row can show employment/location.
    groups_by_id = {u["accountId"]: u.get("groups", set()) for u in project_users}

    for issue in all_issues:
        issue_key = issue["key"]
        worklog_url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
        worklog_response = requests.get(worklog_url, headers=headers, timeout=30)
        if worklog_response.status_code != 200:
            print(f"Error fetching worklogs for {issue_key}: {worklog_response.status_code}")
            continue
        worklogs = worklog_response.json().get("worklogs", [])
        if not worklogs:
            no_worklog.append(issue_key)
            continue
        summary = issue.get("fields", {}).get("summary", "N/A")
        for log in worklogs:
            started = log["started"][:10]
            if not (start_date <= started <= end_date):
                continue
            author_id = log["author"].get("accountId")
            if selected_account_id and author_id != selected_account_id:
                continue
            if allowed_ids is not None and author_id not in allowed_ids:
                continue
            hours = log.get("timeSpentSeconds", 0) / 3600
            hours_by_account[author_id] = hours_by_account.get(author_id, 0) + hours
            author_groups = groups_by_id.get(author_id, set())
            worklog_rows.append({
                "Date": started,
                "Author": log["author"]["displayName"],
                "Employment": label_for_groups(author_groups, EMP_GROUPS),
                "Location": label_for_groups(author_groups, LOC_GROUPS),
                "Hours": round(hours, 2),
                "Project": summary,
                "Issue Key": issue_key,
            })

    worklog_rows.sort(key=lambda x: x["Project"])
    totals = {}
    user_totals = {}
    for row in worklog_rows:
        totals[row["Project"]] = totals.get(row["Project"], 0) + row["Hours"]
        user_totals[row["Author"]] = user_totals.get(row["Author"], 0) + row["Hours"]
    return worklog_rows, no_worklog, totals, user_totals, hours_by_account


# ---------------------------------------------------------------------------
# Request-handling helpers (replace the old Tkinter get_inputs / dropdowns)
# ---------------------------------------------------------------------------

def assignees_for_filters(emp_group, loc_group):
    """List of {accountId, label} for users matching the group filters,
    prefixed with the 'All Assignees' sentinel (accountId == '')."""
    matching = [u for u in project_users if user_matches(u, emp_group, loc_group)]
    out = [{"accountId": "", "label": ALL_ASSIGNEES}]
    for u in matching:
        out.append({
            "accountId": u["accountId"],
            "label": f'{u["displayName"]} <{u.get("emailAddress", "")}>',
        })
    return out


def parse_request_args(args):
    """Pull and validate the query params shared by /view and /download.

    Returns (start, end, selected_user, allowed_ids) or raises ValueError with
    a user-facing message.
    """
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    if not start or not end:
        raise ValueError("Please choose both a start and end date.")
    if start > end:
        raise ValueError("Start date must be on or before End date.")

    emp_group = EMP_GROUPS.get(args.get("employment", ALL))
    loc_group = LOC_GROUPS.get(args.get("location", ALL))

    account_id = (args.get("assignee") or "").strip()
    selected = None
    if account_id:
        selected = next((u for u in project_users if u["accountId"] == account_id), None)
        if selected is None:
            raise ValueError("Selected assignee not found.")

    if selected is not None:
        allowed_ids = {selected["accountId"]}
    elif emp_group or loc_group:
        allowed_ids = {
            u["accountId"]
            for u in project_users
            if user_matches(u, emp_group, loc_group)
        }
    else:
        allowed_ids = None

    return start, end, selected, allowed_ids


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

# Templates live in ../frontend, not the default backend/templates.
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "frontend"),
)
# Pick up template edits without needing a server restart (dev convenience).
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.route("/")
def index():
    return render_template(
        "index.html",
        employment_options=[ALL] + list(EMP_GROUPS.keys()),
        location_options=[ALL] + list(LOC_GROUPS.keys()),
        assignees=assignees_for_filters(None, None),
    )


@app.route("/api/assignees")
def api_assignees():
    """Filtered assignee list, refreshed when employment/location changes."""
    emp_group = EMP_GROUPS.get(request.args.get("employment", ALL))
    loc_group = LOC_GROUPS.get(request.args.get("location", ALL))
    return jsonify(assignees_for_filters(emp_group, loc_group))


@app.route("/api/worklogs")
def api_worklogs():
    try:
        start, end, selected, allowed_ids = parse_request_args(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    rows, _, totals, user_totals, _ = fetch_worklogs(start, end, selected, allowed_ids)
    return jsonify({
        "rows": rows,
        "totals": [{"Project": p, "Total Hours": round(h, 2)} for p, h in totals.items()],
        "user_totals": [
            {"Author": a, "Total Hours": round(h, 2)}
            for a, h in sorted(user_totals.items(), key=lambda x: x[1], reverse=True)
        ],
    })


@app.route("/api/narc")
def api_narc():
    """List in-scope users whose logged hours fall short of the expected total.

    Expected hours are supplied by the caller (expected_hours) — the app does no
    holiday/working-day math, so you can enter whatever your company expects for
    the range. Users who logged nothing are included (biggest shortfall). Honors
    the same employment/location/assignee filters as the worklog view.
    """
    try:
        start, end, selected, allowed_ids = parse_request_args(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        expected = round(float(request.args.get("expected_hours", 0)), 2)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0:
        return jsonify({"error": "Enter the expected hours (a number greater than 0)."}), 400

    _, _, _, _, hours_by_account = fetch_worklogs(start, end, selected, allowed_ids)

    # Everyone the filters allow — not just people who happened to log time.
    if allowed_ids is None:
        in_scope = project_users
    else:
        in_scope = [u for u in project_users if u["accountId"] in allowed_ids]

    offenders = []
    for u in in_scope:
        logged = round(hours_by_account.get(u["accountId"], 0), 2)
        missing = round(expected - logged, 2)
        if missing > 0.01:
            groups = u.get("groups", set())
            offenders.append({
                "Author": u["displayName"],
                "Email": u.get("emailAddress", ""),
                "Employment": label_for_groups(groups, EMP_GROUPS),
                "Location": label_for_groups(groups, LOC_GROUPS),
                "Logged": logged,
                "Expected": expected,
                "Missing": missing,
            })
    offenders.sort(key=lambda x: x["Missing"], reverse=True)

    return jsonify({
        "expected_hours": expected,
        "offenders": offenders,
    })


@app.route("/download")
def download():
    try:
        start, end, selected, allowed_ids = parse_request_args(request.args)
    except ValueError as e:
        abort(400, description=str(e))
    rows, no_worklog, totals, user_totals, _ = fetch_worklogs(start, end, selected, allowed_ids)

    df_w = pd.DataFrame(rows)
    df_n = pd.DataFrame(no_worklog, columns=["Issues with No Worklog"])
    df_t = pd.DataFrame(list(totals.items()), columns=["Project", "Total Hours"])
    df_u = pd.DataFrame(
        sorted(user_totals.items(), key=lambda x: x[1], reverse=True),
        columns=["Author", "Total Hours"],
    )

    # Build the workbook in memory so it streams to the browser's Downloads
    # folder instead of being written to the server's working directory.
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df_w.to_excel(writer, sheet_name="Worklogs", index=False)
        df_n.to_excel(writer, sheet_name="No Worklogs", index=False)
        df_t.to_excel(writer, sheet_name="Total Hours", index=False)
        df_u.to_excel(writer, sheet_name="Hours by User", index=False)
    buf.seek(0)

    filename = f"jira_worklogs_{start}_to_{end}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


print("Loading users...")
project_users = fetch_project_users("AT")
print(f"Loaded {len(project_users)} users. Ready.")


if __name__ == "__main__":
    # host=0.0.0.0 so other people on the network can reach it; threaded for
    # concurrent users. Pick any free port.
    app.run(host="0.0.0.0", port=5000, threaded=True)
