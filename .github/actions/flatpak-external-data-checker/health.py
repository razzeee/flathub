#!/usr/bin/env python3
"""Collect checker coverage and compare results across scheduled runs."""

import argparse
from collections import Counter
import datetime
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def submodules(path, *source):
    return [
        line.split(" ", 1)[1]
        for line in git(
            path, "config", *source, "--get-regexp", r"^submodule\..*\.path$"
        ).splitlines()
        if " " in line
    ]


def start(directory, selected, skipped=None):
    directory.mkdir(parents=True, exist_ok=True)
    write_json(
        directory / "coverage.json",
        {
            "schema_version": 1,
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "run_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
            "workflow_commit": os.environ.get("GITHUB_SHA"),
            "started_at": now(),
            "app_id": os.environ.get("APP_ID") or None,
            "shard": None
            if os.environ.get("APP_ID")
            else (int(os.environ["GITHUB_RUN_NUMBER"]) - 1) % 6,
            "shard_count": 6,
            "uninitialized_submodules": "excluded",
            "selected": selected,
            "skipped": skipped or {},
        },
    )


def record(directory, repo, status, exit_code=None):
    entry = {"repository": repo, "status": status, "at": now()}
    if status == "checking":
        entry.update(
            {
                "branch": git(repo, "branch", "--show-current"),
                "commit": git(repo, "rev-parse", "HEAD"),
                "checkout": str(Path(repo).resolve()),
                "excluded_submodules": submodules(repo, "--file", ".gitmodules"),
            }
        )
    if status == "finished":
        entry["branches"] = {
            ref.removeprefix("refs/remotes/origin/"): {
                "commit": git(repo, "rev-parse", ref),
            }
            for ref in git(
                repo,
                "for-each-ref",
                "--format=%(refname)",
                "refs/remotes/origin/branch/",
            ).splitlines()
        }
    if exit_code is not None:
        entry["exit_code"] = exit_code
    with (directory / "coverage.jsonl").open("a") as output:
        output.write(json.dumps(entry) + "\n")


