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


def test_provider_page_imports_with_only_the_files_the_image_copies(tmp_path):
    """Simulate the image: only Dockerfile COPY sources are available, then import the page."""
    import shutil
    import subprocess
    import sys

    image = tmp_path / "app"
    image.mkdir()
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if not line.startswith("COPY ") or "--from" in line:
            continue
        *sources, destination = line.split()[1:]
        for source in sources:
            origin = ROOT / source
            target = image / destination.removeprefix("./")
            if destination == ".":
                target = image / origin.name
            if origin.is_dir():
                shutil.copytree(origin, target, dirs_exist_ok=True)
            elif origin.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(origin, target if not str(destination).endswith("/") else target / origin.name)
    code = (
        "import provider_images_ui, provider_odoo_orders, repository_catalog_readonly, "
        "marcolin_images, safilo_images, nuorder_images\n"
        "from app_pages.image_library_page import _visible_previews\n"
        "from image_thumbnails import validate_visible\n"
    )
    run = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(image),
                              "PYTHONDONTWRITEBYTECODE": "1"})
    assert run.returncode == 0, run.stderr[-800:]


def test_runtime_import_smoke_covers_provider_modules():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert all(m in workflow for m in MODULES)
