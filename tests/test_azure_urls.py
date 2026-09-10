"""Creasy 0.9.1 TFS identity root — connectionData is not collection-scoped."""

from src.azure.urls import identity_root, identity_roots


def test_identity_root_keeps_tfs_app_and_strips_collection():
    assert identity_root("https://tfs02.company.com.tr/tfs") == "https://tfs02.company.com.tr/tfs"
    assert (
        identity_root("https://tfs02.company.com.tr/tfs/ExampleCollection")
        == "https://tfs02.company.com.tr/tfs"
    )
    assert identity_root("https://tfs02.company.com.tr") == "https://tfs02.company.com.tr"
    assert identity_root("https://dev.azure.com/contoso") == "https://dev.azure.com/contoso"
    assert identity_root("https://dev.azure.com/contoso/proj") == "https://dev.azure.com/contoso"


def test_identity_roots_host_only_tries_root_then_tfs():
    roots = identity_roots("https://tfs02.company.com.tr")
    assert roots[0] == "https://tfs02.company.com.tr"
    assert "https://tfs02.company.com.tr/tfs" in roots
    assert not any("ExampleCollection" in r for r in roots)


def test_identity_roots_tfs_url_stays_on_app_root():
    assert identity_roots("https://tfs02.company.com.tr/tfs") == [
        "https://tfs02.company.com.tr/tfs"
    ]
    roots = identity_roots("https://tfs02.company.com.tr/tfs/ExampleCollection")
    assert roots[0] == "https://tfs02.company.com.tr/tfs"
    assert "https://tfs02.company.com.tr/tfs/ExampleCollection" not in roots[:1]
