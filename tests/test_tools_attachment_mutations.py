"""Portable link/policy tests and real Linux filesystem/MCP integration tests."""

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import obsidian_mcp.config as config
from obsidian_mcp.domain.models import PreconditionRequiredError, RevisionConflictError
from obsidian_mcp.storage.filesystem import VaultStorage
from obsidian_mcp.storage.policy import VaultAccessPolicy
from obsidian_mcp.tools import attachment_mutations as kb


class LinkTests(unittest.TestCase):
    def rewrite(self, text, files=None):
        return kb.rewrite_links(
            text,
            "Notes/note.md",
            "Assets/Attachments/photo.png",
            "Notes/Images/new photo.png",
            files or {"Assets/Attachments/photo.png"},
        )

    def test_full_wikilink_alias_size_fragment(self):
        text, count = self.rewrite("![[Assets/Attachments/photo.png#part|300]] [[photo.png|cover]]")
        self.assertEqual(count, 2)
        self.assertEqual(
            text, "![[Notes/Images/new photo.png#part|300]] [[Notes/Images/new photo.png|cover]]"
        )

    def test_relative_markdown_and_title(self):
        text, count = self.rewrite('![cover](../Assets/Attachments/photo.png "Title")')
        self.assertEqual(count, 1)
        self.assertEqual(text, '![cover](Images/new%20photo.png "Title")')

    def test_markdown_root_fragment_and_angle(self):
        text, count = self.rewrite("[x](</Assets/Attachments/photo.png#page=2>)")
        self.assertEqual((text, count), ("[x](<Images/new%20photo.png#page=2>)", 1))

    def test_markdown_reference_definition(self):
        text, count = self.rewrite('![x][cover]\n[cover]: ../Assets/Attachments/photo.png "title"')
        self.assertEqual(count, 1)
        self.assertIn('[cover]: Images/new%20photo.png "title"', text)

    def test_external_url_untouched(self):
        self.assertEqual(
            self.rewrite("![x](https://example.com/photo.png)"),
            ("![x](https://example.com/photo.png)", 0),
        )

    def test_ambiguous_short_link_rejected(self):
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.rewrite("![[photo.png]]", {"Assets/Attachments/photo.png", "Notes/photo.png"})

    def test_full_path_disambiguates(self):
        self.assertEqual(
            self.rewrite(
                "![[Assets/Attachments/photo.png]]",
                {"Assets/Attachments/photo.png", "Notes/photo.png"},
            )[1],
            1,
        )

    def test_other_attachment_link_untouched(self):
        text = "![[ReadOnly/photo.png]]"
        self.assertEqual(
            self.rewrite(text, {"Assets/Attachments/photo.png", "ReadOnly/photo.png"}), (text, 0)
        )

    def test_unsupported_html_blocks(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.rewrite('<img src="../Assets/Attachments/photo.png">')

    def test_yaml_reference_blocks(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.rewrite("---\ncover: Assets/Attachments/photo.png\n---")

    def test_encoded_source_filename(self):
        text, count = kb.rewrite_links(
            "![x](../Assets/a%20b.png)",
            "Notes/n.md",
            "Assets/a b.png",
            "Assets/new.png",
            {"Assets/a b.png"},
        )
        self.assertEqual((text, count), ("![x](../Assets/new.png)", 1))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = VaultAccessPolicy(
            self.temp.name,
            write_paths=["Assets/", "Notes/"],
            deny_write_paths=["ReadOnly/", "policy.md"],
        )

    def test_protected_write_and_delete_denied(self):
        for path in ["ReadOnly/x.png", "ReadOnly/n.md", "policy.md"]:
            with self.subTest(path=path), self.assertRaises(PermissionError):
                self.policy.resolve_delete(path)

    def test_write_scopes(self):
        for path in ["Assets/x.png", "Notes/x.png"]:
            self.assertEqual(self.policy.resolve_write(path).relative, path)
        for path in ["Assets-other/x.png", "x.png"]:
            with self.assertRaises(PermissionError):
                self.policy.resolve_write(path)

    def test_traversal_rejected(self):
        for path in ["../x.png", "/etc/passwd", "C:/x.png"]:
            with self.assertRaises(ValueError):
                self.policy.resolve_write(path)


@unittest.skipUnless(
    os.name == "posix", "Real secure filesystem requires Linux; Docker build runs these"
)
class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.vault = root / "vault"
        for name in ["Assets/Attachments", "Notes", "ReadOnly"]:
            (self.vault / name).mkdir(parents=True, exist_ok=True)
        env = {
            "VAULT_PATH": str(self.vault),
            "LOCK_PATH": str(root / "locks"),
            "AUDIT_LOG_PATH": str(root / "audit.jsonl"),
            "READ_ONLY": "false",
            "WRITE_PATHS": "Assets/,Notes/",
            "DENY_WRITE_PATHS": "ReadOnly/,policy.md,.trash/",
            "DENY_READ_PATHS": ".trash/",
            "EXCLUDE_PATHS": ".trash/",
            "ALLOW_PERMANENT_DELETE": "false",
            "REQUIRE_WRITE_PRECONDITIONS": "true",
            "ENABLE_DELETE": "true",
            "ENABLE_MOVE": "true",
            "TRANSPORT": "stdio",
            "HOME": str(root),
            "XDG_DATA_HOME": str(root / "share"),
            "FASTMCP_HOME": str(root / "fastmcp"),
        }
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        config._config = None
        self.addCleanup(lambda: setattr(config, "_config", None))
        self.source = "Assets/Attachments/photo.png"
        self.dest = "Notes/new.png"
        self.data = b"\x89PNG\r\n\x1a\n\x00\xffunchanged payload"
        (self.vault / self.source).write_bytes(self.data)
        self.revision = kb.inspect_attachment(self.source)["revision"]

    def write(self, path, content):
        p = self.vault / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def move(self, **kwargs):
        return kb.move_attachment(
            self.source, self.dest, expected_revision=self.revision, dry_run=False, **kwargs
        )

    def test_preview_is_read_only_and_commit_updates_links(self):
        self.write("Notes/n.md", "![[photo.png|300]]\n![x](../Assets/Attachments/photo.png)")
        before = (self.vault / "Notes/n.md").read_bytes()
        preview = kb.move_attachment(self.source, self.dest)
        self.assertEqual(preview["links"], 2)
        self.assertTrue((self.vault / self.source).exists())
        self.assertEqual((self.vault / "Notes/n.md").read_bytes(), before)
        self.move()
        self.assertFalse((self.vault / self.source).exists())
        self.assertEqual((self.vault / self.dest).read_bytes(), self.data)
        self.assertEqual(
            (self.vault / "Notes/n.md").read_text(), "![[Notes/new.png|300]]\n![x](new.png)"
        )

    def test_protected_reference_blocks_every_change(self):
        self.write("Assets/n.md", "![[photo.png]]")
        self.write("ReadOnly/n.md", "![[photo.png]]")
        with self.assertRaises(PermissionError):
            self.move()
        self.assertTrue((self.vault / self.source).exists())
        self.assertFalse((self.vault / self.dest).exists())
        self.assertEqual((self.vault / "Assets/n.md").read_text(), "![[photo.png]]")

    def test_protected_destination_blocked(self):
        with self.assertRaises(PermissionError):
            kb.move_attachment(self.source, "ReadOnly/photo.png")

    def test_protected_source_blocked(self):
        self.write("ReadOnly/photo.png", "protected")
        with self.assertRaises(PermissionError):
            kb.move_attachment("ReadOnly/photo.png", self.dest)

    def test_collision_no_changes(self):
        self.write(self.dest, "existing")
        with self.assertRaises(FileExistsError):
            self.move()
        self.assertEqual((self.vault / self.dest).read_text(), "existing")

    def test_stale_revision_no_changes(self):
        (self.vault / self.source).write_bytes(b"new version")
        with self.assertRaises(RevisionConflictError):
            self.move()
        self.assertEqual((self.vault / self.source).read_bytes(), b"new version")

    def test_revision_required(self):
        with self.assertRaises(PreconditionRequiredError):
            kb.move_attachment(self.source, self.dest, dry_run=False)

    def test_trash_and_restore(self):
        result = kb.delete_attachment(self.source, expected_revision=self.revision, dry_run=False)
        self.assertFalse((self.vault / self.source).exists())
        kb.restore_attachment(result["trashed_name"], self.source)
        self.assertEqual((self.vault / self.source).read_bytes(), self.data)

    def test_delete_referenced_requires_explicit_override(self):
        self.write("Notes/n.md", "![[photo.png]]")
        with self.assertRaisesRegex(ValueError, "still referenced"):
            kb.delete_attachment(self.source, expected_revision=self.revision, dry_run=False)
        self.assertTrue((self.vault / self.source).exists())

    def test_delete_protected_reference_blocks_even_override(self):
        self.write("ReadOnly/n.md", "![[photo.png]]")
        with self.assertRaises(PermissionError):
            kb.delete_attachment(
                self.source, expected_revision=self.revision, dry_run=False, allow_broken_links=True
            )

    def test_permanent_delete_flag(self):
        with self.assertRaises(PermissionError):
            kb.delete_attachment(
                self.source, expected_revision=self.revision, trash=False, dry_run=False
            )
        os.environ["ALLOW_PERMANENT_DELETE"] = "true"
        config._config = None
        kb.delete_attachment(
            self.source, expected_revision=self.revision, trash=False, dry_run=False
        )
        self.assertFalse((self.vault / self.source).exists())
        self.assertFalse((self.vault / ".trash").exists())

    def test_symlink_escape_blocked(self):
        (self.vault / "Assets/link").symlink_to(self.vault / "ReadOnly", target_is_directory=True)
        with self.assertRaises((ValueError, PermissionError)):
            kb.move_attachment(self.source, "Assets/link/photo.png")

    def test_canvas_reference_blocks(self):
        self.write("Assets/map.canvas", '{"file":"Assets/Attachments/photo.png"}')
        with self.assertRaisesRegex(ValueError, "unsupported document format"):
            self.move()

    def test_rollback_after_second_note_write_fails(self):
        self.write("Assets/a.md", "![[photo.png]]")
        self.write("Notes/b.md", "![[photo.png]]")
        original = VaultStorage.write_text_atomic

        def failing(storage, path, content, **kwargs):
            if path == "Notes/b.md":
                raise OSError("simulated disk error")
            return original(storage, path, content, **kwargs)

        with (
            patch.object(VaultStorage, "write_text_atomic", failing),
            self.assertRaisesRegex(RuntimeError, "rolled back"),
        ):
            self.move()
        self.assertEqual((self.vault / self.source).read_bytes(), self.data)
        self.assertFalse((self.vault / self.dest).exists())
        self.assertEqual((self.vault / "Assets/a.md").read_text(), "![[photo.png]]")

    def test_cross_device_failure_leaves_notes_untouched(self):
        self.write("Assets/n.md", "![[photo.png]]")
        with (
            patch.object(VaultStorage, "move", side_effect=OSError(18, "Cross-device link")),
            self.assertRaises(OSError),
        ):
            self.move()
        self.assertEqual((self.vault / "Assets/n.md").read_text(), "![[photo.png]]")

    def test_mcp_discovers_new_tools_and_inspects_attachment(self):
        from fastmcp import Client

        from obsidian_mcp import server

        importlib.reload(server)

        def restore_server():
            self.env_patch.stop()
            config._config = None
            importlib.reload(server)

        self.addCleanup(restore_server)

        async def exercise():
            async with Client(server.mcp) as client:
                names = {tool.name for tool in await client.list_tools()}
                self.assertTrue(
                    {
                        "move_attachment_tool",
                        "delete_attachment_tool",
                        "restore_attachment_tool",
                        "inspect_attachment_tool",
                    }
                    <= names
                )
                result = await client.call_tool("inspect_attachment_tool", {"path": self.source})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["revision"], self.revision)
                # Preview passes through the actual MCP schema and envelope.
                result = await client.call_tool(
                    "move_attachment_tool", {"from_path": self.source, "to_path": self.dest}
                )
                self.assertTrue(result.structured_content["meta"]["dry_run"])
                result = await client.call_tool(
                    "move_attachment_tool",
                    {
                        "from_path": self.source,
                        "to_path": self.dest,
                        "expected_revision": self.revision,
                        "dry_run": False,
                    },
                )
                self.assertEqual(result.structured_content["meta"]["action"], "moved")
                self.assertEqual((self.vault / self.dest).read_bytes(), self.data)
                result = await client.call_tool(
                    "delete_attachment_tool",
                    {"path": self.dest, "expected_revision": self.revision, "dry_run": False},
                )
                trashed = result.structured_content["data"]["trashed_name"]
                result = await client.call_tool(
                    "restore_attachment_tool", {"trashed_name": trashed, "to_path": self.source}
                )
                self.assertEqual(result.structured_content["meta"]["action"], "restored")
                self.assertEqual((self.vault / self.source).read_bytes(), self.data)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
