"""Configurable table discovery across native bootstrap and app factories."""

import importlib
import sys

import pytest
import yaml

from papilio.api.application import create_app
from papilio.core.bootstrap import Bootstrapper, get_bootstrapper
from papilio.core.config import AppConfig, Settings
from papilio.scaffolding import project


@pytest.fixture
def table_package(tmp_path, monkeypatch):
    package = "table_path_test"
    root = tmp_path / package
    sources = {
        "__init__.py": "",
        "feature/__init__.py": "",
        "feature/domain/__init__.py": "",
        "feature/infra/__init__.py": "",
        "feature/infra/tables.py": "source = 'default'\n",
        "feature/infra/pg/__init__.py": "",
        "feature/infra/pg/tables/__init__.py": "",
        "feature/infra/pg/tables/records.py": "source = 'postgresql'\n",
    }
    for relative, source in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield package
    for name in tuple(sys.modules):
        if name == package or name.startswith(package + "."):
            del sys.modules[name]


@pytest.mark.parametrize("custom", [False, True])
def test_discovery_uses_only_selected_table_path(table_package, custom):
    bootstrap = (
        Bootstrapper((table_package,), tables_path="infra.pg.tables")
        if custom
        else Bootstrapper((table_package,))
    )
    bootstrap.boot_sqlmodels()
    path = "infra.pg.tables.records" if custom else "infra.tables"
    imported = sys.modules[f"{table_package}.feature.{path}"]
    assert imported.source == ("postgresql" if custom else "default")
    other = "infra.tables" if custom else "infra.pg.tables.records"
    assert f"{table_package}.feature.{other}" not in sys.modules
    bootstrap.boot_sqlmodels()
    assert sys.modules[f"{table_package}.feature.{path}"] is imported


def test_custom_file_and_broken_import_keep_native_semantics(
    table_package, tmp_path
):
    root = tmp_path / table_package / "feature" / "infra" / "pg"
    source = root / "other.py"
    source.write_text("import table_path_missing_dependency\n")
    bootstrap = Bootstrapper((table_package,), tables_path="infra.pg.absent")
    bootstrap.boot_sqlmodels()
    bootstrap.tables_path = "infra.pg.other"
    with pytest.raises(ModuleNotFoundError, match="table_path_missing"):
        bootstrap.boot_sqlmodels()
    source.write_text("source = 'file'\n")
    bootstrap.boot_sqlmodels()
    assert sys.modules[f"{table_package}.feature.infra.pg.other"].source == (
        "file"
    )


def test_configured_bootstrapper_preserves_default_and_custom_path(
    table_package, monkeypatch
):
    assert AppConfig().tables_path == "infra.tables"
    settings = Settings.model_validate(
        yaml.safe_load(project.files("shop", "Shop")["config.yml"])
    )
    settings.app.modules = [table_package]
    settings.app.tables_path = "infra.pg.tables"
    monkeypatch.setattr(
        "papilio.core.bootstrap.get_settings", lambda: settings
    )
    get_bootstrapper.cache_clear()
    try:
        bootstrap = get_bootstrapper()
        bootstrap.boot_sqlmodels()
        assert bootstrap.tables_path == "infra.pg.tables"
        assert (
            f"{table_package}.feature.infra.pg.tables.records" in sys.modules
        )
        assert f"{table_package}.feature.infra.tables" not in sys.modules
    finally:
        get_bootstrapper.cache_clear()


@pytest.mark.parametrize("standalone_mcp", [False, True])
async def test_factories_honor_custom_table_path(
    table_package, standalone_mcp
):
    settings = Settings.model_validate(
        yaml.safe_load(project.files("shop", "Shop")["config.yml"])
    )
    settings.app.modules = [table_package]
    settings.app.tables_path = "infra.pg.tables"
    if standalone_mcp:
        pytest.importorskip("mcp")
        from papilio.mcp.application import create_app as create_mcp_app

        app = create_mcp_app(settings)
    else:
        app = create_app(settings)
    try:
        assert (
            f"{table_package}.feature.infra.pg.tables.records" in sys.modules
        )
        assert f"{table_package}.feature.infra.tables" not in sys.modules
    finally:
        await app.state.dishka_container.close()
