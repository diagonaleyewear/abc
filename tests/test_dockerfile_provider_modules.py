"""The provider-image page is in the navigation, so its modules must ship in the image (#59)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("provider_images", "provider_images_ui", "provider_odoo_orders",
           "repository_catalog_readonly", "marcolin_images", "safilo_images", "nuorder_images")


def _copied(dockerfile):
    names = set()
    for line in dockerfile.splitlines():
        if line.startswith("COPY ") and "--from" not in line:
            names.update(part for part in line.split()[1:-1])
    return names


def test_dockerfile_copies_every_provider_module():
    copied = _copied((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert [m for m in MODULES if f"{m}.py" not in copied] == []


def test_dockerfile_copies_local_dependencies_of_provider_modules():
    copied = _copied((ROOT / "Dockerfile").read_text(encoding="utf-8"))
    for module in MODULES:
        text = (ROOT / f"{module}.py").read_text(encoding="utf-8")
        for dep in set(re.findall(r"^\s*(?:from|import) (\w+)", text, re.M)):
            if (ROOT / f"{dep}.py").exists():
                assert f"{dep}.py" in copied, f"{module} imports {dep}, which the image lacks"


def test_runtime_import_smoke_covers_provider_modules():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert all(m in workflow for m in MODULES)
