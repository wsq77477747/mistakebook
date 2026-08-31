import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "pull_update.py"
SPEC = importlib.util.spec_from_file_location("pull_update", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pull_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pull_update)


class PullUpdateTests(unittest.TestCase):
    def test_ci_requires_success_from_expected_workflow(self):
        sha = "a" * 40
        response = {
            "workflow_runs": [
                {
                    "head_sha": sha,
                    "path": ".github/workflows/other.yml",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "head_sha": sha,
                    "path": pull_update.WORKFLOW_PATH,
                    "status": "completed",
                    "conclusion": "failure",
                },
            ]
        }
        with mock.patch.object(pull_update, "get_json", return_value=response):
            state, message = pull_update.get_ci_state(sha)
        self.assertEqual(state, "failed")
        self.assertIn("failure", message)

        response["workflow_runs"][1]["conclusion"] = "success"
        with mock.patch.object(pull_update, "get_json", return_value=response):
            state, _ = pull_update.get_ci_state(sha)
        self.assertEqual(state, "success")

    def test_record_deployed_sha_is_readable(self):
        sha = "b" * 40
        with tempfile.TemporaryDirectory() as temp_name:
            state_file = Path(temp_name) / "state" / "last_success_sha"
            with mock.patch.object(pull_update, "STATE_FILE", state_file):
                pull_update.record_deployed_sha(sha)
                self.assertEqual(pull_update.read_deployed_sha(), sha)

    def test_extract_archive_finds_project_root(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            source = temp_dir / "source"
            script = source / "mistakebook-test" / "deploy" / "update_native.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            archive = temp_dir / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(source / "mistakebook-test", arcname="mistakebook-test")

            project = pull_update.extract_archive(archive, temp_dir / "extract")
            self.assertEqual(project.name, "mistakebook-test")
            self.assertTrue((project / "deploy" / "update_native.sh").is_file())


if __name__ == "__main__":
    unittest.main()
