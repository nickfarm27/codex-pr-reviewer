from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PEER = ROOT / "peer"
PLUGIN = PEER / "plugins" / "nickfarm27-pr-review"
SKILL = PLUGIN / "skills" / "nickfarm27-pr-review"


class PeerDistributionTests(unittest.TestCase):
    def run_script(
        self,
        path: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [str(path), *args],
            cwd=ROOT,
            env=merged_env,
            check=check,
            text=True,
            capture_output=True,
        )

    def test_peer_core_is_the_exact_reusable_slice_of_the_review_prompt(self) -> None:
        source_prompt = (ROOT / "prompts" / "review.md").read_bytes()
        start = source_prompt.index(b"## 2. Build the minimum useful context")
        end = source_prompt.index(b"## 6. Complete the cycle")
        source = source_prompt[start:end].rstrip(b"\n") + b"\n"
        packaged = (SKILL / "references" / "review-core.md").read_bytes()
        self.assertEqual(source, packaged)

    def test_version_is_consistent_across_skill_and_manifests(self) -> None:
        version = (PLUGIN / "VERSION").read_text().strip()
        codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        skill_text = (SKILL / "SKILL.md").read_text()
        skill_version = re.search(r'^  version: "([^"]+)"$', skill_text, re.MULTILINE)

        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(codex["version"], version)
        self.assertEqual(claude["version"], version)
        self.assertEqual(marketplace["plugins"][0]["version"], version)
        marketplace_source = ROOT / marketplace["plugins"][0]["source"]
        self.assertEqual(marketplace_source.resolve(), PLUGIN.resolve())
        self.assertIsNotNone(skill_version)
        self.assertEqual(skill_version.group(1), version)

    def test_setup_is_idempotent_and_uninstall_removes_only_owned_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex-skills"
            claude = root / "claude-skills"
            env = {
                "NICKFARM27_PR_REVIEW_CODEX_SKILLS_DIR": str(codex),
                "NICKFARM27_PR_REVIEW_CLAUDE_SKILLS_DIR": str(claude),
            }

            self.run_script(PEER / "setup", "--host", "codex", "--skip-doctor", env=env)
            self.run_script(PEER / "setup", "--host", "codex", "--skip-doctor", env=env)
            installed = codex / "nickfarm27-pr-review"
            self.assertTrue(installed.is_symlink())
            self.assertEqual(Path(os.readlink(installed)), SKILL)

            self.run_script(PEER / "uninstall", "--host", "codex", env=env)
            self.assertFalse(installed.exists())

    def test_setup_refuses_to_overwrite_an_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex = Path(temporary) / "codex-skills"
            existing = codex / "nickfarm27-pr-review"
            existing.mkdir(parents=True)
            marker = existing / "owned-by-user.txt"
            marker.write_text("keep")
            env = {"NICKFARM27_PR_REVIEW_CODEX_SKILLS_DIR": str(codex)}

            result = self.run_script(
                PEER / "setup",
                "--host",
                "codex",
                "--skip-doctor",
                env=env,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(), "keep")

    def test_custom_agent_skill_directory_can_be_installed_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skills_dir = Path(temporary) / "agent-skills"
            self.run_script(
                PEER / "setup",
                "--host",
                "custom",
                "--skills-dir",
                str(skills_dir),
                "--skip-doctor",
            )
            installed = skills_dir / "nickfarm27-pr-review"
            self.assertTrue(installed.is_symlink())
            self.assertEqual(Path(os.readlink(installed)), SKILL)

            self.run_script(
                PEER / "uninstall",
                "--host",
                "custom",
                "--skills-dir",
                str(skills_dir),
            )
            self.assertFalse(installed.exists())

    def test_doctor_fails_when_no_link_based_install_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_script(
                PEER / "bin" / "doctor",
                "--host",
                "auto",
                "--offline",
                env={
                    "NICKFARM27_PR_REVIEW_CODEX_SKILLS_DIR": str(root / "codex"),
                    "NICKFARM27_PR_REVIEW_CLAUDE_SKILLS_DIR": str(root / "claude"),
                },
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No installed Nicholas PR Review skill link", result.stderr)

    def test_update_check_fails_closed_when_release_differs(self) -> None:
        current = (PLUGIN / "VERSION").read_text().strip()
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = Path(temporary)
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = auth ]; then exit 0; fi\n"
                "if [ \"$1\" = release ]; then printf 'peer-v%s\\n' \"$FAKE_PEER_VERSION\"; exit 0; fi\n"
                "exit 1\n"
            )
            fake_gh.chmod(0o755)
            env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

            ok = self.run_script(
                PEER / "bin" / "check-update",
                env={**env, "FAKE_PEER_VERSION": current},
            )
            self.assertIn(f"{current} is current", ok.stdout)

            stale = self.run_script(
                PEER / "bin" / "check-update",
                env={**env, "FAKE_PEER_VERSION": "99.0.0"},
                check=False,
            )
            self.assertEqual(stale.returncode, 10)
            self.assertIn("not current", stale.stderr)

    def test_release_archive_contains_only_peer_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            self.run_script(PEER / "bin" / "package-release", str(output))
            archives = list(output.glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            checksum = Path(f"{archives[0]}.sha256")
            self.assertTrue(checksum.is_file())
            expected_digest = checksum.read_text().split()[0]
            self.assertEqual(
                hashlib.sha256(archives[0].read_bytes()).hexdigest(), expected_digest
            )

            with tarfile.open(archives[0]) as bundle:
                members = {member.name: member for member in bundle.getmembers()}
                names = set(members)

            self.assertTrue(
                all(
                    name == "nickfarm27-pr-review"
                    or name.startswith("nickfarm27-pr-review/")
                    for name in names
                )
            )
            self.assertIn("nickfarm27-pr-review/setup", names)
            self.assertTrue(members["nickfarm27-pr-review/setup"].mode & 0o111)
            self.assertIn("nickfarm27-pr-review/SETUP.md", names)
            self.assertTrue(
                any(name.endswith("/skills/nickfarm27-pr-review/SKILL.md") for name in names)
            )
            self.assertTrue(any(name.endswith("/scripts/check-update") for name in names))
            self.assertTrue(
                any(name.endswith("/references/review-core.md") for name in names)
            )
            self.assertFalse(any("review_queue.py" in name for name in names))
            self.assertFalse(any("reviews.db" in name for name in names))
            self.assertFalse(any("reports/" in name for name in names))


if __name__ == "__main__":
    unittest.main()
