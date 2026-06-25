#!/usr/bin/env python3
"""
源码仓库 License 检查脚本

检测逻辑（优先级由高到低）：
  1. 语言 manifest 文件中的 license 字段（机器可读，最权威）
  2. LICENSE / COPYING 文件内容关键词匹配

分类映射（参考 DESIGN.md 4.3 节）：
  permissive     → MIT, Apache-2.0, BSD, ISC, Unlicense 等  → 通过
  weak_copyleft  → LGPL, MPL                                → 通过，记录警告
  strong_copyleft→ GPL-2.0, GPL-3.0, AGPL                  → 通过，报告记录（分发无风险）
  no_commercial  → CC-BY-NC, BUSL, SSPL                    → 阻断
  unknown        → 无法识别                                  → 阻断，需人工确认
  unlicensed     → 无任何许可证声明                          → 阻断

用法：
  python3 check_license.py <source_dir>
  python3 check_license.py <source_dir> -o result.json
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET


# ── License 关键词匹配规则（按特异性从高到低排列）──────────────────────────
LICENSE_PATTERNS = [
    # AGPL（必须在 GPL 之前，因为文本包含 "GENERAL PUBLIC LICENSE"）
    (r"GNU AFFERO GENERAL PUBLIC LICENSE",                        "AGPL-3.0"),
    # GPL（Version 可能在同一行或下一行，用 [\s\S]{0,80} 跨行匹配）
    (r"GNU GENERAL PUBLIC LICENSE[\s\S]{0,80}Version 3",          "GPL-3.0"),
    (r"GNU GENERAL PUBLIC LICENSE[\s\S]{0,80}Version 2",          "GPL-2.0"),
    # LGPL
    (r"GNU LESSER GENERAL PUBLIC LICENSE[\s\S]{0,80}Version 3",   "LGPL-3.0"),
    (r"GNU LESSER GENERAL PUBLIC LICENSE[\s\S]{0,80}Version 2\.1","LGPL-2.1"),
    (r"GNU LESSER GENERAL PUBLIC LICENSE[\s\S]{0,80}Version 2",   "LGPL-2.0"),
    # MPL
    (r"Mozilla Public License[^\n]*Version 2\.0",                 "MPL-2.0"),
    (r"Mozilla Public License[^\n]*Version 1\.1",                 "MPL-1.1"),
    # Apache（"Apache License" 和 "Version 2.0" 在标准文件中跨行，用 [\s\S]{0,100}）
    (r"Apache License[\s\S]{0,100}Version 2\.0",                  "Apache-2.0"),
    (r"Apache License[\s\S]{0,100}Version 1\.1",                  "Apache-1.1"),
    # MIT（用最具特异性的短语）
    (r"Permission is hereby granted, free of charge",             "MIT"),
    # BSD-3（三条款，有 "neither the name" 限制）
    (r"Redistribution and use in source and binary forms.{0,200}neither the name",
                                                                   "BSD-3-Clause"),
    # BSD-2（两条款，无广告条款）
    (r"Redistribution and use in source and binary forms",        "BSD-2-Clause"),
    # ISC
    (r"Permission to use, copy, modify, and(?:/or)? distribute",  "ISC"),
    # Unlicense
    (r"This is free and unencumbered software released into the public domain",
                                                                   "Unlicense"),
    # CC 系列（先匹配 NC 变体）
    (r"Creative Commons[^\n]*NonCommercial",                      "CC-BY-NC"),
    (r"Creative Commons[^\n]*Attribution[^\n]*ShareAlike",        "CC-BY-SA"),
    (r"Creative Commons[^\n]*Attribution",                        "CC-BY"),
    # 商用限制许可证
    (r"Business Source License",                                   "BUSL-1.1"),
    (r"Server Side Public License",                                "SSPL-1.0"),
    # Ruby 特有
    (r"Ruby(?:'s)? License",                                       "Ruby"),
    # Artistic
    (r"The Artistic License 2\.0",                                 "Artistic-2.0"),
    (r"Artistic License",                                          "Artistic-1.0"),
    # EUPL
    (r"European Union Public Licen[cs]e",                          "EUPL-1.2"),
]

# ── License → 分类映射 ────────────────────────────────────────────────────
PERMISSIVE = {
    "MIT", "Apache-2.0", "Apache-1.1",
    "BSD-2-Clause", "BSD-3-Clause", "BSD-4-Clause",
    "ISC", "Unlicense", "CC0-1.0",
    "Artistic-2.0",   # Ruby gems 常用
    "Ruby",           # Ruby 自身的许可证，宽松
    "WTFPL", "Zlib", "libpng",
}
WEAK_COPYLEFT = {
    "LGPL-2.0", "LGPL-2.1", "LGPL-3.0",
    "MPL-1.1", "MPL-2.0",
    "CDDL-1.0", "EPL-1.0", "EPL-2.0",
    "EUPL-1.2",
    "Artistic-1.0",
    "CC-BY", "CC-BY-SA",   # 内容类许可，不传染代码
}
STRONG_COPYLEFT = {
    "GPL-2.0", "GPL-3.0", "AGPL-3.0",
}
NO_COMMERCIAL = {
    "CC-BY-NC", "CC-BY-NC-SA", "CC-BY-NC-ND",
    "BUSL-1.1", "SSPL-1.0",
}


def classify(spdx_id: str) -> str:
    """将 SPDX 标识符映射为分类。"""
    # 处理 "or-later" / "only" 后缀（如 GPL-3.0-only → GPL-3.0）
    normalized = re.sub(r"-(only|or-later|or-AND-later)$", "", spdx_id)
    # 去掉 WITH xxx 异常条款（如 GPL-2.0 WITH Classpath-exception → 按 GPL-2.0 判断）
    normalized = re.sub(r"\s+WITH\s+\S+", "", normalized)
    if normalized in PERMISSIVE:
        return "permissive"
    if normalized in WEAK_COPYLEFT:
        return "weak_copyleft"
    if normalized in STRONG_COPYLEFT:
        return "strong_copyleft"
    if normalized in NO_COMMERCIAL:
        return "no_commercial"
    return "unknown"


def parse_spdx_expression(expr: str):
    """
    简单解析 SPDX 表达式，返回所有涉及的 license ID 列表。
    例：'MIT OR Apache-2.0' → ['MIT', 'Apache-2.0']
         'GPL-2.0 AND Classpath-exception-2.0' → ['GPL-2.0']（仅取主许可证）
    """
    # 去掉括号
    expr = expr.strip().strip("()")
    # 拆 OR / AND
    parts = re.split(r"\s+(?:OR|AND)\s+", expr, flags=re.IGNORECASE)
    results = []
    for p in parts:
        p = p.strip().strip("()")
        # 跳过 exception 标识符（通常含 "exception"）
        if "exception" in p.lower():
            continue
        if p:
            results.append(p)
    return results or [expr]


def normalize_license_ids(values: list[str]) -> list[str]:
    """将 manifest 中的原始 license 值规范化为已知 SPDX ID。"""
    normalized_ids = []
    for value in values:
        text = re.sub(r"^SPDX-License-Identifier:\s*", "", value.strip(), flags=re.IGNORECASE)
        if not text:
            continue
        if classify(text) != "unknown":
            if text not in normalized_ids:
                normalized_ids.append(text)
            continue
        for pattern, spdx_id in LICENSE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL) and spdx_id not in normalized_ids:
                normalized_ids.append(spdx_id)
    return normalized_ids


# ── 各语言 manifest 读取 ──────────────────────────────────────────────────
def read_pyproject_toml(path: str) -> str | None:
    """读取 pyproject.toml 中的 license 字段。"""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            # 手动解析（仅处理简单情况）
            return _parse_toml_license_simple(path)

    with open(path, "rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    # PEP 621：license 可以是 {text="MIT"} 或 {file="LICENSE-Apache.txt"}
    lic = project.get("license")
    if isinstance(lic, dict):
        if lic.get("text") or lic.get("expression"):
            return lic.get("text") or lic.get("expression")
        # {file: "..."} 形式：读文件内容后用关键词识别
        lic_file = lic.get("file")
        if lic_file:
            lic_path = os.path.join(os.path.dirname(path), lic_file)
            if os.path.isfile(lic_path):
                with open(lic_path, encoding="utf-8", errors="ignore") as lf:
                    content = lf.read()
                snippet = content[:3000]
                for pattern, spdx_id in LICENSE_PATTERNS:
                    if re.search(pattern, snippet, re.IGNORECASE | re.DOTALL):
                        return spdx_id
        return None
    if isinstance(lic, str):
        return lic

    # Poetry 格式
    tool_poetry = data.get("tool", {}).get("poetry", {})
    return tool_poetry.get("license")


def _parse_toml_license_simple(path: str) -> str | None:
    """不依赖 tomllib 的简单 license 字段提取。"""
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    # 匹配 license = "MIT" 或 license = { text = "MIT" }
    m = re.search(r'license\s*=\s*["\']([^"\']+)["\']', content)
    if m:
        return m.group(1)
    m = re.search(r'license\s*=\s*\{[^}]*(?:text|expression)\s*=\s*["\']([^"\']+)["\']', content)
    return m.group(1) if m else None


def read_setup_cfg(path: str) -> str | None:
    import configparser
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path, encoding="utf-8")
        return cfg.get("metadata", "license", fallback=None)
    except Exception:
        return None


def read_setup_py(path: str) -> str | None:
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    m = re.search(r"license\s*=\s*['\"]([^'\"]+)['\"]", content)
    return m.group(1) if m else None


def read_cargo_toml(path: str) -> str | None:
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    m = re.search(r'license\s*=\s*["\']([^"\']+)["\']', content)
    return m.group(1) if m else None


def read_package_json(path: str) -> str | None:
    with open(path, encoding="utf-8", errors="ignore") as f:
        data = json.load(f)
    lic = data.get("license")
    if isinstance(lic, str):
        return lic
    if isinstance(lic, dict):
        return lic.get("type")
    return None


def read_pom_xml(path: str) -> str | None:
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns = re.match(r"\{[^}]+\}", root.tag)
        prefix = ns.group(0) if ns else ""
        licenses = root.find(f"{prefix}licenses")
        if licenses is not None:
            first = licenses.find(f"{prefix}license")
            if first is not None:
                name = first.find(f"{prefix}name")
                if name is not None and name.text:
                    return name.text.strip()
    except Exception:
        pass
    return None


def read_gemspec(path: str) -> str | None:
    """读取 .gemspec 文件中的 license 字段。"""
    with open(path, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    # spec.license = "MIT" 或 spec.licenses = ["MIT", "Apache-2.0"]
    m = re.search(r'\.licenses?\s*=\s*\[?["\']([^"\']+)["\']', content)
    return m.group(1) if m else None


def read_gemfile_or_gemspec(source_dir: str) -> str | None:
    """Ruby 项目：优先找 .gemspec，其次找 Gemfile 中的线索。"""
    for fname in os.listdir(source_dir):
        if fname.endswith(".gemspec"):
            result = read_gemspec(os.path.join(source_dir, fname))
            if result:
                return result
    return None


def detect_license_from_manifest(source_dir: str) -> tuple[str | None, str]:
    """
    从 manifest 文件读取 license。
    返回 (license_str, source_file)。
    """
    checks = [
        ("pyproject.toml",  read_pyproject_toml),
        ("setup.cfg",       read_setup_cfg),
        ("setup.py",        read_setup_py),
        ("Cargo.toml",      read_cargo_toml),
        ("package.json",    read_package_json),
        ("pom.xml",         read_pom_xml),
    ]
    for filename, reader in checks:
        fpath = os.path.join(source_dir, filename)
        if os.path.isfile(fpath):
            result = reader(fpath)
            if result:
                return result.strip(), filename

    # Ruby：.gemspec 文件名不固定
    gemspec_result = read_gemfile_or_gemspec(source_dir)
    if gemspec_result:
        return gemspec_result.strip(), "*.gemspec"

    return None, ""


# ── LICENSE 文件识别 ──────────────────────────────────────────────────────
LICENSE_FILENAMES = [
    "LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst",
    "LICENCE", "LICENCE.txt", "LICENCE.md",
    "COPYING", "COPYING.txt", "COPYING.md",
    "COPY", "COPYRIGHT",
]


def find_license_files(source_dir: str) -> list[str]:
    """返回所有可能的 license 文件路径（支持 LICENSE-MIT 等变体）。"""
    found = []
    try:
        entries = os.listdir(source_dir)
    except OSError:
        return found
    for entry in entries:
        upper = entry.upper()
        # 精确匹配
        if entry in LICENSE_FILENAMES:
            found.append(os.path.join(source_dir, entry))
            continue
        # 变体匹配：LICENSE-MIT / LICENSE.MIT / LICENCE_APACHE 等
        if re.match(r"(LICENSE|LICENCE|COPYING)[_.\-]", upper):
            found.append(os.path.join(source_dir, entry))
    return found


def match_license_from_content(content: str) -> str | None:
    """用关键词规则从文件内容识别 license。"""
    # 取前 3000 个字符（避免超长文件）
    snippet = content[:3000]
    for pattern, spdx_id in LICENSE_PATTERNS:
        if re.search(pattern, snippet, re.IGNORECASE | re.DOTALL):
            return spdx_id
    return None


def detect_license_from_files(source_dir: str) -> tuple[list[str], str]:
    """
    从 LICENSE 文件识别 license。
    返回 (spdx_ids_list, source_description)。
    """
    files = find_license_files(source_dir)
    if not files:
        return [], ""

    found_ids = []
    file_names = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            spdx_id = match_license_from_content(content)
            if spdx_id and spdx_id not in found_ids:
                found_ids.append(spdx_id)
            file_names.append(os.path.basename(fpath))
        except OSError:
            continue

    return found_ids, ", ".join(file_names)


# ── 主入口 ────────────────────────────────────────────────────────────────
def check_license(source_dir: str) -> dict:
    """
    检查 source_dir 的 license，返回结构化结果。

    结果字段：
      license_ids        : list[str]   SPDX 标识符列表（可能多个）
      category           : str         最严格分类
      source             : str         信息来源文件
      blocking           : bool        直接规则阻断（仅明确不合规时为 True）
      needs_ai_fallback  : bool        是否必须触发 AI 兜底判断
      final_blocking     : bool        当前规则阶段的最终阻断状态
      message            : str         人读信息
      all_categories     : list[str]   所有分类（多许可证时）
    """
    # ── Step 1：manifest ──
    manifest_lic, manifest_src = detect_license_from_manifest(source_dir)
    manifest_ids = normalize_license_ids(parse_spdx_expression(manifest_lic)) if manifest_lic else []

    # ── Step 2：LICENSE 文件 ──
    file_ids, file_source = detect_license_from_files(source_dir)

    spdx_ids = []
    source_parts = []
    for src in (manifest_src if manifest_ids else "", file_source if file_ids else ""):
        if src and src not in source_parts:
            source_parts.append(src)
    source = ", ".join(source_parts)

    for lid in manifest_ids + file_ids:
        if lid not in spdx_ids:
            spdx_ids.append(lid)

    # ── Step 3：有文件但未能识别 ──
    if not spdx_ids and (manifest_src or file_source):
        unknown_source = ", ".join(part for part in [manifest_src, file_source] if part)
        return {
            "license_ids": [],
            "category": "unknown",
            "source": unknown_source,
            "blocking": False,
            "needs_ai_fallback": True,
            "final_blocking": False,
            "message": f"找到 {unknown_source} 但规则脚本无法识别许可证类型，需要 AI 兜底判断后再决定是否继续",
            "all_categories": ["unknown"],
        }

    # ── Step 4：无任何许可证信息 ──
    if not spdx_ids:
        return {
            "license_ids": [],
            "category": "unlicensed",
            "source": source or "none",
            "blocking": False,
            "needs_ai_fallback": True,
            "final_blocking": False,
            "message": "未找到任何 License 声明（无 manifest 字段，无 LICENSE 文件），需要 AI 兜底判断后再决定是否继续",
            "all_categories": ["unlicensed"],
        }

    # ── Step 5：分类 & 决策 ──
    categories = [classify(lid) for lid in spdx_ids]
    # 多许可证取最严格分类（优先级：no_commercial > unknown > strong_copyleft > weak_copyleft > permissive）
    PRIORITY = ["no_commercial", "unknown", "strong_copyleft", "weak_copyleft", "permissive"]
    worst = min(categories, key=lambda c: PRIORITY.index(c) if c in PRIORITY else 99)

    blocking = worst == "no_commercial"
    needs_ai_fallback = worst == "unknown"

    license_str = " / ".join(spdx_ids)

    if worst == "no_commercial":
        msg = f"{license_str} 限制商用，不符合 openEuler 开源要求，阻断"
    elif worst == "unknown":
        msg = f"{license_str} 规则脚本无法识别，需要 AI 兜底判断并给出可解释结论后方可继续"
    elif worst == "strong_copyleft":
        msg = f"{license_str} 为强 Copyleft 许可证，openEuler 可分发，spec License 字段需正确填写"
    elif worst == "weak_copyleft":
        msg = f"{license_str} 为弱 Copyleft 许可证，动态链接场景兼容，通过"
    else:
        msg = f"{license_str} 为宽松许可证，直接通过"

    return {
        "license_ids": spdx_ids,
        "category": worst,
        "source": source,
        "blocking": blocking,
        "needs_ai_fallback": needs_ai_fallback,
        "final_blocking": blocking,
        "message": msg,
        "all_categories": categories,
    }


def print_report(result: dict, pkg_name: str = "") -> None:
    label = f"[{pkg_name}] " if pkg_name else ""
    if result["blocking"]:
        status = "❌ 规则阻断"
    elif result.get("needs_ai_fallback"):
        status = "🤖 需要 AI 兜底判断"
    elif result["category"] in ("strong_copyleft", "weak_copyleft"):
        status = "⚠️  警告"
    else:
        status = "✅ 通过"
    print(f"\nLicense 检查报告 {label}")
    print("─" * 50)
    print(f"状态      : {status}")
    print(f"License   : {', '.join(result['license_ids']) or '未识别'}")
    print(f"分类      : {result['category']}")
    print(f"来源      : {result['source']}")
    print(f"AI 兜底判断: {'需要' if result.get('needs_ai_fallback') else '不需要'}")
    print(f"说明      : {result['message']}")
    print("─" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="检查源码仓库的 License")
    parser.add_argument("source_dir", help="源码目录路径")
    parser.add_argument("-o", "--output", help="输出 JSON 文件路径")
    parser.add_argument("--pkg", default="", help="包名（仅用于报告显示）")
    args = parser.parse_args()

    if not os.path.isdir(args.source_dir):
        print(f"错误：目录不存在：{args.source_dir}", file=sys.stderr)
        sys.exit(1)

    result = check_license(args.source_dir)
    print_report(result, args.pkg)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已写入：{args.output}")

    sys.exit(1 if result["blocking"] else 0)


if __name__ == "__main__":
    main()
