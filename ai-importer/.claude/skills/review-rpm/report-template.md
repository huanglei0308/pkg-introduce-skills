# 软件包引入审核报告模板

每次完成归档后生成一份 `reports/<pkgname>_introduction_report.md`，严格遵循以下格式。
中间阶段的单阶段报告（spec/lint/final）仍使用原有 `review_<pkgname>_<stage>.md` 格式，本模板为最终汇总报告。

---

## 文件命名规则

| 用途 | 文件名 |
|------|--------|
| spec 阶段审查（中间产物） | `review_<pkgname>_spec.md` |
| lint 阶段审查（中间产物） | `review_<pkgname>_lint.md` |
| final 阶段审查（中间产物） | `review_<pkgname>_final.md` |
| **最终汇总审核报告** | `<pkgname>_introduction_report.md` |

---

## 最终汇总报告格式

```markdown
# 软件包引入审核报告：<pkgname>

> 本报告由自动化流程生成，供人工审核决策使用。
> 生成时间：<YYYY-MM-DD>

---

## 一、基本信息

| 项目 | 内容 |
|------|------|
| 包名 | <pkgname> |
| 版本 | <version> |
| 上游地址 | <upstream_url> |
| 上游最后更新 | <YYYY-MM-DD>（N 天前） |
| 语言类型 | <lang>（说明，如 header-only / 共享库 / 可执行文件） |
| License | <spdx_id> |
| 包类型 | <noarch / arch>，说明原因 |
| 归档仓库 | <repo_url> |

---

## 二、引入必要性

| 检查项 | 结果 |
|--------|------|
| openEuler 社区源（OS / EPOL / everything / update）是否已有 | 有 / **无** |
| 用户 RPM 仓库是否已有 | 有 / **无** |
| 引入决策 | introduce_new / upgrade_user_repo / reuse_* |

<details>
<summary>existing-check 原始输出</summary>

```json
{ <existing_check_<pkgname>.json 的关键字段> }
```

</details>

---

## 三、合规检查

### 3.1 上游仓库合规

| 项目 | 结果 |
|------|------|
| 平台 | <GitHub / GitLab / ...> |
| 上游最后活跃 | <YYYY-MM-DD>，N 天前 |
| 阻断 | 是 / **否** |

<details>
<summary>repo-check 原始输出</summary>

```json
{ <repo_check_<pkgname>.json 全文> }
```

</details>

### 3.2 License 合规

| 项目 | 结果 |
|------|------|
| 识别到的 License | <spdx_id> |
| 证据文件 | <LICENSE / COPYING / ...> |
| 分类 | permissive / weak_copyleft / strong_copyleft / unknown |
| 阻断 | 是 / **否** |
| 需要 AI 兜底 | 是（说明结论） / **否** |

**结论**：<一句话说明合规情况和风险>

<details>
<summary>license-check 原始输出</summary>

```json
{ <license_check_<pkgname>.json 全文> }
```

</details>

---

## 四、模块结构与依赖关系

### 4.1 模块结构

本包为**单模块 / 多模块**，共生成 N 个二进制包 + 1 个 SRPM：

| 子包 | 说明 | 主要内容 |
|------|------|----------|
| `<pkgname>` | 主包 | <主要文件，如 .so 文件、可执行文件、license> |
| `<pkgname>-devel` | 开发包 | <头文件、cmake 文件、pkgconfig 文件等> |
| `<pkgname>-<submodule>` | 子模块包（若有） | <说明> |
| `<pkgname>-<version>.src.rpm` | 源码包 | spec + 上游 tarball |

> 多模块说明（若有）：<说明为何拆分，各子包的功能边界>

### 4.2 运行时依赖（Requires）

**<pkgname> 主包**：

```
<rpm -qp --requires 输出>
```

**<pkgname>-devel**：

```
<rpm -qp --requires 输出>
```

> 若有其他子包，逐一列出。

### 4.3 编译期依赖（BuildRequires）

```
<spec 中的 BuildRequires 列表>
```

> 若 BuildRequires 中有非社区源依赖，说明来源（用户 RPM 仓库 / 本次同批引入）。

### 4.4 对外提供（Provides）

```
<rpm -qp --provides 输出>
```

### 4.5 依赖分析结论

- <是否引入新的依赖链，依赖链深度>
- <是否存在循环依赖风险>
- <用户安装时会自动拉取哪些包>

---

## 五、Spec 文件

### 5.1 最终 Spec 内容

```spec
<spec 文件全文>
```

### 5.2 Spec 审查历史（共 N 轮）

#### 第 1 轮：<PASS / WARN / BLOCK>（E=N，W=N，I=N）

| 级别 | 规则 | 位置 | 问题 | 后续状态 |
|------|------|------|------|----------|
| E | <rule_id> | <location> | <问题描述> | ✓ 已修复 / ✗ 未修复 |
| W | <rule_id> | <location> | <问题描述> | ✓ 已修复 / — 忽略 |

> 若第 1 轮即 PASS，注明"首轮通过，无需修复"。

#### 第 N 轮：<PASS>（E=0，W=N，I=N）

<说明所有 E 级问题的修复情况，以及剩余 I 级提示>

<details>
<summary>spec 审查原始 JSON（最终轮）</summary>

```json
{ <review_<pkgname>_spec.json 全文> }
```

</details>

---

## 六、构建结果

### 6.1 构建产物

| 产物 | 大小 | 架构 |
|------|------|------|
| `<pkgname>-<version>-1.noarch.rpm` | N KB/MB | noarch / x86_64 |
| `<pkgname>-devel-<version>-1.noarch.rpm` | N KB/MB | noarch / x86_64 |
| `<pkgname>-<version>-1.src.rpm` | N MB | src |

构建环境：容器 `oe-build-env`（openEuler），`rpmbuild -ba`

### 6.2 安装文件清单

**<pkgname> 主包**
```
<rpm -qp --list 输出>
```

**<pkgname>-devel**
```
<rpm -qp --list 输出（头文件过多时截取前几行并注明总数）>
```

---

## 七、rpmlint 检查

### 7.1 原始输出

```
<rpmlint 原始输出全文>
```

### 7.2 Critic 裁决：<PASS / WARN / BLOCK>（E=N，W=N，I=N）

<一句话说明核心结论，例如"rpmlint 报告的 N 个 E 全部为环境误报，无真实 E 级问题">

**W 级逐条分析**

| # | rpmlint 输出 | 性质 | 处置结论 |
|---|-------------|------|----------|
| 1 | `<原始输出>` | <误报 / 风格建议 / 需修复> | <处置说明> |

**I 级（过滤）**

| # | rpmlint 输出 | 过滤依据 |
|---|-------------|---------|
| 1 | `<原始输出>` | 规则 §N：<说明> |

> 若裁决为 BLOCK，在此列出 E 级问题及修复要求。

<details>
<summary>lint 审查原始 JSON</summary>

```json
{ <review_<pkgname>_lint.json 全文> }
```

</details>

---

## 八、归档完整性检查

### 8.1 Critic 裁决：<PASS / WARN / BLOCK>

| 规则 | 级别 | 检查项 | 证据 | 结果 |
|------|------|--------|------|------|
| AR-01 | E | `dist/` 存在 binary RPM | `<文件名>`（N KB） | ✓ / ✗ PASS / FAIL |
| AR-02 | E | `dist/` 存在 SRPM | `<文件名>`（N MB） | ✓ / ✗ |
| AR-03 | E | 包目录存在 spec | `<pkgname>/<pkgname>.spec` 存在 | ✓ / ✗ |
| AR-04 | W | RPM 版本与 spec 版本一致 | RPM `<version>` = spec `Version: <version>` | ✓ / ✗ |
| AR-05 | W | repodata 已更新 | `dist/repodata/` 于 <时间> 更新，N 包录入 primary.xml | ✓ / ✗ |

### 8.2 CI 门禁

| 检查项 | 结果 |
|--------|------|
| 运行时依赖（repoclosure） | ✓ 通过 / ✗ 失败（原因） |
| 编译期依赖（dnf builddep） | ✓ 通过 / ✗ 失败（原因） |

<details>
<summary>final 审查原始 JSON</summary>

```json
{ <review_<pkgname>_final.json 全文> }
```

</details>

---

## 九、遗留问题

| # | 问题 | 级别 | 说明 |
|---|------|------|------|
| 1 | <问题描述> | I / W | <详细说明及后续处理建议> |

> 若无遗留问题，写"无"。

---

## 十、各阶段结论汇总

| 阶段 | 裁决 | 说明 |
|------|------|------|
| 上游合规检查 | ✅ PASS / ❌ BLOCK | <简短说明> |
| License 合规检查 | ✅ PASS / ❌ BLOCK | <简短说明> |
| 重复引入检查 | ✅ introduce_new / ⚠️ upgrade | <简短说明> |
| Spec 审查（第 1 轮） | ✅ PASS / ❌ BLOCK | <问题数量> |
| Spec 审查（第 N 轮） | ✅ PASS | 所有问题修复 |
| rpmlint 检查 | ✅ PASS / ⚠️ WARN | <简短说明> |
| 归档完整性检查 | ✅ PASS / ❌ BLOCK | <简短说明> |
| CI 门禁 | ✅ PASS / ❌ BLOCK | <简短说明> |

**综合建议：可以引入 / 建议暂缓 / 建议拒绝。**

<一段话说明综合建议的理由，包括风险点和注意事项>

---

*本报告由 pkg-introduce / review-rpm / archive-rpm-sources 自动化流程生成，最终引入决策由人工审核确认。*
```

