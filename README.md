# TickTest MCP Server

<!-- mcp-name: io.github.shakagold/ticktest-mcp -->

让 AI Agent（Claude Code、Cursor、Windsurf 等）通过 MCP 协议直接调用 TickTest A 股量化回测 API。

## 安装

```bash
pip install ticktest-mcp
```

## 配置

### Claude Code

在 `.claude/mcp.json` 或项目根目录 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "ticktest": {
      "command": "python",
      "args": ["-m", "ticktest_mcp"],
      "env": {
        "TICKTEST_API_URL": "https://api.ticktest.cn",
        "TICKTEST_API_KEY": "tt_your_api_key_here"
      }
    }
  }
}
```

### Cursor / Windsurf

在 MCP 配置中添加：

```json
{
  "mcpServers": {
    "ticktest": {
      "command": "ticktest-mcp",
      "env": {
        "TICKTEST_API_URL": "https://api.ticktest.cn",
        "TICKTEST_API_KEY": "tt_your_api_key_here"
      }
    }
  }
}
```

## 获取 API Key

无需离开对话，Agent 会帮你完成注册：

1. Agent 询问你的邮箱
2. Agent 调用 `send-code` → 你收到 6 位验证码
3. 告诉 Agent 验证码 → 注册成功，拿到 API Key + 500 信用点
4. Agent 配置 Key，开始回测

## Tools

| Tool | 认证 | 说明 |
|:-----|:-----|:-----|
| `get_capabilities` | 公开 | 获取策略、因子、定价、注册流程 |
| `health_check` | 公开 | 检查 API 服务状态 |
| `search_symbols` | 公开 | 搜索 A 股股票代码 |
| `validate_strategy` | 需 Key | 免费验证策略参数 |
| `run_backtest` | 需 Key | 运行回测，¥0.50/次 |
| `create_payment` | 需 Key | 生成三卡支付页：¥0.50 单次 / ¥1 体验卡 / ¥19 月卡 |

## 快速体验

海龟交易策略**永久免费**，无需付费：

```
Agent: 帮我回测中际旭创，海龟交易
→ 自动搜索 sz300308 → 回测 → 返回收益率/夏普/回撤/胜率
```

## 定价

- 单次回测：¥0.50（25 信用点）
- 畅测体验卡：¥1（24 小时回测不限次，新人限购 1 次）
- 畅测月卡：¥19（30 天回测不限次，真·不限次仅并发≤2）
- 注册送 500 信用点（≈ 20 次）
- 海龟交易：永久免费

## 环境变量

| 变量 | 必填 | 说明 |
|:-----|:-----|:-----|
| `TICKTEST_API_URL` | 否 | API 地址，默认 `https://api.ticktest.cn` |
| `TICKTEST_API_KEY` | 否 | API Key，不配则只能调公开 Tool |
| `TICKTEST_BACKTEST_TIMEOUT` | 否 | 回测超时秒数，默认 90 |

## License

MIT
