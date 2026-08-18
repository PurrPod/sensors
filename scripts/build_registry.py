#!/usr/bin/env python3
"""构建 sensors 的 registry.json 并同步 README.md

目录约定:
  sensors/<sensor-name>/config.json      每个 sensor 一个独立文件夹，包含 config.json 与同名 .py
  sensors/<sensor-name>/<sensor-name>.py sensor 的单个代码文件

统一字段: name / description / enabled / env / capabilities
"""
import json
import os
import sys

REGISTRY_FILE = "registry.json"
README_FILE = "README.md"
REPO_URL = "https://github.com/PurrPod/sensors"

SENSORS_DIR = "sensors"

REQUIRED_FIELDS = ("name", "description", "enabled", "env", "capabilities")


def fail(msg):
    print(f"[Error] {msg}")
    sys.exit(1)


def load_json(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"无法读取或解析 {filepath}: {e}")


def validate_entry(folder, entry, expected_name):
    """校验单个 sensor 条目"""
    if not isinstance(entry, dict):
        fail(f"[{folder}/config.json] 内容必须是一个 JSON 对象")

    for field in REQUIRED_FIELDS:
        if field not in entry:
            fail(f"[{folder}/config.json] 缺少必填字段 '{field}'")

    # 校验 1: name 必须与文件夹名一致
    name = str(entry.get("name", "")).strip()
    if not name:
        fail(f"[{folder}/config.json] 'name' 不能为空")
    if name != expected_name:
        fail(f"[{folder}/config.json] 'name' ('{name}') 必须与文件夹名 ('{expected_name}') 一致")

    # 校验 2: description 不能为空
    if not str(entry.get("description", "")).strip():
        fail(f"[{folder}/config.json] 'description' 不能为空")

    # 校验 3: enabled 必须是布尔
    if not isinstance(entry.get("enabled"), bool):
        fail(f"[{folder}/config.json] 'enabled' 必须是布尔值 (true/false)")

    # 校验 4: env 必须是对象
    if not isinstance(entry.get("env"), dict):
        fail(f"[{folder}/config.json] 'env' 必须是一个对象")

    # 校验 5: capabilities 必须是对象且包含 observe/express
    caps = entry.get("capabilities")
    if not isinstance(caps, dict):
        fail(f"[{folder}/config.json] 'capabilities' 必须是一个对象")
    for cap in ("observe", "express"):
        if cap not in caps or not isinstance(caps[cap], bool):
            fail(f"[{folder}/config.json] 'capabilities.{cap}' 必须是布尔值")

    # 校验 6: 同名 .py 代码文件必须存在
    code_file = os.path.join(folder, f"{expected_name}.py")
    if not os.path.isfile(code_file):
        fail(f"[{folder}] 缺失 sensor 代码文件: {expected_name}.py")


def normalize(entry):
    """输出为统一的注册表条目"""
    return {
        "name": entry["name"],
        "description": entry["description"],
        "enabled": entry["enabled"],
        "env": entry["env"],
        "capabilities": entry["capabilities"],
    }


def scan_sensors():
    """扫描 sensors/ 下的 sensor 文件夹"""
    entries = []
    if not os.path.isdir(SENSORS_DIR):
        return entries

    for item in sorted(os.listdir(SENSORS_DIR)):
        if item.startswith("."):
            continue
        folder = os.path.join(SENSORS_DIR, item)
        if not os.path.isdir(folder):
            continue

        config_file = os.path.join(folder, "config.json")
        if not os.path.isfile(config_file):
            continue  # 不是 sensor 文件夹，跳过

        entry = load_json(config_file)
        validate_entry(folder, entry, item)
        entries.append((item, normalize(entry)))
    return entries


def generate_markdown_table(entries):
    """生成 Markdown 格式表格"""
    lines = [
        "| 传感器名 (Install ID) | 描述 | 状态 | 能力 |",
        "| :--- | :--- | :--- | :--- |",
    ]

    if not entries:
        lines.append("| *(虚位以待)* | 期待您的收录！ | - | - |")
        return "\n".join(lines) + "\n"

    for short_id, info in sorted(entries):
        name = info["name"]
        desc = str(info["description"]).replace("|", "\\|")
        status = "✅ 启用" if info.get("enabled") else "⏸️ 停用"
        caps = info.get("capabilities", {})
        cap_list = [k for k in ("observe", "express") if caps.get(k)]
        cap_text = ", ".join(cap_list) if cap_list else "-"
        lines.append(f"| `{name}` | {desc} | {status} | {cap_text} |")

    return "\n".join(lines) + "\n"


def replace_between_tags(text, start_tag, end_tag, new_content):
    """将文本中 start_tag 与 end_tag 之间的内容替换为 new_content"""
    start_idx = text.find(start_tag)
    end_idx = text.find(end_tag)
    if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
        head = text[: start_idx + len(start_tag)]
        tail = text[end_idx:]
        return f"{head}\n{new_content}{tail}"
    return text


def update_readme(entries):
    """回写更新 README.md 中的表格"""
    if not os.path.exists(README_FILE):
        return
    with open(README_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    content = replace_between_tags(
        content, "<!-- SENSORS:START -->", "<!-- SENSORS:END -->",
        generate_markdown_table(entries),
    )

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    print(f"扫描 {SENSORS_DIR}/ 下的 sensor ...")
    entries = scan_sensors()

    # 校验: 不允许同名 sensor
    names = [name for name, _ in entries]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        fail(f"存在同名 sensor: {', '.join(duplicates)}")

    registry = {
        "version": "2.0",
        "repository": REPO_URL,
        "sensors": [info for _, info in entries],
    }

    print(f"生成 {REGISTRY_FILE} ...")
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("更新 README.md ...")
    update_readme(entries)

    print(f"构建与校验完成！共 {len(entries)} 个 sensor。")


if __name__ == "__main__":
    main()
