import importlib.util
import io
import os
import struct
import tempfile
import unittest
import zlib

from PIL import Image, PngImagePlugin

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check_png_metadata", os.path.join(HERE, "..", "check_png_metadata.py"))
cpm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpm)


def png(info=None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def info(*items):
    """items: (kind, key, value)，kind 是 text／ztxt／itxt／itxt-z。"""
    pi = PngImagePlugin.PngInfo()
    for kind, key, value in items:
        if kind == "text":
            pi.add_text(key, value)
        elif kind == "ztxt":
            pi.add_text(key, value, zip=True)
        elif kind == "itxt":
            pi.add_itxt(key, value)
        else:
            pi.add_itxt(key, value, zip=True)
    return pi


class PngMetadataTest(unittest.TestCase):
    def check(self, data, max_text=1024):
        return cpm.problems_for("x.png", data, max_text)

    def test_clean_png_passes(self):
        self.assertEqual(self.check(png()), [])

    def test_small_harmless_text_passes(self):
        self.assertEqual(self.check(png(info(("text", "Software", "Pillow"), ("itxt", "Title", "種子")))), [])

    def test_forbidden_keys_fail_in_every_chunk_kind(self):
        for kind in ("text", "ztxt", "itxt", "itxt-z"):
            for key in ("prompt", "Workflow", "parameters"):
                with self.subTest(kind=kind, key=key):
                    probs = self.check(png(info((kind, key, '{"a": 1}'))))
                    self.assertEqual(len(probs), 1)
                    self.assertIn(key, probs[0])

    def test_large_text_fails_even_with_harmless_key(self):
        big = "x" * 2000
        for kind in ("text", "ztxt", "itxt", "itxt-z"):
            with self.subTest(kind=kind):
                probs = self.check(png(info((kind, "Comment", big))))
                self.assertEqual(len(probs), 1)
                self.assertIn("2000", probs[0])
        self.assertEqual(self.check(png(info(("text", "Comment", "x" * 1000)))), [])

    def test_content_is_never_printed(self):
        probs = self.check(png(info(("text", "prompt", "SECRET-PROMPT-TEXT"))))
        self.assertNotIn("SECRET-PROMPT-TEXT", "".join(probs))

    def test_broken_png_fails(self):
        self.assertTrue(self.check(b"not a png"))
        self.assertTrue(self.check(png()[:30]))
        # zTXt 內容壞掉
        body = b"Comment\0\0" + b"garbage"
        chunk = struct.pack(">I", len(body)) + b"zTXt" + body + struct.pack(">I", zlib.crc32(b"zTXt" + body))
        data = png()
        self.assertTrue(self.check(data[:33] + chunk + data[33:]))

    def test_cli_on_files(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad = os.path.join(d, "good.png"), os.path.join(d, "bad.png")
            open(good, "wb").write(png())
            open(bad, "wb").write(png(info(("text", "workflow", "{}"))))
            import subprocess, sys
            script = os.path.join(HERE, "..", "check_png_metadata.py")
            self.assertEqual(subprocess.run([sys.executable, script, good], capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run([sys.executable, script, good, bad], capture_output=True).returncode, 1)

    def test_repo_pngs_are_clean(self):
        import subprocess, sys
        script = os.path.join(HERE, "..", "check_png_metadata.py")
        r = subprocess.run([sys.executable, script, "--tree"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
