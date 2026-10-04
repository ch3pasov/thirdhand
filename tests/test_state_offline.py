"""Test the exact state functions without importing runtime config or Telegram.

main.py currently loads local configuration and third-party packages on import.
Compile only the named function definitions from its AST so this stdlib-only
suite cannot execute that initialization. Integration tests remain separate.
"""

import ast
import asyncio
import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "main.py"
FUNCTIONS = {"save_state", "get_last_processed_id", "mark_processed"}


def load_state_functions(state_path):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in FUNCTIONS
    ]
    if {node.name for node in selected} != FUNCTIONS:
        raise AssertionError("Expected state functions are missing from main.py")
    module = ModuleType("thirdhand_state_offline")
    module.__dict__.update(
        json=json,
        STATE_PATH=state_path,
        last_processed_ids={},
        state_lock=asyncio.Lock(),
    )
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), module.__dict__)
    return module


class StatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.state_path = Path(temporary) / "state.json"
        self.state = load_state_functions(self.state_path)
        self.original = {"-1001": 12, "-1002": 99}
        self.state.last_processed_ids.update(self.original)
        self.state.save_state(self.original)

    async def test_write_failure_does_not_advance_memory_or_disk(self):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await self.state.mark_processed(-1001, 13)

        self.assertEqual(self.state.last_processed_ids, self.original)
        self.assertEqual(json.loads(self.state_path.read_text()), self.original)
        self.assertEqual(await self.state.get_last_processed_id(-1001), 12)

    async def test_replace_failure_keeps_cursor_retryable(self):
        with patch.object(Path, "replace", side_effect=OSError("read-only directory")):
            with self.assertRaisesRegex(OSError, "read-only directory"):
                await self.state.mark_processed(-1001, 13)

        self.assertEqual(self.state.last_processed_ids, self.original)
        self.assertEqual(json.loads(self.state_path.read_text()), self.original)
        await self.state.mark_processed(-1001, 13)

        expected = {"-1001": 13, "-1002": 99}
        self.assertEqual(self.state.last_processed_ids, expected)
        self.assertEqual(json.loads(self.state_path.read_text()), expected)

    async def test_success_advances_memory_only_after_save(self):
        original_reference = self.state.last_processed_ids
        save = self.state.save_state

        def inspect_before_save(candidate):
            self.assertEqual(self.state.last_processed_ids, self.original)
            save(candidate)

        with patch.object(self.state, "save_state", side_effect=inspect_before_save):
            await self.state.mark_processed(-1001, 13)

        self.assertIs(self.state.last_processed_ids, original_reference)
        expected = {"-1001": 13, "-1002": 99}
        self.assertEqual(self.state.last_processed_ids, expected)
        self.assertEqual(json.loads(self.state_path.read_text()), expected)

    async def test_older_and_duplicate_ids_do_not_write_or_move_backwards(self):
        with patch.object(self.state, "save_state") as save:
            await self.state.mark_processed(-1001, 11)
            await self.state.mark_processed(-1001, 12)

        save.assert_not_called()
        self.assertEqual(self.state.last_processed_ids, self.original)

    async def test_independent_channel_updates_keep_both_cursors(self):
        await asyncio.gather(
            self.state.mark_processed(-1001, 13),
            self.state.mark_processed(-1003, 5),
        )

        expected = {"-1001": 13, "-1002": 99, "-1003": 5}
        self.assertEqual(self.state.last_processed_ids, expected)
        self.assertEqual(json.loads(self.state_path.read_text()), expected)

    async def test_update_waits_for_lock_before_persisting_or_changing_memory(self):
        await self.state.state_lock.acquire()
        update = asyncio.create_task(self.state.mark_processed(-1001, 13))
        try:
            await asyncio.sleep(0)
            self.assertFalse(update.done())
            self.assertEqual(self.state.last_processed_ids, self.original)
            self.assertEqual(json.loads(self.state_path.read_text()), self.original)
        finally:
            self.state.state_lock.release()
            await update
        self.assertEqual(self.state.last_processed_ids['-1001'], 13)
        self.assertEqual(json.loads(self.state_path.read_text())['-1001'], 13)


if __name__ == "__main__":
    unittest.main()
