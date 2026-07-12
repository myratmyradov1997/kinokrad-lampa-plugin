from pathlib import Path


PLUGIN = (Path(__file__).parents[1] / "plugin.js").read_text(encoding="utf-8")


def test_remote_and_pointer_activation_are_both_supported():
    assert "hover:enter click" in PLUGIN
    assert ".off('hover:enter click'," not in PLUGIN
    assert "Lampa.Controller.collectionSet" in PLUGIN
    assert "Lampa.Controller.toggle('content')" in PLUGIN


def test_expected_components_and_player_exist():
    assert "kinokrad_catalog" in PLUGIN
    assert "kinokrad_detail" in PLUGIN
    assert "Lampa.Player.play" in PLUGIN
    assert "/api/resolve" in PLUGIN


def test_kinokrad_card_is_not_replaced_by_tmdb_search():
    assert "TMDB" not in PLUGIN
    assert "search/multi" not in PLUGIN
