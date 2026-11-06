"""Keep the published API contract and current documentation connected to source."""
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from backend.api import ConnectionSetup, create_app
from backend.telemetry import validate_source


ROOT = Path(__file__).resolve().parents[2]


def test_openapi_snapshot_matches_backend():
    assert json.loads((ROOT / "docs/api/openapi.json").read_text()) == create_app().openapi()


def test_api_guide_covers_every_operation():
    schema = create_app().openapi()
    expected = {(method.upper(), path) for path, operations in schema["paths"].items()
                for method in operations}
    guide = (ROOT / "docs/api/README.md").read_text()
    documented = set(re.findall(r"\| `(GET|POST|PATCH|PUT|DELETE)` \| `([^`]+)` \|", guide))
    # The protected schema endpoint deliberately excludes itself from OpenAPI.
    assert documented == expected | {("GET", "/openapi.json")}


def test_documented_connection_payload_is_supported():
    guide = (ROOT / "docs/api/README.md").read_text()
    body = json.loads(re.search(r"<<'JSON'\n(.*?)\nJSON", guide, re.S)[1])
    setup = ConnectionSetup.model_validate(body)
    assert validate_source(setup.source)["provider"] == "prometheus"


def test_current_documentation_links_resolve():
    documents = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "lab/README.md",
                 ROOT / "e2e/README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    broken = []
    for document in documents:
        for target in re.findall(r"\]\(([^\s)]+)\)", document.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            if not (document.parent / unquote(url.path)).exists():
                broken.append(f"{document.relative_to(ROOT)}: {target}")
    assert not broken, "Broken documentation links:\n" + "\n".join(broken)
