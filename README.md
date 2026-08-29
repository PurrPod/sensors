<h1 align="center">PurrCat Sensors Market</h1>

<p align="center">
    专为 <a href="https://github.com/PurrPod/purrcat">PurrCat</a> 构建的传感器（Sensor）市场与注册表中心。
</p>

---

## 1. 简介

Sensor 是 PurrCat Agent 的「感官中枢」，负责**感知（observe）**外部世界的事件与**表达（express）**对外的主动动作。每个 Sensor 都是一个独立的 Python 脚本，通过标准输入输出与主进程进行 JSON 行协议通信。

* **observe**：监听外部事件（如飞书消息、RSS 更新、闹钟到点），将感知到的内容推送给主进程。
* **express**：接收主进程指令，执行对外动作（如发送飞书消息）。

---

## 2. 仓库架构设计

```text
sensors/
├── .github/workflows/   # CI/CD 自动化构建流水线
├── scripts/             # 注册表构建与校验脚本
├── registry.json        # 全局注册表 (由 Action 自动生成)
├── README.md            # 说明文档与 Sensor 列表 (由 Action 自动更新)
│
└── sensors/             # 官方 sensor (源码直接在本仓库维护)
    └── <sensor-name>/   # 每个 sensor 一个独立文件夹
        ├── <sensor-name>.py # sensor 的单个代码文件 (与文件夹同名)
        └── config.json      # sensor 配置 (name / description / enabled / env / tool_detail / capabilities)
```

---

## 3. 已收录 Sensor 清单

*(注：本列表由自动化流水线实时生成)*

<!-- SENSORS:START -->
| 传感器名 (Install ID) | 描述 | 状态 | 能力 |
| :--- | :--- | :--- | :--- |
| `feishu-bot` | 飞书机器人传感器，通过 WebSocket 长连接监听飞书群消息并支持主动下发卡片消息。 | ✅ 启用 | observe, express |
| `rss-watcher` | RSS 订阅观察器，按固定间隔轮询多个 RSS 源，发现新更新时推送摘要到主进程。 | ⏸️ 停用 | observe |
| `system-clock` | 系统时钟传感器，定时轮询本地 cron 配置并按规则触发闹钟提醒或启动任务图。 | ✅ 启用 | observe |
| `wechat-clawbot` | 微信 iLink Bot 双向通道传感器，长轮询监听微信消息并支持将回复（文本/图片）发回微信。 | ✅ 启用 | observe, express |
<!-- SENSORS:END -->

---

## 4. 统一字段规范

每个 sensor 的 `config.json` 必须包含以下字段：

```json
{
  "name": "feishu-bot",
  "description": "飞书机器人传感器，通过 WebSocket 长连接监听飞书群消息并支持主动下发卡片消息。",
  "enabled": true,
  "env": {
    "FEISHU_APP_ID": "cli_xxx",
    "FEISHU_APP_SECRET": "xxx",
    "FEISHU_CHAT_ID": "oc_xxx"
  },
  "tool_detail": false,
  "capabilities": {
    "observe": true,
    "express": true
  }
}
```

### 字段解析

* **`name`** (必填): 安装标识，必须与文件夹名、代码文件名（去掉 `.py`）完全一致。
* **`description`** (必填): 一句话描述该 Sensor 的用途。
* **`enabled`** (必填): 是否启用该 Sensor。
* **`env`** (必填): 环境变量键值对，注入到 Sensor 运行时。
* **`tool_detail`** (必填): 是否在消息中携带工具调用明细，默认为 `false`。
* **`capabilities`** (必填): 能力声明，包含 `observe` (布尔) 与 `express` (布尔) 两个子字段。

### 命名一致性要求

**文件夹名 = config.json 中的 `name` 字段 = 代码文件名（去掉 `.py`）**

三者必须严格一致，CI 构建时会自动校验。例如 sensor `feishu-bot`：

```
sensors/feishu-bot/
├── feishu-bot.py      # ✅ 与文件夹同名
└── config.json        # ✅ name: "feishu-bot"
```

---

## 5. 收录方式

在 `sensors/` 目录下新建 `<sensor-name>/` 文件夹，包含：

1. `<sensor-name>.py`：Sensor 的单文件 Python 脚本（文件名必须与文件夹名一致）。
2. `config.json`：包含上述全部必填字段的配置文件。

提交 Pull Request。CI 会自动校验：

* 文件夹名、`config.json` 中的 `name` 字段、代码文件名三者一致；
* `config.json` 可解析且包含全部 5 个必填字段；
* `capabilities` 包含 `observe` 与 `express` 两个布尔子字段。

PR 审核通过并合并后，流水线将自动把所有 sensor 合并为全局注册表 `registry.json`，并重写本文档的 sensor 清单。