def restore(directory):
    repository = os.environ["GITHUB_REPOSITORY"]
    current_run = int(os.environ["GITHUB_RUN_ID"])
    pages = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/actions/artifacts?per_page=100",
            ]
        )
    )
    artifacts = [artifact for page in pages for artifact in page["artifacts"]]
    candidates = sorted(
        (
            a
            for a in artifacts
            if a["name"].startswith("checker-health-")
            and not a["expired"]
            and a.get("workflow_run", {}).get("head_branch")
            == os.environ["GITHUB_REF_NAME"]
            and a["workflow_run"]["id"] < current_run
        ),
        key=lambda a: (a["workflow_run"]["id"], a["id"]),
        reverse=True,
    )
    for artifact in candidates:
        data = subprocess.check_output(
            ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"]
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "health-state.json" not in archive.namelist():
                continue
            state = json.loads(archive.read("health-state.json"))
            if state.get("schema_version") != 1:
                raise ValueError("Unsupported health history schema")
            write_json(directory / "previous-state.json", state)
            write_json(
                directory / "history.json",
                {"status": "restored", "artifact_id": artifact["id"]},
            )
            return
    write_json(directory / "history.json", {"status": "empty"})


def excluded(error, context):
    if error.get("type") != "ManifestFileOpenError":
        return False
    message = error.get("message", "")
    checkout = context.get("checkout")
    return bool(checkout) and any(
        f"'{checkout}/{path.rstrip('/')}/" in message
        for path in context.get("excluded_submodules", [])
        if not Path(path).is_absolute() and ".." not in Path(path).parts
    )


def observation_key(repo, branch, location, category):
    return json.dumps([repo, branch, location, category], separators=(",", ":"))


def findings(repo, report, context):
    failures = {}
    checked = set()
    source_errors = set()
    ignored = 0
    branch = report.get("branch") or context.get("branch", "")

    def add_failure(location, category, message):
        key = observation_key(repo, branch, location, category)
        failures[key] = {
            "repository": repo,
            "branch": branch,
            "location": location,
            "category": category,
            "message": message,
        }

    for source in report.get("sources", []):
        location = f"{source['manifest']}#{source['source_pointer']}"
        status = source.get("status")
        if status in ("checked", "update_available", "blocked"):
            checked.add(location)
        errors = source.get("errors", [])
        source_errors.update((e.get("type"), e.get("message")) for e in errors)
        if status == "failed":
            errors = errors or [
                {
                    "type": source.get("reason") or "CheckFailed",
                    "message": "Source check failed",
                }
            ]
        elif status == "unchecked" and source.get("reason") == "no_applicable_checker":
            errors = [
                {
                    "type": "NoApplicableChecker",
                    "message": f"No checker ran for {source.get('checker_type')}",
                }
            ]
        for error in errors:
            add_failure(location, error["type"], error["message"])
    for error in report.get("errors", []):
        if (error.get("type"), error.get("message")) in source_errors:
            continue
        if excluded(error, context):
            ignored += 1
            continue
        # Temporary worktree paths change between runs.
        message = re.sub(r"/tmp/fedc-[^/]+/checkout/", "", error["message"])
        message = (
            message.replace(context.get("checkout", "") + "/", "")
            if context.get("checkout")
            else message
        )
        digest = hashlib.sha256(message.encode()).hexdigest()[:16]
        location = "run:" + digest
        add_failure(location, error["type"], error["message"])
    if report.get("delivery") == "failed" and not failures:
        add_failure("delivery", "DeliveryFailed", "Update delivery failed")
    if report.get("status") == "failed" and not failures and not ignored:
        add_failure("run:unknown", "RunFailed", "Check failed without diagnostics")
    return failures, checked, ignored


def summarize(directory):
    coverage = json.loads((directory / "coverage.json").read_text())
    previous_path = directory / "previous-state.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    state = previous.get("failures", {}).copy()
    repositories = previous.get("repositories", {}).copy()
    history_path = directory / "history.json"
    history = (
        json.loads(history_path.read_text())
        if history_path.exists()
        else {"status": "unavailable"}
    )
    contexts = {repo: {"status": "selected"} for repo in coverage["selected"]}
    contexts.update(
        {
            repo: {"status": status}
            for repo, status in coverage.get("skipped", {}).items()
        }
    )
    events = directory / "coverage.jsonl"
    if events.exists():
        for line in events.read_text().splitlines():
            event = json.loads(line)
            contexts.setdefault(event["repository"], {}).update(event)
    recovered = []
    observed = []
    missing = []
    ignored = 0
    for repo, context in contexts.items():
        if context["status"] == "outside_shard":
            continue
        repository = repositories.setdefault(repo, {})
        repository["status"] = context["status"]
        if repo in coverage["selected"]:
            repository["last_selected"] = coverage["started_at"]
        if context["status"] not in ("checking", "finished"):
            if context["status"] in ("selected", "clone_failed"):
                missing.append(repo)
            continue
        path = directory / f"{repo}.json"
        try:
            report = json.loads(path.read_text())
            if (
                not isinstance(report, dict)
                or report.get("schema_version") != 1
                or not isinstance(report.get("sources"), list)
            ):
                raise ValueError("Invalid report schema")
        except (OSError, ValueError):
            missing.append(repo)
            continue
        repository["last_checked"] = coverage["started_at"]
        repository["run_url"] = coverage["run_url"]
        for result in report.get("branches", [report]):
            branch = result.get("branch") or context.get("branch", "")
            context.setdefault("branch_results", {})[branch] = result.get("status")
            if result.get("status") == "skipped":
                continue
            commit = (
                context.get("branches", {})
                .get(branch, {})
                .get("commit", context.get("commit"))
            )
            failures, checked, count = findings(repo, result, context)
            ignored += count
            for key, failure in failures.items():
                old = state.get(key, {})
                failure.update(
                    {
                        "consecutive_checks": old.get("consecutive_checks", 0) + 1,
                        "first_seen": old.get("first_seen", coverage["started_at"]),
                        "last_seen": coverage["started_at"],
                        "run_url": coverage["run_url"],
                        "commit": commit,
                    }
                )
                state[key] = failure
                observed.append(failure)
            for key, old in list(state.items()):
                if (
                    key in failures
                    or old["repository"] != repo
                    or old["branch"] != branch
                ):
                    continue
                location = old["location"]
                healthy = location in checked
                if location.startswith("run:"):
                    healthy = not failures and (
                        result.get("status") == "completed" or count > 0
                    )
                if location == "delivery":
                    healthy = result.get("delivery") in (
                        "pr_handled",
                        "committed",
                        "edited",
                    )
                if healthy:
                    recovered.append(state.pop(key))
    coverage.update(
        {"finished_at": now(), "repositories": contexts, "missing_reports": missing}
    )
    write_json(directory / "coverage.json", coverage)
    if history["status"] in ("restored", "empty"):
        write_json(
            directory / "health-state.json",
            {
                "schema_version": 1,
                "run_id": coverage["run_id"],
                "run_url": coverage["run_url"],
                "failures": state,
                "repositories": repositories,
            },
        )
    cutoff = datetime.datetime.fromisoformat(
        coverage["started_at"]
    ) - datetime.timedelta(hours=48)
    stale = [
        repo
        for repo, context in repositories.items()
        if context.get("status") in ("finished", "checking", "clone_failed", "selected")
        and context.get("last_checked")
        and datetime.datetime.fromisoformat(context["last_checked"]) < cutoff
    ]
    repeated = [f for f in observed if f["consecutive_checks"] >= 2]
    write_json(
        directory / "triage.json",
        {
            "new_failures": [f for f in observed if f["consecutive_checks"] == 1],
            "repeated_failures": repeated,
            "recovered": recovered,
            "missing_reports": missing,
            "excluded_submodule_errors": ignored,
            "not_checked_in_48_hours": stale,
        },
    )
    lines = [
        "## Checker health",
        "",
        f"Selected repositories: {len(coverage['selected'])}.",
        f"Missing reports or failed clones: {len(missing)}.",
        f"Missing-file errors in excluded submodules: {ignored}.",
        "",
        f"Repositories not checked in 48 hours: {len(stale)}.",
        f"History: {history['status']}.",
        "",
        "Failure counts advance only when that repository and branch are checked.",
        "Skipped repositories and missing reports do not count as recoveries.",
    ]
    dispositions = Counter(context["status"] for context in contexts.values())
    lines.extend(["", "### Coverage", ""])
    lines.extend(
        f"- {status}: {count}" for status, count in sorted(dispositions.items())
    )
    if missing:
        lines.extend(["", "Missing reports: " + ", ".join(missing)])
    if stale:
        lines.extend(["", "Not checked in 48 hours: " + ", ".join(stale)])
    for title, rows in (
        ("Repeated failures", repeated),
        ("New failures", [f for f in observed if f["consecutive_checks"] == 1]),
        ("Recovered", recovered),
    ):
        lines.extend(["", f"### {title}", ""])
        if not rows:
            lines.append("None.")
        for row in rows[:100]:
            location = html.escape(f"{row['branch']} {row['location']}")
            message = html.escape(row["message"].splitlines()[0][:240])
            lines.append(
                f"- [{row['repository']}](https://github.com/flathub/{row['repository']}) "
                f"<code>{location}</code>: {message}"
            )
        if len(rows) > 100:
            lines.append("See triage.json in the artifact for the remaining entries.")
    (directory / "summary.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "record", "restore", "summarize"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("repo", nargs="?")
    parser.add_argument("status", nargs="?")
    parser.add_argument("exit_code", nargs="?", type=int)
    args = parser.parse_args()
    if args.command == "start":
        selected = []
        skipped = {}
        for line in sys.stdin.read().splitlines():
            if not line:
                continue
            repo, separator, reason = line.partition("\t")
            if separator:
                skipped[repo] = reason
            else:
                selected.append(repo)
        start(args.directory, selected, skipped)
    elif args.command == "record":
        record(args.directory, args.repo, args.status, args.exit_code)
    elif args.command == "restore":
        try:
            restore(args.directory)
        except Exception:
            write_json(args.directory / "history.json", {"status": "unavailable"})
            raise
    else:
        summarize(args.directory)


if __name__ == "__main__":
    main()
