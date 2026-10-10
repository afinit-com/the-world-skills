from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginPackageTests(unittest.TestCase):
    def test_manifests_share_identity_and_only_expose_skills(self) -> None:
        portable = json.loads((ROOT / "plugin.json").read_text())
        self.assertEqual(portable["license"], "MIT")
        self.assertEqual(
            (ROOT / "skills/the-world-start/assets/kit/world/LICENSE").read_text(),
            (ROOT / "LICENSE").read_text(),
        )
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                manifest = json.loads((ROOT / f".{host}-plugin/plugin.json").read_text())
                for field in ("name", "version", "description", "author", "repository", "license"):
                    self.assertEqual(manifest[field], portable[field])
                self.assertEqual(manifest["skills"], "./skills/")
                self.assertNotIn("hooks", manifest)
                self.assertNotIn("mcpServers", manifest)
        self.assertFalse((ROOT / "hooks/hooks.json").exists())
        self.assertFalse((ROOT / "mcp.json").exists())
        self.assertEqual(
            sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")),
            ["help-my-world", "the-world-audit", "the-world-start", "the-world-util"],
        )
        for name in ("the-world-start", "help-my-world"):
            skill = ROOT / "skills" / name
            self.assertIn("disable-model-invocation: true", (skill / "SKILL.md").read_text())
            self.assertIn("allow_implicit_invocation: false", (skill / "agents/openai.yaml").read_text())

    def test_start_skill_and_readme_agree_on_where_the_harness_lives(self) -> None:
        # Without a rule the model improvised: one test install landed in the current
        # directory, another in a made-up sibling. One rule, said the same way everywhere.
        skill = " ".join((ROOT / "skills/the-world-start/SKILL.md").read_text().split())
        self.assertIn("goes beside it as `<project>-world` (`~/code/my-app` → `~/code/my-app-world`)", skill)
        for readme in ("README.md", "README.ko.md"):
            self.assertIn("`~/code/my-app` → `~/code/my-app-world`", (ROOT / readme).read_text(), readme)
        self.assertIn("nothing about the Slack plan", skill)  # LAB-323: one app, no app limit

    def test_start_skill_confirms_each_voice_and_keeps_facts_exact(self) -> None:
        text = " ".join((ROOT / "skills/the-world-start/SKILL.md").read_text().split())
        self.assertIn("Write it to `Persona(voice=...)` only after the user edits or approves it", text)
        self.assertIn('Numbers, "unverified" markers, errors, and approval requests stay exact', text)

    def test_sync_restores_shared_fields_without_touching_app_specific_ones(self) -> None:
        # The three manifests cannot be one file or a symlink — each app reads its own and
        # carries its own extra keys — so --sync is what keeps their shared half honest.
        paths = [ROOT / "plugin.json"] + [ROOT / f".{host}-plugin/plugin.json"
                                          for host in ("claude", "codex")]
        catalogs = [ROOT / relative for relative in (".claude-plugin/marketplace.json",
                                                     ".agents/plugins/marketplace.json")]
        originals = [path.read_text() for path in paths + catalogs]
        script = [sys.executable, str(ROOT / "scripts/build-plugin.py")]
        try:
            bumped = json.loads(originals[0])
            bumped["version"] = "9.9.9"
            paths[0].write_text(json.dumps(bumped, ensure_ascii=False, indent=2) + "\n")
            drifted = subprocess.run(script + [str(ROOT / "nonexistent-build")],
                                     capture_output=True, text=True)
            self.assertNotEqual(drifted.returncode, 0)
            self.assertIn("--sync", drifted.stderr)
            check = subprocess.run(script + ["--check"], capture_output=True, text=True)
            self.assertNotEqual(check.returncode, 0)
            self.assertIn(".claude-plugin/plugin.json: version differs", check.stderr)
            self.assertIn("plugin.json: 9.9.9", check.stdout)
            synced = subprocess.run(script + ["--sync"], check=True,
                                    capture_output=True, text=True)
            # two manifests take the new version, and so does the Claude catalog;
            # the Codex catalog carries no version, so a version bump leaves it alone
            self.assertEqual(len(synced.stdout.strip().splitlines()), 3)
            for path in catalogs[:1]:
                self.assertEqual(json.loads(path.read_text())["metadata"]["version"], "9.9.9")
            for path in paths[1:]:
                manifest = json.loads(path.read_text())
                self.assertEqual(manifest["version"], "9.9.9")
                self.assertEqual(manifest["skills"], "./skills/")
            self.assertIn("interface", json.loads(paths[2].read_text()))
            again = subprocess.run(script + ["--sync"], check=True,
                                   capture_output=True, text=True)
            self.assertIn("Already in sync", again.stdout)
            agreed = subprocess.run(script + ["--check"], capture_output=True, text=True)
            self.assertEqual(agreed.returncode, 0, agreed.stderr)
            self.assertNotEqual(subprocess.run(script + ["--sync", "--force"],
                                               capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(script, capture_output=True).returncode, 0)
        finally:
            for path, original in zip(paths + catalogs, originals):
                path.write_text(original)

    def test_repository_root_is_the_published_marketplace(self) -> None:
        # `claude plugin marketplace add afinit-com/the-world-skills` reads these files from
        # the repository root, so each must list this plugin at "./", and the root must
        # carry the manifest each app looks for there.
        manifest = json.loads((ROOT / "plugin.json").read_text())
        for relative in (".claude-plugin/marketplace.json",):
            with self.subTest(catalog=relative):
                catalog = json.loads((ROOT / relative).read_text())
                self.assertEqual(catalog["name"], "the-world")
                self.assertEqual(catalog["metadata"]["version"], manifest["version"])
                self.assertEqual([(p["name"], p["source"]) for p in catalog["plugins"]],
                                 [(manifest["name"], "./")])
        codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(codex["name"], "the-world")
        self.assertEqual(codex["plugins"][0]["source"], {"source": "local", "path": "./"})
        for host in ("claude", "codex"):
            self.assertTrue((ROOT / f".{host}-plugin/plugin.json").is_file())

    def test_package_leaves_out_everything_a_harness_ignores(self) -> None:
        # A harness tried out inside the kit leaves state, logs, tasks and tokens behind;
        # whatever the harness's own .gitignore keeps out of git, the package keeps out too.
        import fnmatch
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_plugin", ROOT / "scripts/build-plugin.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        template = ROOT / "skills/the-world-start/assets/kit/templates/gitignore.tmpl"
        for line in template.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")) or line == "env/*":  # env/ ships the example
                continue
            name = line.rstrip("/").rsplit("/", 1)[-1]
            self.assertTrue(any(fnmatch.fnmatch(name, pat) for pat in build.PACKAGE_IGNORE), line)

    def test_detached_package_installs_and_catalogs_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve()
            output = temporary / "marketplace"
            command = [sys.executable, str(ROOT / "scripts/build-plugin.py"), str(output)]
            subprocess.run(command, check=True, capture_output=True, cwd=temporary)
            plugin = output / "plugins/the-world-skills"
            for host in ("claude",):
                catalog = json.loads((output / f".{host}-plugin/marketplace.json").read_text())
                self.assertEqual((output / catalog["plugins"][0]["source"]).resolve(), plugin)
            codex = json.loads((output / ".agents/plugins/marketplace.json").read_text())
            self.assertEqual((output / codex["plugins"][0]["source"]["path"]).resolve(), plugin)
            for path in plugin.rglob("*"):
                self.assertFalse(path.is_symlink())
                self.assertNotIn(path.name, ("__pycache__", ".venv", ".pytest_cache", "harness.env"))
            target = temporary / "harness"
            subprocess.run([
                sys.executable, str(plugin / "skills/the-world-start/scripts/install-kit.py"),
                str(target), "--provider", "codex",
            ], check=True, capture_output=True, cwd=temporary)
            prereqs = subprocess.run([
                sys.executable, str(plugin / "skills/the-world-start/scripts/check-prereqs.py"),
                "--json",
            ], capture_output=True, text=True, cwd=temporary)
            self.assertIn(prereqs.returncode, (0, 1))
            self.assertIn("python", {item["name"] for item in json.loads(prereqs.stdout)["items"]})
            for relative in ("world/world_map.html", "bin/world-map-demo.py", "docs/world-map.md"):
                self.assertTrue((target / relative).is_file())
            self.assertEqual((target / "world/LICENSE").read_text(), (ROOT / "LICENSE").read_text())
            self.assertTrue((plugin / "docs/plugins.md").is_file())
            self.assertTrue((plugin / "docs/plugins.ko.md").is_file())
            self.assertEqual((plugin / "LICENSE").read_text(), (ROOT / "LICENSE").read_text())
            self.assertFalse((plugin / "skills/the-world-delegate").exists())
            self.assertTrue((plugin / "skills/the-world-audit/scan.py").is_file())
            self.assertTrue((plugin / "skills/the-world-start/scripts/check-prereqs.py").is_file())
            self.assertFalse((plugin / "docs/img").exists())
            self.assertFalse((plugin / "docs/index.html").exists())
            self.assertFalse((plugin / "skills/the-world-init").exists())
            # the packaged copy of build-plugin.py can rebuild from the package itself
            rebuilt = temporary / "rebuilt"
            subprocess.run([sys.executable, str(plugin / "scripts/build-plugin.py"), str(rebuilt)],
                           check=True, capture_output=True, cwd=temporary)
            self.assertTrue((rebuilt / "plugins/the-world-skills/skills/the-world-start/SKILL.md").is_file())
            sentinel = output / "keep.txt"
            sentinel.write_text("mine")
            retry = subprocess.run(command, capture_output=True, cwd=temporary)
            self.assertNotEqual(retry.returncode, 0)
            self.assertEqual(sentinel.read_text(), "mine")


if __name__ == "__main__":
    unittest.main()


class RepositoryReferenceTests(unittest.TestCase):
    """Every place that tells a person where this repo lives must agree with plugin.json.

    The landing page and install-kit.py's recovery hint both pointed at an org the repo
    had moved away from, and the hint named a skill (`-s the-world`) that does not exist.
    Nothing read them, so nothing noticed; the hint only prints when an install is already
    broken, which is the one moment a wrong command costs the most.
    """

    SLUG = re.compile(r"(?:github\.com/|npx skills add )([\w.-]+)/the-world-skills")
    SKILL_FLAG = re.compile(r"npx skills add \S+ -s ([\w-]+)")

    def _tracked_text(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.split()
        for name in files:
            path = ROOT / name
            if path.suffix not in {".md", ".html", ".py", ".json", ".toml", ".yaml", ".sh"}:
                continue
            yield name, path.read_text(errors="replace")

    def test_every_repo_reference_names_the_canonical_owner(self) -> None:
        owner = json.loads((ROOT / "plugin.json").read_text())["repository"].split("/")[-2]
        for name, text in self._tracked_text():
            for found in self.SLUG.findall(text):
                with self.subTest(file=name):
                    self.assertEqual(found, owner)

    def test_every_install_hint_names_a_skill_that_exists(self) -> None:
        skills = {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}
        for name, text in self._tracked_text():
            for skill in self.SKILL_FLAG.findall(text):
                with self.subTest(file=name, skill=skill):
                    self.assertIn(skill, skills)
