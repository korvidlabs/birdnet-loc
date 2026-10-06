"""The page template is a Python string: a bare "\\n" inside a JS string literal
becomes a real newline and breaks the whole script (shipped once, 2026-09-08).
Guard: the served script must parse. Uses node when present, else a cheap
check for raw newlines inside double-quoted JS strings."""
import re
import shutil
import subprocess

from fastapi.testclient import TestClient

from birdloc import web


def _script(path: str = "/") -> str:
    html = TestClient(web.app).get(path).text
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[0]



def test_inline_script_parses(tmp_path):
    js = _script("/")
    if shutil.which("node"):
        p = tmp_path / "page.js"
        p.write_text(js)
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    else:
        for line in js.split("\n"):
            # an odd number of unescaped quotes on a line = a string that was split by a newline
            assert line.count('"') - line.count('\\"') != 1 or line.strip().startswith("//"), line
