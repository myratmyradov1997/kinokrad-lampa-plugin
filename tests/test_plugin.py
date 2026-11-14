import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_PATH = Path(__file__).parents[1] / "plugin.js"
PLUGIN = PLUGIN_PATH.read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_plugin_is_valid_javascript():
    subprocess.run(["node", "--check", str(PLUGIN_PATH)], check=True)


def test_remote_and_pointer_activation_are_both_supported():
    assert "hover:enter click" in PLUGIN
    assert "Lampa.Controller.collectionSet" in PLUGIN
    assert "toggle: bindCollection" in PLUGIN  # без рекурсии toggle->focus->toggle
    assert "Lampa.Controller.toggle('content')" in PLUGIN
    assert "target.trigger('hover:enter')" in PLUGIN
    assert "scroll.immediate" in PLUGIN


def test_fast_paths_are_wired():
    assert "/api/prepare?" in PLUGIN          # поиск+карточка+прогрев при открытии карточки
    assert "/api/prefetch?" in PLUGIN         # подготовка потока при наведении
    assert "default_audio" in PLUGIN          # без лишнего выбора дорожки
    assert "Lampa.Player.play" in PLUGIN
    assert "work.quality_switched" in PLUGIN  # восстановление ссылки после ошибки
    assert "Lampa.Search.addSource" in PLUGIN
    assert "view--torrent" in PLUGIN


def test_player_keeps_remote_focus():
    # список под плеером перерисовывается без перехвата фокуса
    assert "renderBehindPlayer()" in PLUGIN
    assert "|| silent) return;" in PLUGIN
