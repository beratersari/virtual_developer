"""Creasy 0.9.1 TFS identity root — connectionData is not collection-scoped."""

from src.azure.urls import (
    identity_root,
    identity_roots,
    parse_tfs_collection_url,
    require_tfs_collection_url,
)


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


def test_identity_root_strips_collection_without_tfs_vdir():
    assert (
        identity_root("https://ado.example.com/DefaultCollection")
        == "https://ado.example.com"
    )
    roots = identity_roots("https://ado.example.com/DefaultCollection")
    assert roots[0] == "https://ado.example.com"
    assert "https://ado.example.com/tfs" not in roots


def test_parse_collection_url_keeps_or_omits_tfs():
    assert (
        parse_tfs_collection_url(
            "https://tfs.example.com/tfs/DefaultCollection"
        )
        == "https://tfs.example.com/tfs/DefaultCollection"
    )
    assert (
        parse_tfs_collection_url("https://ado.example.com/DefaultCollection")
        == "https://ado.example.com/DefaultCollection"
    )
    assert (
        parse_tfs_collection_url(
            "https://ado.example.com/DefaultCollection/Demo/_git/app"
        )
        == "https://ado.example.com/DefaultCollection"
    )
    assert parse_tfs_collection_url("https://ado.example.com") == ""
    assert parse_tfs_collection_url("https://ado.example.com/tfs") == ""
    assert (
        require_tfs_collection_url("https://ado.example.com/MyCol")
        == "https://ado.example.com/MyCol"
    )