---

## 裁决规则（中间阶段 spec/lint/final 不变）

| 裁决 | 条件 | 主流程行为 |
|------|------|-----------|
| `PASS` | 无 E 级问题 | 继续下一步 |
| `WARN` | 有 W 级问题，无 E 级 | 继续，问题记入 `import_issues.log` |
| `BLOCK` | 有 E 级问题 | Actor 修复后重新触发本阶段，最多 3 轮 |

超过 3 轮仍 `BLOCK`：升级为人工介入，写入 `import_issues.log` 并终止。

---

## 生成时机

最终汇总报告（`<pkgname>_introduction_report.md`）由 **pkg-introduce Orchestrator** 在归档步骤（Step B）完成、`publish_rpm.py` 推送成功后生成，汇总以下来源：

| 数据来源 | 对应报告章节 |
|----------|-------------|
| `repo_check_<pkgname>.json` | 三、合规检查 3.1 |
| `license_check_<pkgname>.json` | 三、合规检查 3.2 |
| `existing_check_<pkgname>.json` | 二、引入必要性 |
| `review_<pkgname>_spec.json/md` | 五、Spec 审查历史 |
| `review_<pkgname>_lint.json/md` | 七、rpmlint 检查 |
| `review_<pkgname>_final.json/md` | 八、归档完整性 |
| `rpm -qp --requires/--provides/--list` | 四、模块结构与依赖 |
| `rpmlint` 原始输出 | 七、rpmlint 原始输出 |
