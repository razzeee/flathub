import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import health


class HealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "flathub/flathub",
                "GITHUB_RUN_ID": "100",
                "GITHUB_RUN_NUMBER": "7",
                "GITHUB_REF_NAME": "master",
                "APP_ID": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def source(self, status="failed", branch=None):
        source = {
            "manifest": "app.json",
            "source_pointer": "/modules/0/sources/0",
            "status": status,
            "checker_type": "json",
            "errors": [],
        }
        if status == "failed":
            source["errors"] = [
                {"type": "CheckerQueryError", "message": "No matching release"}
            ]
        if branch:
            source["branch"] = branch
        return source

    def run_reports(self, reports, previous=None, statuses=None, history="empty"):
        directory = self.root / str(len(list(self.root.iterdir())))
        directory.mkdir()
        health.start(directory, list(reports))
        health.write_json(directory / "history.json", {"status": history})
        if previous:
            health.write_json(directory / "previous-state.json", previous)
        for repo, report in reports.items():
            context = {
                "repository": repo,
                "status": (statuses or {}).get(repo, "finished"),
                "branch": "master",
                "commit": "abc",
                "checkout": f"/workspace/{repo}",
                "excluded_submodules": ["shared-modules"],
                "exit_code": 0,
            }
            with (directory / "coverage.jsonl").open("a") as output:
                output.write(json.dumps(context) + "\n")
            if report is not None:
                health.write_json(directory / f"{repo}.json", report)
        health.summarize(directory)
        return directory

    def report(self, source_status="failed", **kwargs):
        source = self.source(source_status)
        return {
            "schema_version": 1,
            "status": "failed" if source_status == "failed" else "completed",
            "delivery": "no_changes",
            "sources": [source],
            "errors": source["errors"],
            **kwargs,
        }

    def load(self, directory, name="health-state.json"):
        return json.loads((directory / name).read_text())

    def test_repeat_across_shards_and_recovery(self):
        first = self.run_reports({"app": self.report()})
        self.assertEqual(len(self.load(first)["failures"]), 1)
        other = self.run_reports({"other": self.report("checked")}, self.load(first))
        self.assertEqual(self.load(other)["failures"], self.load(first)["failures"])
        second = self.run_reports({"app": self.report()}, self.load(other))
        triage = self.load(second, "triage.json")
        self.assertEqual(triage["repeated_failures"][0]["consecutive_checks"], 2)
        third = self.run_reports({"app": self.report("checked")}, self.load(second))
        self.assertEqual(self.load(third)["failures"], {})
        self.assertEqual(len(self.load(third, "triage.json")["recovered"]), 1)

    def test_missing_skipped_or_unchecked_does_not_recover(self):
        first = self.run_reports({"app": self.report()})
        previous = self.load(first)
        for status, report in (
            ("finished", None),
            ("disabled", None),
            ("finished", self.report("unchecked")),
        ):
            with self.subTest(status=status, report=report):
                directory = self.run_reports({"app": report}, previous, {"app": status})
                self.assertEqual(self.load(directory)["failures"], previous["failures"])
                self.assertEqual(self.load(directory, "triage.json")["recovered"], [])

    def test_only_declared_submodule_missing_files_are_excluded(self):
        errors = [
            {"type": "ManifestFileOpenError", "message": f"Can't open '{path}'"}
            for path in (
                "/workspace/app/shared-modules/a.json",
                "/workspace/app/missing.json",
            )
        ]
        directory = self.run_reports(
            {"app": self.report("checked", errors=errors, status="failed")}
        )
        triage = self.load(directory, "triage.json")
        self.assertEqual(triage["excluded_submodule_errors"], 1)
        self.assertEqual(len(triage["new_failures"]), 1)
        self.assertIn("missing.json", triage["new_failures"][0]["message"])

    def test_branch_results_do_not_recover_other_branches(self):
        failed = self.report(branch="branch/24.08")
        first = self.run_reports({"app": self.report(branches=[failed])})
        passed = self.report("checked", branch="branch/25.08")
        second = self.run_reports(
            {"app": self.report("checked", branches=[passed])}, self.load(first)
        )
        self.assertEqual(self.load(second)["failures"], self.load(first)["failures"])

    def test_unknown_checker_but_not_missing_metadata_is_actionable(self):
        for reason, count in (
            ("no_applicable_checker", 1),
            ("no_update_checker", 0),
            ("no_candidate", 0),
        ):
            report = self.report("unchecked")
            report["sources"][0]["reason"] = reason
            directory = self.run_reports({"app": report})
            self.assertEqual(len(self.load(directory)["failures"]), count)

    def test_history_failure_does_not_replace_saved_state(self):
        directory = self.run_reports({"app": self.report()}, history="unavailable")
        self.assertFalse((directory / "health-state.json").exists())
        self.assertTrue(self.load(directory, "triage.json")["new_failures"])

    def test_stale_coverage_survives_shard_rotation(self):
        first = self.run_reports({"app": self.report("checked")})
        state = self.load(first)
        state["repositories"]["app"]["last_checked"] = "2020-01-01T00:00:00+00:00"
        second = self.run_reports(
            {"app": None, "other": self.report("checked")},
            state,
            statuses={"app": "outside_shard"},
        )
        self.assertEqual(
            self.load(second, "triage.json")["not_checked_in_48_hours"], ["app"]
        )

    def test_partial_run_records_unstarted_repositories(self):
        directory = self.run_reports({"app": None}, statuses={"app": "selected"})
        self.assertEqual(
            self.load(directory, "coverage.json")["missing_reports"], ["app"]
        )

    def test_restore_ignores_current_run_and_other_branches(self):
        artifacts = [
            {
                "id": number,
                "name": f"checker-health-{number}-1",
                "expired": False,
                "workflow_run": {"id": number, "head_branch": branch},
            }
            for number, branch in ((100, "master"), (99, "other"), (98, "master"))
        ]
        state = {"schema_version": 1, "failures": {}, "repositories": {}}
        artifacts.append(
            {
                "id": 300,
                "name": "checker-health-50-2",
                "expired": False,
                "workflow_run": {"id": 50, "head_branch": "master"},
            }
        )
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("health-state.json", json.dumps(state))
        with patch.object(
            health.subprocess,
            "check_output",
            side_effect=[
                json.dumps([{"artifacts": artifacts}]).encode(),
                data.getvalue(),
            ],
        ) as run:
            health.restore(self.root)
        self.assertIn("/98/zip", run.call_args.args[0][-1])
        self.assertEqual(self.load(self.root, "previous-state.json"), state)

    def test_record_captures_revision_before_checker_changes_head(self):
        repo = self.root / "app"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
        (repo / ".gitmodules").write_text(
            '[submodule "shared"]\npath = shared-modules\n'
        )
        subprocess.run(["git", "-C", str(repo), "add", ".gitmodules"], check=True)
        command = [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
        ]
        subprocess.run([*command, "Initial"], check=True)
        health.record(self.root, str(repo), "checking")
        revision = health.git(repo, "rev-parse", "HEAD")
        subprocess.run([*command, "Update"], check=True)
        health.record(self.root, str(repo), "finished", 1)
        events = [
            json.loads(line)
            for line in (self.root / "coverage.jsonl").read_text().splitlines()
        ]
        self.assertEqual(events[0]["commit"], revision)
        self.assertEqual(events[0]["excluded_submodules"], ["shared-modules"])
        self.assertEqual(events[1]["exit_code"], 1)

    def test_failed_report_without_diagnostics_is_not_healthy(self):
        directory = self.run_reports({"app": self.report("checked", status="failed")})
        self.assertEqual(
            self.load(directory, "triage.json")["new_failures"][0]["category"],
            "RunFailed",
        )

    def test_runtime_temporary_paths_do_not_change_failure_identity(self):
        def report(path):
            return self.report(
                "checked",
                status="failed",
                errors=[
                    {
                        "type": "ManifestFileOpenError",
                        "message": f"Can't open '{path}'",
                    }
                ],
            )

        first = self.run_reports(
            {"app": report("/tmp/fedc-first/checkout/missing.json")}
        )
        second = self.run_reports(
            {"app": report("/tmp/fedc-second/checkout/missing.json")}, self.load(first)
        )
        self.assertEqual(
            self.load(second, "triage.json")["repeated_failures"][0][
                "consecutive_checks"
            ],
            2,
        )

    def test_restore_finds_history_after_first_artifact_page(self):
        unrelated = [{"id": i, "name": "other-artifact"} for i in range(100)]
        artifact = {
            "id": 101,
            "name": "checker-health-98-1",
            "expired": False,
            "workflow_run": {"id": 98, "head_branch": "master"},
        }
        state = {"schema_version": 1, "failures": {"old": {"consecutive_checks": 2}}}
        archive_data = io.BytesIO()
        with zipfile.ZipFile(archive_data, "w") as archive:
            archive.writestr("health-state.json", json.dumps(state))

        def request(command):
            if command[-1].endswith("/101/zip"):
                return archive_data.getvalue()
            pages = [
                {"total_count": 101, "artifacts": unrelated},
                {"total_count": 101, "artifacts": [artifact]},
            ]
            if "--paginate" in command and "--slurp" in command:
                return json.dumps(pages).encode()
            return json.dumps(pages[0]).encode()

        with patch.object(health.subprocess, "check_output", side_effect=request):
            health.restore(self.root)
        self.assertEqual(self.load(self.root, "previous-state.json"), state)

    def test_selection_exclusions_are_recorded(self):
        names = [f"app{i}" for i in range(20)]
        checksums = {
            name: int(
                subprocess.check_output(["cksum"], input=name.encode()).split()[0]
            )
            for name in names
        }
        selected = next(name for name in names if checksums[name] % 6 == 0)
        outside = next(name for name in names if checksums[name] % 6 != 0)
        listing = self.root / "repos.json"
        listing.write_text(
            json.dumps(
                [
                    {"name": name, "archived": name == "archived", "private": False}
                    for name in (selected, outside, "archived", "inactive")
                ]
            )
        )
        environment = self.root / "environment.sh"
        environment.write_text("""
git() { return 0; }
sleep() { return 0; }
curl() {
    while [[ "$1" != --output ]]; do shift; done
    printf 'inactive\\n' > "$2"
}
gh() {
    if [[ "$*" == *search/code* ]]; then
        printf '{"incomplete_results":false,"total_count":0,"items":[]}\\n'
    elif [[ "$*" == *orgs/flathub/repos* ]]; then
        command jq -r "${@: -1}" "$REPO_LISTING"
    else
        printf 'selected\\n'
    fi
}
parallel() { while read -r repo; do mkdir -p "$repo"; done; }
python3() { command "$PYTHON" "$HEALTH_SCRIPT" "${@:2}"; }
""")
        for target in ("", "inactive"):
            with self.subTest(target=target):
                workspace = self.root / (target or "global")
                workspace.mkdir()
                script = Path(health.__file__).with_name("entrypoint.sh")
                env = {
                    **os.environ,
                    "GITHUB_WORKSPACE": str(workspace),
                    "APP_ID": target,
                    "BASH_ENV": str(environment),
                    "REPO_LISTING": str(listing),
                    "PYTHON": os.sys.executable,
                    "HEALTH_SCRIPT": health.__file__,
                }
                subprocess.run(
                    ["bash", str(script)],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                coverage = self.load(workspace / "checker-health", "coverage.json")
                self.assertEqual(coverage["skipped"]["inactive"], "inactive")
                if not target:
                    self.assertEqual(coverage["selected"], [selected])
                    self.assertEqual(coverage["skipped"]["archived"], "archived")
                    self.assertEqual(coverage["skipped"][outside], "outside_shard")


if __name__ == "__main__":
    unittest.main()
