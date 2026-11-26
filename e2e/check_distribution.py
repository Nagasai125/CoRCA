"""Check that a built production wheel ships the console without development tools."""
import sys
from pathlib import Path
from zipfile import ZipFile


def check(path):
    with ZipFile(path) as wheel:
        names = wheel.namelist()
        assert any(name.endswith('.dist-info/licenses/LICENSE') for name in names), 'Missing project license'
        assert b'License-Expression: MIT' in wheel.read(next(name for name in names if name.endswith('.dist-info/METADATA')))
        assert {"backend/api.py", "backend/evidence.py", "backend/reviews.py", "frontend/workspace-visuals.js", "frontend/workspace-visuals.css", "corca/corca.py", "frontend/app.html",
                "frontend/index.html", "frontend/help-topics.json"} <= set(names), "Missing application assets"
        assert any(name.startswith("frontend/fonts/") and name.endswith(".woff") for name in names), "Missing console fonts"
        unwanted = [name for name in names if name.startswith(("runtime/", "scripts/", "lab/", "tests/", "e2e/"))
                    or Path(name).name.startswith("test_")]
        assert not unwanted, f"Development files shipped: {unwanted}"
        entries = wheel.read(next(name for name in names if name.endswith(".dist-info/entry_points.txt"))).decode()
        assert "corca-serve = backend.api:main" in entries
        assert "corca-admin = backend.admin:main" in entries
        assert "scripts." not in entries, "Research entry points shipped"
    print(f"Production package checked: {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python e2e/check_distribution.py path/to/corca.whl")
    check(sys.argv[1])
