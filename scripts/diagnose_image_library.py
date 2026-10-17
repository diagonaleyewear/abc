"""Read-only diagnosis of the image library statistics, per provider.

Run it where the repository lives (for example inside the app container):

    python scripts/diagnose_image_library.py

It never writes to the repository. It explains the three headline figures:
  imágenes = stored image records, EAN = distinct EAN, modelos = distinct verified model values
and, per provider, how many EAN have a verified model, only a technical value (file name, view
word, EAN-like number) or no model at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_library as library  # noqa: E402
from image_repository import ImageRepository  # noqa: E402


def main() -> int:
    records = ImageRepository().catalog_records()
    groups = library.group_records(records)
    totals = library.summary(groups)
    print(f"Imágenes: {totals['images']}  EAN: {totals['eans']}  Modelos verificados: {totals['models']}")
    print(f"{'Proveedor':<16}{'Imágenes':>9}{'EAN':>6}{'Con modelo':>11}{'Solo técnico':>13}{'Sin modelo':>11}{'Modelos':>8}")
    for row in library.metadata_coverage(records):
        print(f"{row['provider']:<16}{row['images']:>9}{row['eans']:>6}{row['eans_with_verified_model']:>11}"
              f"{row['eans_with_technical_model_only']:>13}{row['eans_without_model']:>11}{row['distinct_models']:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
