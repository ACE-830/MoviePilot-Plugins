"""保证发布索引与插件实际代码保持一致。"""

import ast
import json
from pathlib import Path


def test_market_metadata_matches_source():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "package.v3.json").read_text())
    assert set(manifest) == {"DoubanWatchingPlus"}
    entry = manifest["DoubanWatchingPlus"]
    tree = ast.parse((root / "plugins.v3/doubanwatchingplus/__init__.py").read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoubanWatchingPlus")
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in cls.body
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)}
    assert cls.name == "DoubanWatchingPlus"
    assert values["plugin_version"] == entry["version"] == "1.1"
    assert values["plugin_desc"] == entry["description"]
    assert values["plugin_name"] == entry["name"]
    assert values["plugin_icon"] == entry["icon"]
    assert values["plugin_author"] == entry["author"] == "ACE-830"
    assert values["plugin_config_prefix"] == "doubanwatchingplus_"
    assert not (root / "plugins.v3/doubanwatching").exists()
    assert list(entry["history"]) == ["v1.1", "v1.0"]
    readme = (root / "README.md").read_text()
    for version, description in entry["history"].items():
        assert f"- **{version}**：{description}" in readme
    assert values["author_url"] == "https://github.com/ACE-830"
    assert f"[**{entry['author']}**]({values['author_url']})" in readme
    assert "原版插件作者：[**honue**](https://github.com/honue)" in readme
    assert entry["system_version"] == ">=3.0.0,<4.0.0"
    assert json.loads((root / "package.json").read_text()) == {}
    assert json.loads((root / "package.v2.json").read_text()) == {}
