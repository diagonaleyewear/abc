import unittest
import os
from io import BytesIO
from pathlib import Path

import pandas as pd

from app_exports import (
    dataframe_to_excel_bytes,
    load_masterdata_file,
    normalize_result_export_schema,
    split_report_warnings,
)
from transform_luxottica_masterdata import (
    RESULT_EXPORT_COLUMNS,
    find_dictionary_values_not_resolved,
    normalize_color,
    normalize_material,
    normalize_shape,
    transform_masterdata,
)


class TransformMasterdataTests(unittest.TestCase):
    def test_color_dictionary_is_checked_before_alphanumeric_fallback(self) -> None:
        color_map = {"BLACK": "Negro", "GREEN": "Verde", "901": "Negro"}

        self.assertEqual(normalize_color("BLACK", color_map), "Negro")
        self.assertEqual(normalize_color("GREEN", color_map), "Verde")
        self.assertEqual(normalize_color("901", color_map), "Negro")

    def test_supplier_color_removes_slashes(self) -> None:
        self.assertEqual(normalize_color("C395/3", {}), "C3953")

    def test_source_collection_classifies_optical_frames(self) -> None:
        row = self._base_row()
        row["Color de las lentes"] = ""
        row["Tipo"] = "Aro completo"

        output, report, _, _, _ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Categoría"], "MONTURAS")

    def test_confirmed_dg5116_blue_light_frame_is_not_sunglasses(self) -> None:
        row = self._base_row()
        row.update({
            "UPC": "8056266003066",
            "Nombre de la marca": "DOLCE & GABBANA",
            "Código de marca": "DG",
            "Código del modelo": "0DG5116",
            "Color": "3091",
            "Calibre": "54",
            "Categoría": "Productos finales: Gafas",
            "Color de las lentes": "Transparente con Filtro de Luz Azul",
        })

        output, report, *_ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Barcode"], "8056266003066")
        self.assertEqual(output.iloc[0]["Categoría"], "MONTURAS")
        self.assertEqual(output.iloc[0]["Color"], "3091")
        self.assertEqual(output.iloc[0]["Calibre"], "54")

    def test_classification_uses_only_original_collection(self) -> None:
        for collection, expected in [
            ("  GÁFAS  DE \t VÍSTA ", "MONTURAS"),
            ("GAFAS DE SOL", "GAFAS DE SOL"),
            ("Gafas de vista junior", "MONTURAS"),
            ("Gafas de sol junior", "GAFAS DE SOL"),
        ]:
            with self.subTest(collection=collection):
                row = self._base_row()
                row.update({
                    "Colección": collection,
                    "Categoría": "Gafas de sol" if expected == "MONTURAS" else "MONTURAS",
                    "Nombre del modelo": "SOL",
                    "Tipo": "Sol",
                    "Color de las lentes": "Transparente con Filtro de Luz Azul",
                })
                rules = [
                    {"Columna": "Colección", "Valor": collection, "Transformado": "Gafas de sol"},
                    {"Columna": "Categoría", "Valor": row["Categoría"], "Transformado": "INCORRECTA"},
                ]
                output, report, *_ = transform_masterdata(
                    pd.DataFrame([row]), color_map={}, dictionary_rules=rules,
                )
                self.assertEqual(output.iloc[0]["Categoría"], expected)
                self.assertEqual(report.category_review_rows, [])

    def test_unknown_collection_is_kept_and_explicitly_flagged(self) -> None:
        for collection in ["", "General", "Meta Smart", "Gafas de vista desconocidas"]:
            with self.subTest(collection=collection):
                row = self._base_row()
                row.update({"Colección": collection, "Categoría": "Productos finales: Gafas"})
                original = pd.DataFrame([row])
                output, report, *_ = transform_masterdata(original, color_map={})
                self.assertEqual(len(output), 1)
                self.assertEqual(output.iloc[0]["Categoría"], "")
                self.assertEqual(report.category_review_rows[0]["UPC"], row["UPC"])
                self.assertEqual(report.category_review_rows[0]["Colección"], collection)
                self.assertEqual(report.category_review_rows[0]["fila_excel"], 2)
                self.assertEqual(report.to_dict()["category_review_rows"], report.category_review_rows)
                self.assertTrue(split_report_warnings(report)[0])
                pd.testing.assert_frame_equal(original, pd.DataFrame([row]))

    def test_sunglasses_preview_xlsx_and_csv_categories_match(self) -> None:
        row = self._base_row()
        row["Colección"] = "Gafas de sol"
        row["Color de las lentes"] = ""
        output, _, *_ = transform_masterdata(pd.DataFrame([row]), color_map={})
        export = normalize_result_export_schema(output)
        preview = export.map(lambda value: str(value).upper())
        downloaded = load_masterdata_file(BytesIO(dataframe_to_excel_bytes(export)))
        csv = pd.read_csv(BytesIO(export.to_csv(index=False, sep=";").encode("utf-8-sig")),
                          dtype=str, sep=";").fillna("")
        self.assertEqual(list(export.columns), RESULT_EXPORT_COLUMNS)
        self.assertEqual(preview["Categoría"].tolist(), ["GAFAS DE SOL"])
        self.assertEqual(downloaded["Categoría"].tolist(), preview["Categoría"].tolist())
        self.assertEqual(csv["Categoría"].tolist(), preview["Categoría"].tolist())

    def test_ray_ban_junior_follows_explicit_collection(self) -> None:
        for collection, category in [
            ("Gafas de vista junior", "MONTURAS"), ("Gafas de sol junior", "GAFAS DE SOL"),
        ]:
            with self.subTest(collection=collection):
                row = self._base_row()
                row.update({"Nombre de la marca": "RAY BAN JUNIOR", "Colección": collection})
                output, _, *_ = transform_masterdata(pd.DataFrame([row]), color_map={})
                self.assertEqual(output.iloc[0]["Marca"], "Ray Ban Junior")
                self.assertEqual(output.iloc[0]["Género"], "Niño")
                self.assertEqual(output.iloc[0]["Categoría"], category)

    def test_color_code_is_preserved_in_masterdata(self) -> None:
        row = self._base_row()
        row["Color"] = "900642"
        output, report, *_ = transform_masterdata(
            pd.DataFrame([row]),
            color_map={"900642": "No debe aplicarse"},
        )

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Color"], "900642")

    def test_supplier_color_slash_is_removed_from_transformed_output(self) -> None:
        row = self._base_row()
        row["Color"] = "C395/3"

        output, report, *_ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Color"], "C3953")

    def test_shape_dictionary_resolves_odoo_names(self) -> None:
        shape_map = {
            "aviator": "Aviator",
            "cat eye": "Cat Eye",
            "rectangular/cuadrada": "Rectangular/Cuadrada",
        }

        self.assertEqual(normalize_shape("Aviator", shape_map), "Aviator")
        self.assertEqual(normalize_shape("Cat Eye", shape_map), "Cat Eye")
        self.assertEqual(normalize_shape("Rectangular/Cuadrada", shape_map), "Rectangular/Cuadrada")

    def test_irregular_shape_and_material_rules_are_canonicalized(self) -> None:
        self.assertEqual(normalize_shape("Irregular"), "RECTANGULAR/CUADRADA")
        self.assertEqual(normalize_material("Nylon"), "ACETATO")
        self.assertEqual(normalize_material("Acetato y Metal"), "ACETATO/METAL")

    def test_unresolved_dictionary_values_are_reported(self) -> None:
        source = pd.DataFrame(
            {
                "Color": ["BLACK", "CUSTOM_GREEN", "UNKNOWN_COLOR"],
                "Forma": ["Aviator", "CUSTOM_SHAPE", "UNKNOWN_SHAPE"],
            }
        )
        unresolved = find_dictionary_values_not_resolved(
            source,
            color_map={"BLACK": "Negro"},
            shape_map={"aviator": "Aviator"},
            dictionary_rules=[
                {"Columna": "Color", "Valor": "CUSTOM_GREEN", "Transformado": "Verde"},
                {"Columna": "Forma", "Valor": "CUSTOM_SHAPE", "Transformado": "Geométrica"},
            ],
        )

        self.assertEqual(
            {(item["Columna"], item["Valor"]) for item in unresolved},
            {("Forma", "UNKNOWN_SHAPE")},
        )

    def _base_row(self) -> dict[str, str]:
        return {
            "Punto de venta": "0001",
            "Código del modelo": "0RB-1234/56",
            "Calibre": "52",
            "Color": "000123",
            "UPC": "1234567890123",
            "Nombre de la marca": "RAY BAN",
            "Código de marca": "RB",
            "Colección": "Gafas de vista",
            "Género": "Unisex",
            "Forma": "Rectangular",
            "Tipo": "Aro Completo",
            "Nombre del modelo": "WAYFARER",
            "Descripción del color": "BLACK",
            "Material del frente": "Metal",
            "Color del frontal": "BLACK",
            "Material de las lentes": "Cristal",
            "Color de las lentes": "GREEN",
            "Fotocromático": "",
            "Polarizado": "X",
            "Largo de varilla": "145",
            "Dimensión del puente": "18",
            "PVP sugerido": "100",
            "PVO": "50",
            "Categoría": "",
        }

    def test_rayban_meta_detected(self) -> None:
        row = self._base_row()
        row["Colección"] = "Meta Smart"
        row["Nombre del modelo"] = "Wayfarer Meta"

        output, report, _, _, _ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Marca"], "Ray Ban Meta")

    def test_brand_name_is_canonicalized_from_brand_map(self) -> None:
        row = self._base_row()
        row["Nombre de la marca"] = "EMPORIO ARMANI"
        row["Código de marca"] = "EA"

        output, report, _, _, _ = transform_masterdata(
            pd.DataFrame([row]),
            color_map={},
            brand_map={"emporio armani": "Emporio Armani"},
        )

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Marca"], "Emporio Armani")

    def test_brand_alias_tiffany_is_resolved(self) -> None:
        row = self._base_row()
        row["Nombre de la marca"] = "TIFFANY"
        row["Código de marca"] = "TF"

        output, report, _, _, _ = transform_masterdata(
            pd.DataFrame([row]),
            color_map={},
            brand_map={"tiffany & co.": "Tiffany & Co."},
        )

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Marca"], "Tiffany & Co.")
        self.assertEqual(report.unmatched_brand_names, [])

    def test_brand_code_is_derived_from_model_code_when_missing(self) -> None:
        row = self._base_row()
        row["Nombre de la marca"] = "DOLCE & GABBANA"
        row["Código de marca"] = ""
        row["Código del modelo"] = "0DG-1322"

        output, report, _, _, _ = transform_masterdata(
            pd.DataFrame([row]),
            color_map={},
            brand_map={"dolce gabbana": "Dolce Gabbana"},
        )

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Modelo"], "DG1322")

    def test_generics_remains_as_incidence(self) -> None:
        row = self._base_row()
        row["Nombre de la marca"] = "GENERICS"
        row["Código de marca"] = "GN"

        output, report, _, _, brand_audit = transform_masterdata(
            pd.DataFrame([row]),
            color_map={},
            brand_map={"ray ban": "Ray Ban"},
        )

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Marca"], "GENERICS")
        self.assertEqual(report.unmatched_brand_names, ["GENERICS"])
        self.assertEqual(brand_audit.iloc[0]["marca_origen"], "GENERICS")
        self.assertEqual(brand_audit.iloc[0]["existe_en_bd"], "NO")

    def test_invalid_product_discarded(self) -> None:
        valid = self._base_row()
        invalid = self._base_row()
        invalid["UPC"] = "9988776655443"
        invalid["Código del modelo"] = ""

        output, report, discarded_audit, _, _ = transform_masterdata(pd.DataFrame([valid, invalid]), color_map={})

        self.assertEqual(report.discarded_invalid_product, 1)
        self.assertEqual(report.total_output, 1)
        self.assertEqual(len(output), 1)
        self.assertIn("producto_invalido", discarded_audit["motivo_descarte"].tolist()[0])

    def test_accessory_by_model_code_prefix_is_discarded(self) -> None:
        valid = self._base_row()
        accessory = self._base_row()
        accessory["UPC"] = "1111111111111"
        accessory["Código del modelo"] = "AOO9188LS"
        accessory["Colección"] = ""
        accessory["Nombre del modelo"] = "HOLBROOK"
        accessory["Categoría"] = ""
        accessory["Tipo"] = ""

        output, report, discarded_audit, _, _ = transform_masterdata(pd.DataFrame([valid, accessory]), color_map={})

        self.assertEqual(report.discarded_accessories, 1)
        self.assertEqual(len(output), 1)
        reasons = ";".join(discarded_audit["motivo_descarte"].tolist())
        self.assertIn("accesorio", reasons)

    def test_accessory_whitelist_keeps_model_code(self) -> None:
        valid = self._base_row()
        accessory = self._base_row()
        accessory["UPC"] = "2222222222222"
        accessory["Código del modelo"] = "AOO9188LS"
        accessory["Nombre del modelo"] = "HOLBROOK"
        accessory["Colección"] = ""
        accessory["Categoría"] = ""
        accessory["Tipo"] = ""

        output, report, discarded_audit, _, _ = transform_masterdata(
            pd.DataFrame([valid, accessory]),
            color_map={},
            accessory_whitelist={"AOO9188LS"},
        )

        self.assertEqual(report.discarded_accessories, 0)
        self.assertEqual(report.accessory_whitelist_kept, 1)
        self.assertEqual(len(output), 2)
        self.assertEqual(len(discarded_audit), 0)

    def test_pvp_from_alias_column_is_preserved(self) -> None:
        row = self._base_row()
        row.pop("PVP sugerido", None)
        row["PVP"] = "173"

        output, report, _, _, _ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["PVP"], "173")

    def test_collection_comes_from_model_name_column(self) -> None:
        row = self._base_row()
        row["Colección"] = "VALOR_ORIGINAL_COLECCION"
        row["Nombre del modelo"] = "WAYFARER PUFFER"

        output, report, _, _, _ = transform_masterdata(pd.DataFrame([row]), color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Colección"], "WAYFARER PUFFER")

    def test_collection_comes_from_column_l_even_if_header_changes(self) -> None:
        row = self._base_row()
        row["Nombre del modelo"] = "VALOR_COLUMNA_L"
        df = pd.DataFrame([row]).rename(columns={"Nombre del modelo": "L_ORIGEN"})

        output, report, _, _, _ = transform_masterdata(df, color_map={})

        self.assertEqual(report.total_output, 1)
        self.assertEqual(output.iloc[0]["Colección"], "VALOR_COLUMNA_L")


class CustomerWorkbookRegressionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("LUXOTTICA_REGRESSION_XLSX"), "Customer workbook not supplied")
    def test_actual_902_reference_workbook(self) -> None:
        source = load_masterdata_file(Path(os.environ["LUXOTTICA_REGRESSION_XLSX"]))
        self.assertEqual(len(source), 902)
        self.assertEqual(source["Colección"].value_counts().to_dict(),
                         {"Gafas de vista": 464, "Gafas de sol": 438})
        output, report, *_ = transform_masterdata(source, color_map={})
        self.assertEqual(len(output), 902)
        self.assertEqual(output["Categoría"].value_counts().to_dict(),
                         {"MONTURAS": 464, "GAFAS DE SOL": 438})
        self.assertEqual(report.discarded, 0)
        self.assertEqual(report.category_review_rows, [])
        self.assertEqual(output["Barcode"].tolist(), source["UPC"].tolist())
        self.assertEqual(output.loc[output["Barcode"].eq("8056266003066"), "Categoría"].tolist(),
                         ["MONTURAS"])
        for target, origin in [
            ("Color", "Color"), ("Calibre", "Calibre"), ("PVO", "PVO"),
            ("PVP", "PVP Sugerido"), ("Ancho Puente", "Dimensión del puente"),
            ("Longitud Varilla", "Largo de varilla"),
        ]:
            expected = source[origin].str.replace("/", "", regex=False) if target == "Color" else source[origin]
            self.assertEqual(output[target].tolist(), expected.tolist())
        downloaded = load_masterdata_file(BytesIO(dataframe_to_excel_bytes(output)))
        pd.testing.assert_frame_equal(downloaded, output.reset_index(drop=True))
        preview_categories = output["Categoría"].str.upper().tolist()
        self.assertEqual(downloaded["Categoría"].tolist(), preview_categories)


if __name__ == "__main__":
    unittest.main()
