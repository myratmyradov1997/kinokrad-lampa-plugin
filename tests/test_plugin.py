from pathlib import Path


PLUGIN = (Path(__file__).parents[1] / "plugin.js").read_text(encoding="utf-8")


def test_remote_and_pointer_activation_are_both_supported():
    assert "hover:enter click" in PLUGIN
    assert "Lampa.Controller.collectionSet" in PLUGIN
    assert "Lampa.Controller.toggle('content')" in PLUGIN
    assert "scroll.immediate(node, true)" in PLUGIN
    assert "scrollToFocused()" in PLUGIN
    assert "scroll.reset()" in PLUGIN
    assert "kk-online-scroll{height:100%}" in PLUGIN
    assert "enter: function ()" in PLUGIN
    assert "target.trigger('hover:enter')" in PLUGIN


def test_expected_components_and_player_exist():
    assert "kinokrad_online" in PLUGIN
    assert "Lampa.Player.play" in PLUGIN
    assert "/api/resolve" in PLUGIN
    assert "element.quality = audio.quality" in PLUGIN
    assert "work.quality_switched" in PLUGIN
    assert "selectedFile.season" in PLUGIN
    assert "if (enterAction) return enterAction();" not in PLUGIN


def test_plugin_uses_native_lampa_cards_and_search():
    assert "Lampa.Search.addSource" in PLUGIN
    assert "Lampa.Listener.follow('full'" in PLUGIN
    assert "view--torrent" in PLUGIN
    assert "/api/catalog" not in PLUGIN
    assert "kinokrad_catalog" not in PLUGIN
    assert "decodeURIComponent(query)" in PLUGIN
