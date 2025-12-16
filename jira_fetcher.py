from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests
from requests.auth import HTTPBasicAuth
import time
from datetime import datetime
import pandas as pd
import io
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for the frontend

# Get credentials from environment variables
email = os.environ.get("JIRA_EMAIL")
api_token = os.environ.get("JIRA_API_TOKEN")
base_url = os.environ.get("JIRA_BASE_URL", "https://aehr.atlassian.net")

def validate_date(date_str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Jira Worklog API is running"})

@app.route("/worklogs", methods=["GET"])
def get_worklogs():
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not start_date or not end_date:
        return jsonify({"error": "Missing start or end date"}), 400

    if not (validate_date(start_date) and validate_date(end_date)):
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    print(f"Fetching worklogs from {start_date} to {end_date}")

    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    jql = f'project = AT AND worklogDate >= {start_date} AND worklogDate <= {end_date}'
    search_url = f"{base_url}/rest/api/3/search/jql"

    start_at = 0
    all_issues = []

    # Fetch all issues
    while True:
        query = {
            'jql': jql,
            'fields': ['summary', 'key'],
            'maxResults': 50,
            'startAt': start_at
        }
        time.sleep(0.05)
        
        try:
            response = requests.get(search_url, headers=headers, params=query, auth=auth, timeout=30)
        except Exception as e:
            print(f"Error fetching issues: {e}")
            return jsonify({"error": f"Failed to fetch issues: {str(e)}"}), 500

        if response.status_code != 200:
            print(f"Jira API error: {response.status_code} - {response.text}")
            return jsonify({"error": f"Jira API error: {response.text}"}), response.status_code

        data = response.json()
        issues = data.get("issues", [])
        total = data.get("total", 0)

        if not issues:
            break

        all_issues.extend(issues)
        start_at += len(issues)

        if start_at >= total:
            break

    print(f"Found {len(all_issues)} issues")

    worklog_rows = []
    no_worklog = []
    total_hours_per_project = {}

    # Fetch worklogs for each issue
    for issue in all_issues:
        issue_key = issue["key"]
        worklog_url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
        
        try:
            worklog_response = requests.get(worklog_url, headers=headers, auth=auth, timeout=30)
        except Exception as e:
            print(f"Error fetching worklogs for {issue_key}: {e}")
            continue

        if worklog_response.status_code != 200:
            print(f"Error fetching worklogs for {issue_key}: {worklog_response.status_code}")
            continue

        worklogs = worklog_response.json().get("worklogs", [])
        if not worklogs:
            no_worklog.append(issue_key)
            continue

        summary = issue.get("fields", {}).get("summary", "N/A")

        for log in worklogs:
            log_date = log["started"][:10]
            if start_date <= log_date <= end_date:
                author = log["author"]["displayName"]
                hours = log.get("timeSpentSeconds", 0) / 3600
                worklog_rows.append({
                    "Date": log_date,
                    "Author": author,
                    "Hours": round(hours, 2),
                    "Project": summary,
                    "Issue Key": issue_key
                })
                total_hours_per_project[summary] = total_hours_per_project.get(summary, 0) + hours

    print(f"Found {len(worklog_rows)} worklog entries")

    # Sort by project
    worklog_rows.sort(key=lambda x: x["Project"])

    return jsonify({
        "worklogs": worklog_rows,
        "no_worklog": no_worklog,
        "total_hours": {k: round(v, 2) for k, v in total_hours_per_project.items()}
    })

@app.route("/worklogs/excel", methods=["GET"])
def get_worklogs_excel():
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not start_date or not end_date:
        return jsonify({"error": "Missing start or end date"}), 400

    if not (validate_date(start_date) and validate_date(end_date)):
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    print(f"Generating Excel for {start_date} to {end_date}")

    auth = HTTPBasicAuth(email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    jql = f'project = AT AND worklogDate >= {start_date} AND worklogDate <= {end_date}'
    search_url = f"{base_url}/rest/api/3/search/jql"

    start_at = 0
    all_issues = []

    # Fetch all issues
    while True:
        query = {
            'jql': jql,
            'fields': ['summary', 'key'],
            'maxResults': 50,
            'startAt': start_at
        }
        time.sleep(0.05)
        
        try:
            response = requests.get(search_url, headers=headers, params=query, auth=auth, timeout=30)
        except Exception as e:
            print(f"Error fetching issues: {e}")
            return jsonify({"error": f"Failed to fetch issues: {str(e)}"}), 500

        if response.status_code != 200:
            print(f"Jira API error: {response.status_code} - {response.text}")
            return jsonify({"error": f"Jira API error: {response.text}"}), response.status_code

        data = response.json()
        issues = data.get("issues", [])
        total = data.get("total", 0)

        if not issues:
            break

        all_issues.extend(issues)
        start_at += len(issues)

        if start_at >= total:
            break

    print(f"Found {len(all_issues)} issues")

    worklog_rows = []
    no_worklog = []
    total_hours_per_project = {}

    # Fetch worklogs for each issue
    for issue in all_issues:
        issue_key = issue["key"]
        worklog_url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
        
        try:
            worklog_response = requests.get(worklog_url, headers=headers, auth=auth, timeout=30)
        except Exception as e:
            print(f"Error fetching worklogs for {issue_key}: {e}")
            continue

        if worklog_response.status_code != 200:
            print(f"Error fetching worklogs for {issue_key}: {worklog_response.status_code}")
            continue

        worklogs = worklog_response.json().get("worklogs", [])
        if not worklogs:
            no_worklog.append(issue_key)
            continue

        summary = issue.get("fields", {}).get("summary", "N/A")

        for log in worklogs:
            log_date = log["started"][:10]
            if start_date <= log_date <= end_date:
                author = log["author"]["displayName"]
                hours = log.get("timeSpentSeconds", 0) / 3600
                worklog_rows.append({
                    "Date": log_date,
                    "Author": author,
                    "Hours": round(hours, 2),
                    "Project": summary,
                    "Issue Key": issue_key
                })
                total_hours_per_project[summary] = total_hours_per_project.get(summary, 0) + hours

    print(f"Found {len(worklog_rows)} worklog entries")

    # Sort by project
    worklog_rows.sort(key=lambda x: x["Project"])

    # Create Excel file
    output = io.BytesIO()
    
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Worklogs sheet
            if worklog_rows:
                df_worklogs = pd.DataFrame(worklog_rows)
                df_worklogs.to_excel(writer, sheet_name='Worklogs', index=False)
            else:
                df_worklogs = pd.DataFrame(columns=["Date", "Author", "Hours", "Project", "Issue Key"])
                df_worklogs.to_excel(writer, sheet_name='Worklogs', index=False)
            
            # Summary sheet
            if total_hours_per_project:
                df_summary = pd.DataFrame([
                    {"Project": k, "Total Hours": round(v, 2)} 
                    for k, v in total_hours_per_project.items()
                ])
                df_summary.to_excel(writer, sheet_name='Summary', index=False)
            else:
                df_summary = pd.DataFrame(columns=["Project", "Total Hours"])
                df_summary.to_excel(writer, sheet_name='Summary', index=False)
            
            # No worklog sheet
            if no_worklog:
                df_no_worklog = pd.DataFrame({"Issue Key": no_worklog})
                df_no_worklog.to_excel(writer, sheet_name='No Worklogs', index=False)
            else:
                df_no_worklog = pd.DataFrame(columns=["Issue Key"])
                df_no_worklog.to_excel(writer, sheet_name='No Worklogs', index=False)
        
        output.seek(0)
        
        filename = f"worklogs_{start_date}_to_{end_date}.xlsx"
        print(f"Excel file generated: {filename}")
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"Error creating Excel: {e}")
        return jsonify({"error": f"Failed to create Excel file: {str(e)}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)