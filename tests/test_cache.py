"""SQLite recovery cache: files and units."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_epub_translator.cache import Cache  # noqa: E402


class TestCacheLifecycle(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bd = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_put_file_then_get(self):
        with Cache(self.bd) as c:
            oh = Cache.hash_text("hello")
            c.put_file("a.xhtml", oh, "Ciao", "translated", 10, 5)
            row = c.get("a.xhtml")
            self.assertEqual(row["text"], "Ciao")
            self.assertEqual(row["status"], "translated")
            self.assertEqual(row["completion_tokens"], 5)
            self.assertEqual(c.files()[0]["rel"], "a.xhtml")

    def test_pending_follows_the_status(self):
        with Cache(self.bd) as c:
            c.put_file("a.xhtml", "h", "Ciao", "translated")
            self.assertEqual(c.pending(), [])
            c.set_status("a.xhtml", "fail")
            self.assertEqual(c.pending(), ["a.xhtml"])

    def test_done_prunes_the_file_and_its_units(self):
        with Cache(self.bd) as c:
            oh = Cache.hash_text("hello")
            c.put_file("a.xhtml", oh, "Ciao", "fail")
            c.put_unit("a.xhtml", 0, oh, "Ciao", "ok")
            c.done("a.xhtml")
            self.assertIsNone(c.get("a.xhtml"))
            self.assertEqual(c.units("a.xhtml", oh), {})

    def test_units_are_keyed_by_source_hash_and_count_attempts(self):
        with Cache(self.bd) as c:
            oh = Cache.hash_text("hello")
            c.put_unit("a.xhtml", 0, oh, None, "fail", "placeholder <g1> missing")
            c.put_unit("a.xhtml", 0, oh, "Ciao <em>mondo</em>", "ok")
            c.put_unit("a.xhtml", 1, oh, None, "fail", "text abbreviated")
            rows = c.units("a.xhtml", oh)
            self.assertEqual(rows[0]["attempts"], 2)
            self.assertEqual(rows[0]["text"], "Ciao <em>mondo</em>")
            self.assertEqual(c.unit_status("a.xhtml"), (1, 1))
            self.assertEqual(c.failed_units("a.xhtml"), [(1, 1, "text abbreviated")])
            # the source changed: the old rows are invisible, and a new row
            # for the new source starts counting from one
            self.assertEqual(c.units("a.xhtml", Cache.hash_text("other")), {})
            c.put_unit("a.xhtml", 0, Cache.hash_text("other"), "x", "ok")
            self.assertEqual(c.units("a.xhtml", Cache.hash_text("other"))[0]["attempts"], 1)

    def test_persistence_across_reopen(self):
        with Cache(self.bd) as c:
            c.put_file("a.xhtml", "h", "Ciao", "fail", 1, 2)
        with Cache(self.bd) as c:
            self.assertEqual(c.get("a.xhtml")["text"], "Ciao")

    def test_an_old_whole_file_table_is_dropped(self):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.bd, ".cache.sqlite"))
        conn.execute("CREATE TABLE translations (rel TEXT PRIMARY KEY, raw_text TEXT)")
        conn.execute("INSERT INTO translations VALUES ('a.xhtml', '<html>old</html>')")
        conn.commit()
        conn.close()
        with Cache(self.bd) as c:
            self.assertEqual(c.pending(), [])
            tables = {r[0] for r in c.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("translations", tables)


if __name__ == "__main__":
    unittest.main()
