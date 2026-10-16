#!/usr/bin/env python3
"""只读比对 MP V3 源码合同，不导入宿主、不连接数据库或网络。"""

import argparse
import ast
from pathlib import Path


def parse(path: Path):
    """解析 Python 源码，不执行模块初始化。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def main():
    """核对真实识别签名、Webhook 字段和插件使用的宿主入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", type=Path, help="MoviePilot V3 后端仓库目录")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    host = args.backend.resolve()
    tree = parse(host / "app/chain/_recognition.py")
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "recognize_media")
    parameters = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
    errors = []
    search_tree = parse(host / "app/chain/base.py")
    search_function = next(node for node in ast.walk(search_tree)
                           if isinstance(node, ast.FunctionDef) and node.name == "search_medias")
    search_parameters = {arg.arg for arg in search_function.args.args + search_function.args.kwonlyargs}
    if not {"meta", "media_source"} <= search_parameters:
        errors.append("宿主缺少按 media_source 选择来源的搜索入口")
    service_tree = parse(host / "app/application/service.py")
    for method, required_args in {"get_service": {"name", "type_filter"},
                                  "get_services": {"type_filter"}}.items():
        function = next(node for node in ast.walk(service_tree)
                        if isinstance(node, ast.FunctionDef) and node.name == method)
        if not required_args <= {arg.arg for arg in function.args.args + function.args.kwonlyargs}:
            errors.append(f"宿主服务发现方法 {method} 参数不兼容")
    if not {"media_source", "media_id"} <= parameters or "tmdbid" in parameters:
        errors.append("宿主识别函数不符合目标 V3 统一媒体身份合同")
    scheduling = parse(host / "app/application/scheduling.py")
    for method, expected in {"add_plugin_once_job": {"plugin_id", "job_id", "func", "name", "delay_seconds", "func_kwargs"},
                             "remove_plugin_once_job": {"plugin_id", "job_id"}}.items():
        function = next((node for node in scheduling.body if isinstance(node, ast.FunctionDef) and node.name == method), None)
        if function is None or not expected <= {arg.arg for arg in function.args.args + function.args.kwonlyargs}:
            errors.append(f"宿主缺少兼容的一次性任务接口：{method}")
    used_imports = []
    for path in (root / "plugins.v3/doubanwatchingplus").glob("*.py"):
        plugin = parse(path)
        for node in ast.walk(plugin):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"recognize_media", "search_medias"}):
                supported = parameters if node.func.attr == "recognize_media" else search_parameters
                unknown = {keyword.arg for keyword in node.keywords} - supported
                if unknown:
                    errors.append(f"{path.name}:{node.lineno}: 宿主不支持参数 {unknown}")
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                module_path = host.joinpath(*node.module.split("."))
                if not module_path.with_suffix(".py").exists() and not (module_path / "__init__.py").exists():
                    errors.append(f"宿主入口不存在：{node.module}")
                used_imports.append(node.module)
    schema = parse(host / "app/schemas/mediaserver.py")
    webhook = next(node for node in schema.body if isinstance(node, ast.ClassDef) and node.name == "WebhookEventInfo")
    fields = {node.target.id for node in webhook.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    required = {"event", "channel", "item_type", "item_name", "item_path", "season_id", "episode_id",
                "media_source", "media_id", "user_name", "save_reason", "json_object", "server_name", "item_id"}
    if required - fields:
        errors.append(f"Webhook 缺少字段：{required - fields}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"宿主源码合同检查通过：{host}")
    print(f"识别、搜索、服务发现参数及 Webhook 字段匹配；{len(set(used_imports))} 个宿主导入路径存在。")
    print("此检查不代表宿主运行时导入、插件安装、页面渲染或豆瓣真实写入已经验证。")


if __name__ == "__main__":
    main()
