import pandas as pd
import pytest

from engine import run_abcd_engine


@pytest.mark.parametrize("factory", ["application", "kering"])
def test_postgresql_factory_uses_installed_driver_without_network(factory, monkeypatch):
    import ast
    import builtins
    from pathlib import Path
    import socket
    from sqlalchemy import create_engine, URL
    from db_config import DBConfig

    config = DBConfig(host="example.invalid", port=5432, database="offline",
                      user="offline", password="offline:p@ss/%?#")
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name == "psycopg":
            raise AssertionError("psycopg3 is not a project dependency")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    def no_network(*args, **kwargs):
        raise AssertionError("Engine construction must not connect to Odoo")
    monkeypatch.setattr(socket, "create_connection", no_network)

    if factory == "application":
        source = Path(__file__).resolve().parents[1] / "app_enhanced.py"
        module = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get_db_engine")
        function.decorator_list = []
        namespace = {"load_db_config": lambda: config, "create_engine": create_engine, "URL": URL}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        engine = namespace["get_db_engine"]()
    else:
        import kering_jobs
        monkeypatch.setattr(kering_jobs, "load_db_config", lambda: config)
        engine = kering_jobs.db_engine()
    try:
        assert engine.url.drivername == "postgresql+psycopg2"
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
        assert engine.url.password == config.password
        assert engine.url.host == config.host
    finally:
        engine.dispose()


def test_days_without_sales_for_d_override_changes_classification() -> None:
    reference_date = pd.Timestamp("2025-01-01")
    products = pd.DataFrame(
        [
            {
                "PVO": 100,
                "Stock": 4,
                "Primera Compra": reference_date - pd.Timedelta(days=365),
                "Última Venta": reference_date - pd.Timedelta(days=200),
                "Num_Ventas_180D": 0,
            }
        ]
    )

    default_result = run_abcd_engine(products, reference_date=reference_date)
    custom_result = run_abcd_engine(
        products,
        days_without_sales_for_d=250,
        reference_date=reference_date,
    )

    assert default_result.loc[0, "ABCD"] == "D"
    assert custom_result.loc[0, "ABCD"] == "A"


def test_default_d_threshold_is_180_days() -> None:
    reference_date = pd.Timestamp("2025-01-01")

    def classify(days_since_sale: int) -> pd.Series:
        products = pd.DataFrame(
            [
                {
                    "PVO": 100,
                    "Stock": 4,
                    "Primera Compra": reference_date - pd.Timedelta(days=365),
                    "Última Venta": reference_date - pd.Timedelta(days=days_since_sale),
                    "Num_Ventas_180D": 0,
                }
            ]
        )
        return run_abcd_engine(products, reference_date=reference_date).iloc[0]

    assert classify(179)["ABCD"] == "A"
    assert classify(180)["ABCD"] == "D"
    assert classify(180)["Motivo"] == "Sin ventas 180+ días (sin demanda reciente)"