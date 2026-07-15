# -*- coding: utf-8 -*-
"""
TickTest MCP Server — AI Agent 的 A 股回测入口

通过 MCP 协议暴露 TickTest 回测能力，让 Claude Code 等 AI Agent
直接调用 run_backtest / get_capabilities / health_check / validate_strategy / search_symbols。

认证模式（对标 Stripe MCP）：
    API Key 通过 TICKTEST_API_KEY 环境变量注入，不暴露在 Tool 参数中。
    公开 Tool（无需 Key）: get_capabilities / health_check / validate_strategy / search_symbols
    付费 Tool（需 Key）: run_backtest

启动方式：
    python server.py
    # 或在 Claude Code 的 mcpServers 配置中：
    # "ticktest": {
    #   "command": "python",
    #   "args": ["-m", "09-MCP-Server.server"],
    #   "cwd": "/path/to/TickTest",
    #   "env": { "TICKTEST_API_KEY": "tk_xxx", "TICKTEST_API_URL": "https://api-hk.ticktest.cn" }
    # }

参考：
    - Stripe MCP: github.com/stripe/ai（Key 走环境变量）
    - GitHub MCP: github.com/modelcontextprotocol（GITHUB_TOKEN 走环境变量）
    - MCP 认证调研: [[2026-07-08-MCP认证支付调研报告]]

变更日志：
    v0.3.1 (2026-07-13): Phase 4.1 — 402响应新增 payment_proof_format 字段，陌生Agent一次走通AI收闭环
    v0.3.0 (2026-07-13): Phase 4 — 新增 create_payment Tool，双支付码（单次¥0.50+套餐¥29），双通道提示
    v0.2.0 (2026-07-12): Phase 3 — 新增 validate_strategy / search_symbols Tool，完善 Tool 描述联动
    v0.1.0 (2026-07-08): Phase 1+2 — 初始 MCP Server，run_backtest / get_capabilities / health_check
"""

import os
import sys
import re
import json
import time
import logging
import traceback
from typing import Optional

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── 配置 ──────────────────────────────────────────────

API_URL = os.environ.get("TICKTEST_API_URL", "https://api-hk.ticktest.cn").rstrip("/")
API_KEY = os.environ.get("TICKTEST_API_KEY", "")
HAS_AUTH = bool(API_KEY)

# 回测超时（秒）
BACKTEST_TIMEOUT = float(os.environ.get("TICKTEST_BACKTEST_TIMEOUT", "90"))
# 常规请求超时（秒）
DEFAULT_TIMEOUT = 30.0
# 最大重试次数
MAX_RETRIES = 3
# 指数退避基础延迟（秒）
RETRY_BASE_DELAY = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ticktest-mcp] %(levelname)s: %(message)s",
    stream=sys.stderr,  # MCP stdio 用 stdout 通信，日志走 stderr
)
logger = logging.getLogger("ticktest-mcp")

# ── 校验正则 ──────────────────────────────────────────

# 股票代码：sz/sh/bj + 6位数字
SYMBOL_RE = re.compile(r"^(sz|sh|bj)\d{6}$")
# 日期格式：YYYY-MM-DD
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 支持的策略家族
KNOWN_STRATEGY_FAMILIES = [
    "SINGLE_MA",
    "MA_CROSSOVER",
    "TURTLE_TRADING",
]


def validate_symbol(symbol: str) -> Optional[str]:
    """校验股票代码格式，返回错误信息或 None"""
    if not symbol or not isinstance(symbol, str):
        return "股票代码不能为空，格式：sz/sh/bj + 6位数字。示例：sz300308（中际旭创）"
    if not SYMBOL_RE.match(symbol):
        return f"股票代码 '{symbol}' 格式无效，需为 sz/sh/bj + 6位数字。示例：sz300308（中际旭创）、bj430047（诺思兰德）"
    return None


def validate_date(date_str: str, field_name: str = "日期") -> Optional[str]:
    """校检日期格式，返回错误信息或 None"""
    if not date_str or not isinstance(date_str, str):
        return f"{field_name}不能为空，格式 YYYY-MM-DD"
    if not DATE_RE.match(date_str):
        return f"{field_name} '{date_str}' 格式无效，需为 YYYY-MM-DD"
    # 更严格的日期合法性校验
    try:
        parts = date_str.split("-")
        from datetime import date
        date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return f"{field_name} '{date_str}' 不是合法日期"
    return None


# ── HTTP 客户端 ────────────────────────────────────────

def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """创建带认证头的 HTTP 客户端"""
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return httpx.Client(base_url=API_URL, headers=headers, timeout=timeout)


def _http_request(
    method: str,
    path: str,
    json_payload: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_headers: Optional[dict] = None,
) -> httpx.Response:
    """
    带重试（指数退避）的 HTTP 请求。
    对 5xx / 网络错误重试，4xx 不重试。
    """
    last_exception: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _client(timeout=timeout) as c:
                if extra_headers:
                    c.headers.update(extra_headers)
                if method.upper() == "GET":
                    r = c.get(path)
                elif method.upper() == "POST":
                    r = c.post(path, json=json_payload)
                else:
                    raise ValueError(f"不支持的 HTTP 方法: {method}")

                # 4xx 不重试（客户端错误），直接返回
                if 400 <= r.status_code < 500:
                    return r

                # 5xx 或成功直接返回
                if r.status_code < 500:
                    return r

                # 5xx → 记录并重试
                logger.warning(
                    f"HTTP {r.status_code} on {method} {path} "
                    f"(attempt {attempt}/{MAX_RETRIES}): {r.text[:200]}"
                )
                last_exception = httpx.HTTPStatusError(
                    f"HTTP {r.status_code}", request=r.request, response=r
                )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            logger.warning(
                f"网络错误 on {method} {path} (attempt {attempt}/{MAX_RETRIES}): {e}"
            )
            last_exception = e
        except Exception as e:
            # 非网络错误不重试
            logger.error(f"非可重试错误 on {method} {path}: {e}")
            raise

        # 指数退避：1s, 2s, 4s
        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.info(f"等待 {delay:.1f}s 后重试...")
            time.sleep(delay)

    # 所有重试耗尽
    if last_exception:
        raise last_exception
    raise RuntimeError(f"HTTP {method} {path} 重试 {MAX_RETRIES} 次后仍失败")


def _safe_json(response: httpx.Response) -> dict:
    """安全解析 JSON 响应，非 JSON 时返回错误字典"""
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"API 返回非 JSON 响应 [{response.status_code}]: {response.text[:500]}")
        return {
            "success": False,
            "error": f"API 返回了非 JSON 响应 (HTTP {response.status_code})",
            "detail": response.text[:500],
            "content_type": response.headers.get("content-type", "unknown"),
        }


# ── 工具实现 ──────────────────────────────────────────

def get_capabilities() -> dict:
    """
    获取 TickTest API 的能力描述：可用策略分类、因子列表、支持的股票范围。

    优先调用 /v1/capabilities（完整 AI 自描述端点），
    若 404 则回退到 GET /（根路径已含 endpoints 和服务信息）。

    Returns dict with keys: strategies, factors, symbols, pricing, endpoints
    """
    t0 = time.time()
    logger.info("Tool: get_capabilities — 开始")

    result = None
    is_fallback = False

    # 首选：专用 capabilities 端点
    try:
        r = _http_request("GET", "/v1/capabilities")
        if r.status_code != 404:
            r.raise_for_status()
            result = _safe_json(r)
            elapsed = time.time() - t0
            logger.info(
                f"Tool: get_capabilities — 成功 (耗时 {elapsed:.2f}s, "
                f"策略 {len(result.get('strategies', {}))} 类)"
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.info("Tool: get_capabilities — /v1/capabilities 404，回退到 GET /")
        else:
            elapsed = time.time() - t0
            return _log_http_error("get_capabilities", e, elapsed)

    # 回退：用 GET / 拼装能力描述（capabilities 端点未部署时）
    if result is None:
        is_fallback = True
        logger.info("Tool: get_capabilities — 回退到 GET /")
        try:
            r = _http_request("GET", "/")
            r.raise_for_status()
            root = _safe_json(r)
            elapsed = time.time() - t0
            result = {
                "service": root.get("service", "T+0 回测 API"),
                "version": root.get("version", "unknown"),
                "endpoints": root.get("endpoints", {}),
                "strategies": root.get("strategies", ["118条策略+540别名，支持自然语言和策略名两种方式调用"]),
                "pricing": root.get("pricing", {"single_backtest": "¥0.50/次"}),
                "note": "capabilities 端点暂未部署，数据来源为 GET /。升级到 /v1/capabilities 后可获得完整策略分类/因子列表。",
            }
            logger.info(f"Tool: get_capabilities — 成功（回退模式, 耗时 {elapsed:.2f}s）")
        except httpx.HTTPStatusError as e:
            elapsed = time.time() - t0
            return _log_http_error("get_capabilities", e, elapsed)

    # ── 注入 onboarding 引导（Agent 打开 capabilities 第一眼就看到）──
    if result and isinstance(result, dict):
        result = {
            "onboarding": {
                "title": "注册即送500信用点，无需离开终端",
                "steps": ["提供邮箱", "接收验证码", "获取API Key", "开始回测"],
                "free_strategy": "海龟交易永久免费",
            },
            **result,
        }

    return result


def validate_strategy(family: str, params: Optional[dict] = None) -> dict:
    """
    校验策略参数是否合法，在调用 run_backtest 之前使用。

    通过 POST /v1/validate 验证策略族和参数组合。

    Args:
        family: 策略族名称。支持: SINGLE_MA / MA_CROSSOVER / TURTLE_TRADING
        params: 策略参数字典（可选）。
            SINGLE_MA: {ma_n: 20, buy_frequency: "weekly", sell_frequency: "daily"}
            MA_CROSSOVER: {fast: 5, slow: 20, buy_frequency: "daily", sell_frequency: "daily"}
            TURTLE_TRADING: {}（使用默认参数）

    Returns:
        {
            "success": true,
            "valid": true,
            "family": "MA_CROSSOVER",
            "normalized_params": {...},
            "warnings": [...]
        }
    """
    t0 = time.time()

    # ── 参数校验 ──
    if not family or not isinstance(family, str):
        elapsed = time.time() - t0
        logger.warning("Tool: validate_strategy — 参数校验失败（family 为空）")
        return {
            "success": False,
            "valid": False,
            "error": "策略族 family 不能为空",
            "known_families": KNOWN_STRATEGY_FAMILIES,
        }

    family_upper = family.upper().strip()
    if family_upper not in KNOWN_STRATEGY_FAMILIES:
        elapsed = time.time() - t0
        logger.warning(f"Tool: validate_strategy — 未知策略族: {family}")
        return {
            "success": False,
            "valid": False,
            "error": f"未知策略族 '{family}'，支持的策略族: {', '.join(KNOWN_STRATEGY_FAMILIES)}",
            "known_families": KNOWN_STRATEGY_FAMILIES,
        }

    # ── 构造请求 ──
    payload = {"family": family_upper}
    if params:
        payload["params"] = params

    logger.info(
        f"Tool: validate_strategy(family={family_upper}, params={params}) — 开始"
    )

    # ── 发送请求 ──
    try:
        r = _http_request("POST", "/v1/validate", json_payload=payload)

        # 401 = API Key 无效
        if r.status_code == 401:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: validate_strategy({family_upper}) — 401 API Key 无效 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "valid": False,
                "error": "API Key 无效或已过期。请检查 TICKTEST_API_KEY 环境变量是否正确。",
                "auth_error": True,
                "hint": "请在 mcpServers 配置的 env 中更新 TICKTEST_API_KEY。获取新 Key: https://ticktest.cn/keys",
            }

        # 403 = 权限不足
        if r.status_code == 403:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: validate_strategy({family_upper}) — 403 权限不足 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "valid": False,
                "error": "API Key 权限不足，可能未开通策略校验功能。",
                "auth_error": True,
                "hint": "请升级套餐或联系客服开通相关权限。",
            }

        r.raise_for_status()
        result = _safe_json(r)

        elapsed = time.time() - t0
        valid_str = "✓ 合法" if result.get("valid") else "✗ 不合法"
        logger.info(
            f"Tool: validate_strategy({family_upper}) — {valid_str} "
            f"(耗时 {elapsed:.2f}s)"
        )
        return result

    except httpx.TimeoutException:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: validate_strategy({family_upper}) — 超时 "
            f"(耗时 {elapsed:.2f}s)"
        )
        return {
            "success": False,
            "valid": False,
            "error": f"策略校验请求超时（超过 {DEFAULT_TIMEOUT}s）。请稍后重试。",
            "timeout": True,
            "elapsed_seconds": round(elapsed, 1),
        }
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t0
        return _log_http_error("validate_strategy", e, elapsed,
                               context=f"family={family_upper}")
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: validate_strategy({family_upper}) — 未预期异常 "
            f"(耗时 {elapsed:.2f}s): {e}\n{traceback.format_exc()}"
        )
        return {
            "success": False,
            "valid": False,
            "error": f"策略校验异常: {e}",
            "traceback": traceback.format_exc(),
        }


def search_symbols(query: str) -> dict:
    """
    搜索 A 股股票代码/名称，用于查找回测所需的 symbol 参数。

    通过 GET /v1/search_stock?q={query} 搜索股票。

    Args:
        query: 搜索关键词。可以是股票名称（如"中际旭创"）或代码片段（如"300308"）。

    Returns:
        {
            "success": true,
            "query": "中际旭创",
            "results": [
                {"symbol": "sz300308", "name": "中际旭创", "exchange": "sz"},
                ...
            ],
            "count": 1
        }
    """
    t0 = time.time()

    # ── 参数校验 ──
    if not query or not isinstance(query, str) or not query.strip():
        elapsed = time.time() - t0
        logger.warning("Tool: search_symbols — 参数校验失败（query 为空）")
        return {
            "success": False,
            "error": "搜索关键词不能为空。请输入股票名称（如'中际旭创'）或代码片段（如'300308'）。",
            "results": [],
            "count": 0,
        }

    query_clean = query.strip()
    logger.info(f"Tool: search_symbols(query='{query_clean}') — 开始")

    # ── 发送请求 ──
    try:
        # URL 编码关键词，防止特殊字符问题
        from urllib.parse import quote
        path = f"/v1/search_stock?q={quote(query_clean, safe='')}"
        r = _http_request("GET", path)

        # 401 = API Key 无效
        if r.status_code == 401:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: search_symbols({query_clean}) — 401 API Key 无效 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "error": "API Key 无效或已过期。请检查 TICKTEST_API_KEY 环境变量是否正确。",
                "auth_error": True,
                "results": [],
                "count": 0,
                "hint": "请在 mcpServers 配置的 env 中更新 TICKTEST_API_KEY。获取新 Key: https://ticktest.cn/keys",
            }

        # 403 = 权限不足
        if r.status_code == 403:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: search_symbols({query_clean}) — 403 权限不足 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "error": "API Key 权限不足，可能未开通股票搜索功能。",
                "auth_error": True,
                "results": [],
                "count": 0,
                "hint": "请升级套餐或联系客服开通相关权限。",
            }

        r.raise_for_status()
        result = _safe_json(r)

        elapsed = time.time() - t0
        # `/v1/search_stock` 返回 list 或 dict，兼容两种格式
        if isinstance(result, list):
            count = len(result)
        else:
            count = result.get("count", len(result.get("results", [])))
        logger.info(
            f"Tool: search_symbols(query='{query_clean}') — "
            f"找到 {count} 条结果 (耗时 {elapsed:.2f}s)"
        )

        # 确保返回结构标准化：统一为 {results: [...], count: N}
        if isinstance(result, list):
            return {"results": result, "count": len(result)}
        return result

    except httpx.TimeoutException:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: search_symbols({query_clean}) — 超时 "
            f"(耗时 {elapsed:.2f}s)"
        )
        return {
            "success": False,
            "error": f"股票搜索请求超时（超过 {DEFAULT_TIMEOUT}s）。请稍后重试。",
            "timeout": True,
            "results": [],
            "count": 0,
            "elapsed_seconds": round(elapsed, 1),
        }
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t0
        return _log_http_error("search_symbols", e, elapsed,
                               context=f"query={query_clean}")
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: search_symbols({query_clean}) — 未预期异常 "
            f"(耗时 {elapsed:.2f}s): {e}\n{traceback.format_exc()}"
        )
        return {
            "success": False,
            "error": f"股票搜索异常: {e}",
            "results": [],
            "count": 0,
            "traceback": traceback.format_exc(),
        }


def run_backtest(
    symbol: str,
    strategy: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    frequency: str = "daily",
    payment_proof: Optional[str] = None,
) -> dict:
    """
    对指定股票+策略运行回测，返回六大指标。

    Args:
        symbol: 股票代码，如 "sz000001"（平安银行）、"sh600157"（永泰能源）
        strategy: 策略描述，支持自然语言（如 "MA5上穿MA20买入"）或策略名（如 "红杏出墙"）
        start_date: 回测起始日期 YYYY-MM-DD
        end_date: 回测结束日期 YYYY-MM-DD（默认今天）
        frequency: K线周期 daily|weekly|monthly

    Returns:
        {
            "success": true,
            "symbol": "sz000001",
            "strategy": "MA5上穿MA20买入",
            "metrics": {
                "total_return": 0.1523,    # 总收益率
                "annual_return": 0.3012,   # 年化收益率
                "max_drawdown": -0.0821,   # 最大回撤
                "sharpe_ratio": 1.85,      # 夏普比率
                "win_rate": 0.62,          # 胜率
                "trade_count": 15          # 交易次数
            },
            "trades": [...],               # 交易明细（如有）
            "cost": {"credit": 0.50}
        }
    """
    t0 = time.time()

    # ── 认证检查 ──
    if not HAS_AUTH:
        elapsed = time.time() - t0
        logger.warning(
            f"Tool: run_backtest({symbol}, {strategy[:30]}...) — "
            f"拒绝（无 API Key, 耗时 {elapsed:.2f}s）"
        )
        return {
            "success": False,
            "error": "MCP Server 未配置 API Key。请设置 TICKTEST_API_KEY 环境变量。",
            "hint": "在 mcpServers 配置的 env 中添加 TICKTEST_API_KEY。注册地址: https://ticktest.cn",
        }

    # ── 参数校验 ──
    symbol_err = validate_symbol(symbol)
    if symbol_err:
        elapsed = time.time() - t0
        logger.warning(f"Tool: run_backtest — 参数校验失败（股票代码）: {symbol_err}")
        return {"success": False, "error": symbol_err, "field": "symbol"}

    if start_date:
        date_err = validate_date(start_date, "start_date")
        if date_err:
            elapsed = time.time() - t0
            logger.warning(f"Tool: run_backtest — 参数校验失败（start_date）: {date_err}")
            return {"success": False, "error": date_err, "field": "start_date"}

    if end_date:
        date_err = validate_date(end_date, "end_date")
        if date_err:
            elapsed = time.time() - t0
            logger.warning(f"Tool: run_backtest — 参数校验失败（end_date）: {date_err}")
            return {"success": False, "error": date_err, "field": "end_date"}

    if frequency not in ("daily", "weekly", "monthly"):
        elapsed = time.time() - t0
        logger.warning(f"Tool: run_backtest — 参数校验失败（frequency）: {frequency}")
        return {
            "success": False,
            "error": f"K线周期 '{frequency}' 无效，可选: daily, weekly, monthly",
            "field": "frequency",
        }

    # ── 构造请求（start_date 不传则 API 默认半年前）──
    payload = {
        "symbol": symbol,
        "strategy": strategy,
        "frequency": frequency,
    }
    if start_date:
        payload["start_date"] = start_date
    if end_date:
        payload["end_date"] = end_date

    _start_label = start_date or '半年前(默认)'
    logger.info(
        f"Tool: run_backtest({symbol}, {strategy[:40]}..., "
        f"{_start_label}~{end_date or 'today'}, {frequency}) — 开始"
    )

    # ── 发送请求（90s 超时）──
    try:
        extra_headers = {}
        if payment_proof:
            extra_headers["Payment-Proof"] = payment_proof
            logger.info(f"Tool: run_backtest — 带 Payment-Proof 重试")
        r = _http_request("POST", "/v1/backtest", json_payload=payload,
                         timeout=BACKTEST_TIMEOUT, extra_headers=extra_headers or None)

        # 402 = 余额不足
        if r.status_code == 402:
            elapsed = time.time() - t0
            payment_info = r.headers.get("Payment-Needed", "")
            # 透传 API 返回的充值信息，供 AI Agent 自动帮用户创建支付
            body = _safe_json(r)
            logger.warning(
                f"Tool: run_backtest({symbol}) — 402 余额不足 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "error": "信用点不足，需要充值后再使用回测功能。",
                "payment_needed": True,
                "payment_needed_header": payment_info,
                "credit_cost": "¥0.50/次（25信用点）",
                "recharge": body.get("recharge", {}),
                "payment_methods_available": body.get("payment_methods_available", ["credits", "alipay"]),
                "hint": "信用点不足时的充值方法：\n1.【推荐】调用本 MCP 的 create_payment Tool → 自动弹出单次¥0.50+套餐¥29双支付页面 → 支付宝扫码 → 到账后重试回测\n2.【AI收原生】若 Agent 安装了 @alipay/mcp-server-alipay → 解析 payment_needed_header → 用 Alipay MCP 支付 → 带回 payment_proof 重试\n3. 海龟交易策略永久免费，新手注册即送500信用点",
                "routes": {
                    "mcp_builtin": "调用 create_payment Tool（本 MCP Server 自带，兜底通道）",
                    "ai_pay_402": "payment_needed_header 已透传 → Alipay MCP 支付 → 带回 payment_proof 重试 → 全自动闭环",
                    "free_strategy": "海龟交易永久免费，strategy='海龟交易' 即可零成本回测",
                },
                "payment_proof_format": {
                    "description": "AI收402重试时 payment_proof 参数的编码格式（Base64URL）",
                    "json_template": {
                        "protocol": {
                            "tradeNo": "<支付宝交易号，28位数字>",
                            "paymentProof": "<支付凭证，query-payment 返回的 status 或完整响应>"
                        },
                        "method": {
                            "clientSession": "<回测资源标识，从 payment_needed_header 解码后 .protocol.resource_id>"
                        }
                    },
                    "encoding": "1. 将上述 JSON 序列化（不带空格）→ 2. Base64URL 编码（urlsafe_b64encode）→ 3. 去掉末尾 = 填充符",
                    "python_example": "import base64,json; proof=json.dumps({'protocol':{'tradeNo':trade_no,'paymentProof':proof_str},'method':{'clientSession':resource_id}},separators=(',',':')); encoded=base64.urlsafe_b64encode(proof.encode()).decode().rstrip('=')",
                    "usage": "将编码后的字符串作为 run_backtest 的 payment_proof 参数传入即可"
                },
            }

        # 401 = API Key 无效
        if r.status_code == 401:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: run_backtest({symbol}) — 401 API Key 无效 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "error": "API Key 无效或已过期。请检查 TICKTEST_API_KEY 环境变量是否正确。",
                "auth_error": True,
                "hint": "请在 mcpServers 配置的 env 中更新 TICKTEST_API_KEY。获取新 Key: https://ticktest.cn/keys",
            }

        # 403 = 权限不足
        if r.status_code == 403:
            elapsed = time.time() - t0
            logger.warning(
                f"Tool: run_backtest({symbol}) — 403 权限不足 "
                f"(耗时 {elapsed:.2f}s)"
            )
            return {
                "success": False,
                "error": "API Key 权限不足，可能未开通回测功能或套餐不支持此操作。",
                "auth_error": True,
                "hint": "请升级套餐或联系客服开通回测权限。",
            }

        r.raise_for_status()
        result = _safe_json(r)

        elapsed = time.time() - t0
        if result.get("success"):
            metrics = result.get("metrics", {})
            logger.info(
                f"Tool: run_backtest({symbol}) — 成功 "
                f"(耗时 {elapsed:.2f}s, "
                f"年化={metrics.get('annual_return', 'N/A')}, "
                f"夏普={metrics.get('sharpe_ratio', 'N/A')}, "
                f"交易{metrics.get('trade_count', 'N/A')}笔)"
            )
        else:
            logger.warning(
                f"Tool: run_backtest({symbol}) — API 返回失败 "
                f"(耗时 {elapsed:.2f}s): {result.get('error', 'unknown')}"
            )

        return result

    except httpx.TimeoutException:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: run_backtest({symbol}) — 超时 "
            f"(耗时 {elapsed:.2f}s, 阈值 {BACKTEST_TIMEOUT}s)"
        )
        return {
            "success": False,
            "error": f"回测请求超时（超过 {BACKTEST_TIMEOUT}s）。请尝试缩短回测时间范围或选择更简单的策略。",
            "timeout": True,
            "elapsed_seconds": round(elapsed, 1),
        }
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t0
        return _log_http_error("run_backtest", e, elapsed,
                               context=f"symbol={symbol}")
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: run_backtest({symbol}) — 未预期异常 "
            f"(耗时 {elapsed:.2f}s): {e}\n{traceback.format_exc()}"
        )
        return {
            "success": False,
            "error": f"回测执行异常: {e}",
            "traceback": traceback.format_exc(),
        }


def health_check() -> dict:
    """检查 TickTest API 服务状态"""
    t0 = time.time()
    logger.info("Tool: health_check — 开始")

    try:
        r = _http_request("GET", "/v1/health")
        r.raise_for_status()
        result = _safe_json(r)
        elapsed = time.time() - t0
        logger.info(
            f"Tool: health_check — 成功 "
            f"(耗时 {elapsed:.2f}s, 状态={result.get('status', 'unknown')})"
        )
        return result
    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t0
        return _log_http_error("health_check", e, elapsed)
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: health_check — 未预期异常 "
            f"(耗时 {elapsed:.2f}s): {e}\n{traceback.format_exc()}"
        )
        return {
            "success": False,
            "error": f"健康检查异常: {e}",
            "traceback": traceback.format_exc(),
        }


def create_payment(
    symbol: str = "",
    strategy: str = "",
) -> dict:
    """
    创建支付订单 — 同时生成「单次回测 ¥0.50」和「基础套餐 ¥29」两张支付码。

    调用 TickTest 的两个支付 API 端点:
      - POST /v1/payment/create        → 单次回测支付
      - POST /v1/payment/alipay/credit/create → 信用套餐充值 (需 API Key)

    Args:
        symbol: 股票代码（用于订单关联，可选）
        strategy: 策略描述（用于订单关联，可选）

    Returns:
        {
            "success": true,
            "free_strategy": {...},           # 海龟交易免费提示
            "single_backtest": {              # 单次回测 ¥0.50
                "out_trade_no", "amount", "payment_form", ...
            },
            "credit_package": {               # 基础套餐 ¥29
                "out_trade_no", "amount", "plan_name", "credits", "payment_form", ...
            },
            "routes_note": "双通道说明",
            "combined_html": "<html>...</html>" # 可直接打开的支付页面
        }
    """
    t0 = time.time()
    logger.info(f"Tool: create_payment(symbol={symbol}, strategy={strategy[:30] if strategy else 'none'}) — 开始")

    result = {
        "success": True,
        "free_strategy": {
            "name": "海龟交易法则",
            "key": "TURTLE_TRADING",
            "description": "突破20日高点买入，跌破10日低点卖出。经典趋势跟踪策略。",
            "note": "💰💰💰 永久免费！不限次数，不消耗信用点。新手入门首选，Agent 上手体验零成本。",
            "usage": "在 run_backtest 中 strategy 参数填「海龟交易」即可，无需任何支付。",
        },
        "single_backtest": None,
        "credit_package": None,
        "routes_note": (
            "🔀 双通道支付说明：\n"
            "  • 若您的 Agent 安装了 @alipay/mcp-server-alipay → 可用其 create-*-payment 工具走 AI收 402 原生流程\n"
            "  • 本 Tool 永远可用 → 支付宝网页扫码 → 自动充值到账 → 重试回测（无需 Payment-Proof 头）"
        ),
        "combined_html": None,
    }

    # ── 1. 单次回测支付 ¥0.50（page pay → 提取支付宝 URL → HTML 端生成 QR 码）──
    try:
        payload_single = {
            "symbol": symbol or "sz300308",
            "strategy": strategy or "单次回测",
            "total_amount": "0.50",
        }
        r = _http_request("POST", "/v1/payment/create", json_payload=payload_single)
        r.raise_for_status()
        single_data = _safe_json(r)

        if single_data.get("status") == "success" and single_data.get("payment_form"):
            form = single_data["payment_form"]
            import re as _re
            out_trade_no_m = _re.search(r'(TICKTEST_[^"&\\\s<>]+)', form)

            result["single_backtest"] = {
                "success": True,
                "out_trade_no": out_trade_no_m.group(1) if out_trade_no_m else "",
                "amount": "0.50",
                "payment_form": form,
                "description": f"单次回测 — {symbol or 'sz300308'} {strategy or '单次回测'}",
            }
            logger.info(f"Tool: create_payment — 单次支付创建成功 | {result['single_backtest']['out_trade_no']}")
        else:
            logger.warning(f"Tool: create_payment — 单次支付创建失败: {single_data}")
            result["single_backtest"] = {"success": False, "error": single_data.get("message", "未知错误")}
    except Exception as e:
        logger.error(f"Tool: create_payment — 单次支付异常: {e}")
        result["single_backtest"] = {"success": False, "error": str(e)}

    # ── 2. 信用套餐支付 ¥29 基础版（page pay → 提取 URL → HTML 端生成 QR 码）──
    try:
        payload_credit = {"plan_id": "basic"}
        r = _http_request("POST", "/v1/payment/alipay/credit/create", json_payload=payload_credit)
        r.raise_for_status()
        credit_data = _safe_json(r)

        if credit_data.get("success") and credit_data.get("payment_form"):
            form = credit_data["payment_form"]
            import re as _re

            result["credit_package"] = {
                "success": True,
                "out_trade_no": credit_data.get("out_trade_no", ""),
                "amount": credit_data.get("amount", "29.00"),
                "plan_name": credit_data.get("plan_name", "入门套餐"),
                "credits": credit_data.get("credits", 2500),
                "payment_form": form,
                "description": f"入门套餐 — {credit_data.get('credits', 2500)}信用点（≈100次回测）",
            }
            logger.info(f"Tool: create_payment — 套餐支付创建成功 | {result['credit_package']['out_trade_no']} | {result['credit_package']['plan_name']} ¥{result['credit_package']['amount']}")
        else:
            logger.warning(f"Tool: create_payment — 套餐支付创建失败: {credit_data}")
            result["credit_package"] = {"success": False, "error": credit_data.get("message", "未知错误（可能需要先注册账号）")}
    except Exception as e:
        logger.error(f"Tool: create_payment — 套餐支付异常: {e}")
        result["credit_package"] = {"success": False, "error": str(e)}

    # ── 3. 生成合并支付页面 HTML（点击跳转支付宝官方支付页）──
    try:
        import re as _re
        single_form = (result["single_backtest"] or {}).get("payment_form", "")
        credit_form = (result["credit_package"] or {}).get("payment_form", "")

        # 提取支付宝 URL
        single_url = ""
        credit_url = ""
        if single_form:
            m = _re.search(r'action="([^"]+)"', single_form)
            if m: single_url = m.group(1).replace("&amp;", "&")
        if credit_form:
            m = _re.search(r'action="([^"]+)"', credit_form)
            if m: credit_url = m.group(1).replace("&amp;", "&")

        combined_html = _build_combined_payment_html(
            symbol=symbol or "sz300308",
            strategy=strategy or "单次回测",
            single_form=single_form if single_form else "",
            credit_form=credit_form if credit_form else "",
            single_alipay_url=single_url,
            credit_alipay_url=credit_url,
            single_amount="0.50",
            credit_amount=(result["credit_package"] or {}).get("amount", "29.00"),
            credit_plan=(result["credit_package"] or {}).get("plan_name", "入门套餐"),
            credit_points=str((result["credit_package"] or {}).get("credits", 2500)),
        )
        result["combined_html"] = combined_html
    except Exception as e:
        logger.error(f"Tool: create_payment — 生成HTML失败: {e}")

    elapsed = time.time() - t0
    logger.info(f"Tool: create_payment — 完成 (耗时 {elapsed:.2f}s)")

    return result


def _build_combined_payment_html(
    symbol: str,
    strategy: str,
    single_form: str,
    credit_form: str,
    single_alipay_url: str,
    credit_alipay_url: str,
    single_amount: str,
    credit_amount: str,
    credit_plan: str,
    credit_points: str,
) -> str:
    """构建双支付卡片的合并 HTML 页面——点击跳转支付宝官方支付页，用支付宝自己的码，百分百支付成功。"""
    import re as _re
    # 提取 biz_content
    single_biz = ""
    credit_biz = ""
    if single_form:
        m = _re.search(r'name="biz_content"\s+value="([^"]*)"', single_form)
        if m: single_biz = m.group(1)
    if credit_form:
        m = _re.search(r'name="biz_content"\s+value="([^"]*)"', credit_form)
        if m: credit_biz = m.group(1)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TickTest 支付 — {symbol}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }}
  .container {{ max-width: 800px; margin: 0 auto; }}
  h1 {{ color: white; text-align: center; font-size: 28px; margin-bottom: 8px; text-shadow: 0 2px 4px rgba(0,0,0,0.2); }}
  .subtitle {{ color: rgba(255,255,255,0.85); text-align: center; font-size: 14px; margin-bottom: 24px; }}

  .free-banner {{ background: linear-gradient(135deg, #fa8c16, #ffc53d); color: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 20px; text-align: center; box-shadow: 0 4px 16px rgba(250, 140, 22, 0.4); }}
  .free-banner .icon {{ font-size: 48px; margin-bottom: 8px; }}
  .free-banner h2 {{ font-size: 22px; margin-bottom: 4px; }}
  .free-banner p {{ font-size: 14px; opacity: 0.9; }}

  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 640px) {{ .cards {{ grid-template-columns: 1fr; }} }}

  .card {{ background: white; border-radius: 16px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.15); }}
  .card-header {{ padding: 20px 24px 12px; text-align: center; }}
  .card-header .badge {{ display: inline-block; background: #f0f5ff; color: #1677ff; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; margin-bottom: 8px; }}
  .card-header .badge.pro {{ background: #fff7e6; color: #fa8c16; }}
  .card-header .price {{ font-size: 36px; font-weight: bold; color: #1a1a1a; }}
  .card-header .price small {{ font-size: 16px; color: #999; font-weight: normal; }}
  .card-header .desc {{ color: #666; font-size: 13px; margin-top: 4px; }}

  .card-body {{ padding: 0 24px 20px; }}
  .qrcode-area {{ text-align: center; cursor: pointer; border: 2px dashed #e8e8e8; border-radius: 12px; padding: 20px; transition: all 0.3s; }}
  .qrcode-area:hover {{ border-color: #1677ff; background: #f0f5ff; }}
  .qrcode-area .qr-icon {{ font-size: 64px; margin-bottom: 8px; }}
  .qrcode-area .qr-text {{ color: #1677ff; font-size: 16px; font-weight: bold; }}
  .qrcode-area .qr-hint {{ color: #999; font-size: 12px; margin-top: 4px; }}

  .card-footer {{ background: #fafafa; padding: 12px 24px; border-top: 1px solid #f0f0f0; }}
  .card-footer .order-no {{ font-size: 11px; color: #bbb; word-break: break-all; }}

  .routes-note {{ background: rgba(255,255,255,0.15); backdrop-filter: blur(10px); color: white; border-radius: 12px; padding: 16px 20px; margin-top: 20px; font-size: 13px; line-height: 1.8; }}
  .routes-note strong {{ color: #ffd666; }}
  .refund-warning {{ background: #fff2f0; border: 1px solid #ffccc7; border-radius: 8px; padding: 10px 16px; margin-top: 12px; font-size: 13px; color: #cf1322; text-align: center; line-height: 1.6; }}
  .refund-warning span {{ font-weight: bold; font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
  <h1>💳 TickTest 支付</h1>
  <div class="subtitle">{symbol} · {strategy}</div>

  <!-- 免费策略横幅 -->
  <div class="free-banner">
    <div class="icon">🐢</div>
    <h2>海龟交易法则 — 永久免费！</h2>
    <p>突破20日高点买入，跌破10日低点卖出。经典趋势跟踪，不限次数，不消耗信用点。</p>
    <p style="margin-top:8px;font-size:13px;">💡 在 run_backtest 中 strategy 填「海龟交易」即可，零成本体验回测。</p>
  </div>

  <!-- 双支付卡片 -->
  <div class="cards">
    <!-- 单次回测 ¥0.50 -->
    <div class="card" id="card-single">
      <div class="card-header">
        <div class="badge">单次回测</div>
        <div class="price">{single_amount}<small> 元</small></div>
        <div class="desc">1次回测，按需付费</div>
      </div>
      <div class="card-body">
        <div class="qrcode-area" onclick="document.getElementById('form-single').submit()" title="点击跳转支付宝官方支付页">
          <div class="qr-icon">📱</div>
          <div class="qr-text">点击支付 ¥{single_amount}</div>
          <div class="qr-hint">跳转支付宝官方页面支付</div>
        </div>
        <div class="refund-warning">⚠️ 单次回测为虚拟商品<br>一经售出<span>概不退款</span></div>
      </div>
    </div>

    <!-- 基础套餐 ¥29 -->
    <div class="card" id="card-credit">
      <div class="card-header">
        <div class="badge pro">⭐ 推荐</div>
        <div class="price">{credit_amount}<small> 元</small></div>
        <div class="desc">{credit_plan} · {credit_points} 信用点 · ≈100次回测</div>
      </div>
      <div class="card-body">
        <div class="qrcode-area" onclick="document.getElementById('form-credit').submit()" title="点击跳转支付宝官方支付页">
          <div class="qr-icon">💳</div>
          <div class="qr-text">点击支付 ¥{credit_amount}</div>
          <div class="qr-hint">跳转支付宝官方页面支付</div>
        </div>
        <div class="refund-warning">⚠️ 虚拟商品一经售出<span>概不退款</span><br>支付后自动充值到账，即可继续回测</div>
      </div>
    </div>
  </div>

  <!-- 双通道说明 -->
  <div class="routes-note">
    <strong>📋 支付说明：</strong><br>
    · 支付后<strong>自动充值到账</strong>，到账后即可继续回测<br>
    · <strong>海龟交易策略永久免费</strong>，新手注册即送 500 信用点<br>
    · 虚拟商品一经售出<strong>概不退款</strong>，请确认后再支付
  </div>
</div>

<div style="display:none;">
<form id="form-single" method="post" action="{single_alipay_url}" target="_blank">
  <input type="hidden" name="biz_content" value="{single_biz}">
</form>
<form id="form-credit" method="post" action="{credit_alipay_url}" target="_blank">
  <input type="hidden" name="biz_content" value="{credit_biz}">
</form>
</div>

</body>
</html>"""
    return html

def _log_http_error(
    tool_name: str,
    e: httpx.HTTPStatusError,
    elapsed: float,
    context: str = "",
) -> dict:
    """统一处理 HTTP 状态错误：记录日志 + 返回友好错误"""
    status = e.response.status_code
    detail = e.response.text[:500]
    logger.error(
        f"Tool: {tool_name} — HTTP {status} "
        f"(耗时 {elapsed:.2f}s{' — ' + context if context else ''}): {detail}"
    )

    # 按状态码给出中文提示
    status_hints = {
        400: "请求参数有误，请检查输入。",
        401: "API Key 无效或已过期，请检查 TICKTEST_API_KEY。",
        402: "信用点不足。AI Agent 可调用 POST /v1/payment/vmq/create 帮用户充值。充值地址: https://ticktest.cn/billing",
        403: "权限不足，请检查 API Key 是否已开通相关功能。",
        404: "请求的资源不存在，请检查参数是否正确。",
        429: "请求过于频繁，请稍后重试。",
        500: "TickTest API 服务器内部错误，请稍后重试或联系技术支持。",
        502: "TickTest API 网关错误，服务可能正在维护。",
        503: "TickTest API 服务暂时不可用，请稍后重试。",
    }

    return {
        "success": False,
        "error": f"API 返回 HTTP {status}",
        "detail": detail,
        "hint": status_hints.get(status, f"请求失败 (HTTP {status})，请稍后重试。"),
    }


# ── MCP Server 定义 ────────────────────────────────────

server = Server("ticktest-mcp")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """声明 MCP Tools —— AI Agent 看到的工具列表"""
    tools = [
        Tool(
            name="get_capabilities",
            description="获取 TickTest 回测 API 的能力清单：可用策略分类（经典策略/135战法/K线形态/量价关系/周线月线等）、因子列表、支持的股票范围（sz/sh/bj）、定价信息。AI Agent 应先调用此工具了解可用能力，再决定如何构造回测请求。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="health_check",
            description="检查 TickTest API 服务是否正常运行。返回服务状态和各子模块健康信息。",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]

    # 公开 Tool（无需 API Key，底层 API 端点均为公开）
    tools.append(
        Tool(
            name="validate_strategy",
            description="免费验证策略参数是否合法。在调用 run_backtest 之前，先验证策略 family 和 params 格式，避免因参数错误浪费信用点。支持 SINGLE_MA（单均线）/ MA_CROSSOVER（双均线交叉）/ TURTLE_TRADING（海龟交易）三种策略族。返回 valid=true/false 及标准化参数和警告信息。无需 API Key。",
            inputSchema={
                "type": "object",
                "properties": {
                    "family": {
                        "type": "string",
                        "description": "策略族名称。支持: SINGLE_MA（单均线）、MA_CROSSOVER（双均线交叉）、TURTLE_TRADING（海龟交易）",
                        "enum": ["SINGLE_MA", "MA_CROSSOVER", "TURTLE_TRADING"],
                    },
                    "params": {
                        "type": "object",
                        "description": "策略参数（可选）。SINGLE_MA: {ma_n: 20, buy_frequency: 'weekly', sell_frequency: 'daily'}。MA_CROSSOVER: {fast: 5, slow: 20, buy_frequency: 'daily', sell_frequency: 'daily'}。TURTLE_TRADING: {}（使用默认参数）",
                    },
                },
                "required": ["family"],
            },
        )
    )
    tools.append(
        Tool(
            name="search_symbols",
            description="免费搜索 A 股股票代码。输入中文名称（如'中际旭创'）或代码片段（如'300308'），返回匹配的股票列表（含 symbol/name/exchange），用于确定 run_backtest 所需的 symbol 参数。无需 API Key。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。可以是股票中文名称（如'中际旭创''平安银行'）或代码片段（如'300308''000001'）。支持模糊匹配。",
                    },
                },
                "required": ["query"],
            },
        )
    )

    if HAS_AUTH:
        tools.append(
            Tool(
                name="run_backtest",
                description="对指定 A 股股票运行历史回测。只需提供 symbol + strategy 即可，其余参数自动默认。支持自然语言（如'MA5上穿MA20买入，跌破MA10卖出'）或策略别名（如'红杏出墙''三金叉''老鸭头'）。返回收益率、夏普比率、最大回撤等六大指标。每笔 ¥0.50（海龟交易永久免费）。建议先用 search_symbols 确认股票代码、用 validate_strategy 校验策略。若信用点不足，请使用 create_payment 工具充值。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "股票代码，格式：交易所前缀+6位数字。sz=深交所，sh=上交所，bj=北交所。示例：'sz000001'(平安银行)、'sh600157'(永泰能源)。建议先用 search_symbols 搜索确认。",
                        },
                        "strategy": {
                            "type": "string",
                            "description": "策略描述，自然语言即可。如'MA5上穿MA20买入，跌破MA10卖出'、'海龟交易'、'红杏出墙'。海龟交易永久免费。",
                        },
                        "start_date": {
                            "type": "string",
                            "description": "回测起始日期 YYYY-MM-DD。不填默认半年前，用户不指定就不传。",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "回测结束日期 YYYY-MM-DD。不填默认为最近交易日",
                        },
                        "frequency": {
                            "type": "string",
                            "enum": ["daily", "weekly", "monthly"],
                            "description": "K线周期。不填默认 daily",
                        },
                        "payment_proof": {
                            "type": "string",
                            "description": "AI收支付凭证（Base64URL编码的JSON）。走AI收402流程时，将 query-alipay-payment 的支付结果按 payment_proof_format 格式编码后传入，即可自动扣点回测。格式见402响应中的 payment_proof_format 字段。create_payment 兜底通道无需此参数。",
                        },
                    },
                    "required": ["symbol", "strategy"],
                },
            )
        )
        tools.append(
            Tool(
                name="create_payment",
                description="创建支付订单，同时生成「单次回测 ¥0.50」和「入门套餐 ¥29（2500点≈100次）」两张支付码。支付宝网页扫码支付，到账后自动充值，即可继续回测。返回结构化支付信息 + 可直接在浏览器打开的合并支付页面 HTML。海龟交易策略永久免费，无需支付。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "要回测的股票代码（可选，用于订单关联）。如 'sz300308'",
                        },
                        "strategy": {
                            "type": "string",
                            "description": "要回测的策略描述（可选，用于订单关联）。如 '股价突破5日线买入，跌破5日线卖出'",
                        },
                    },
                    "required": [],
                },
            )
        )

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """执行 MCP Tool 调用"""
    t0 = time.time()
    try:
        if name == "get_capabilities":
            result = get_capabilities()
        elif name == "health_check":
            result = health_check()
        elif name == "validate_strategy":
            result = validate_strategy(
                family=arguments["family"],
                params=arguments.get("params"),
            )
        elif name == "search_symbols":
            result = search_symbols(
                query=arguments["query"],
            )
        elif name == "run_backtest":
            result = run_backtest(
                symbol=arguments["symbol"],
                strategy=arguments["strategy"],
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
                frequency=arguments.get("frequency", "daily"),
                payment_proof=arguments.get("payment_proof"),
            )
        elif name == "create_payment":
            result = create_payment(
                symbol=arguments.get("symbol", ""),
                strategy=arguments.get("strategy", ""),
            )
        else:
            elapsed = time.time() - t0
            logger.warning(f"Tool 调用: 未知工具 '{name}' (耗时 {elapsed:.2f}s)")
            return [TextContent(type="text", text=json.dumps(
                {"success": False, "error": f"未知工具: {name}"},
                ensure_ascii=False, indent=2,
            ))]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except httpx.HTTPStatusError as e:
        elapsed = time.time() - t0
        status = e.response.status_code
        detail = e.response.text[:500]
        logger.error(
            f"Tool: {name} — HTTP {status} 异常 (耗时 {elapsed:.2f}s): {detail}\n"
            f"{traceback.format_exc()}"
        )
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"API 返回 HTTP {status}",
                "detail": detail,
                "hint": _status_hint(status),
            }, ensure_ascii=False, indent=2),
        )]
    except httpx.TimeoutException as e:
        elapsed = time.time() - t0
        logger.error(f"Tool: {name} — 超时 (耗时 {elapsed:.2f}s): {e}")
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"请求超时（超过 {DEFAULT_TIMEOUT}s）",
                "hint": "请稍后重试或检查网络连接。",
            }, ensure_ascii=False, indent=2),
        )]
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(
            f"Tool: {name} — 未预期异常 (耗时 {elapsed:.2f}s): {e}\n"
            f"{traceback.format_exc()}"
        )
        return [TextContent(
            type="text",
            text=json.dumps({
                "success": False,
                "error": f"Tool 执行异常: {e}",
                "traceback": traceback.format_exc(),
            }, ensure_ascii=False, indent=2),
        )]


def _status_hint(status: int) -> str:
    """返回 HTTP 状态码的中文提示"""
    return {
        400: "请求参数有误，请检查输入。",
        401: "API Key 无效或已过期，请检查 TICKTEST_API_KEY。",
        402: "信用点不足。AI Agent 可调用 POST /v1/payment/vmq/create 帮用户充值。充值地址: https://ticktest.cn/billing",
        403: "权限不足，请检查 API Key 是否已开通相关功能。",
        404: "请求的资源不存在。",
        429: "请求过于频繁，请稍后重试。",
        500: "TickTest API 服务器内部错误，请稍后重试。",
        502: "TickTest API 网关错误，服务可能正在维护。",
        503: "TickTest API 服务暂时不可用，请稍后重试。",
    }.get(status, f"请求失败 (HTTP {status})，请稍后重试。")


# ── 入口 ────────────────────────────────────────────────

async def main():
    """启动 MCP Server（stdio 传输）"""
    logger.info("=" * 50)
    logger.info("TickTest MCP Server v0.3.1 启动")
    logger.info(f"API URL: {API_URL}")
    logger.info(f"认证状态: {'已配置' if HAS_AUTH else '未配置（只读模式）'}")
    if HAS_AUTH:
        logger.info(f"API Key: {API_KEY[:8]}...{API_KEY[-4:]}")
    logger.info(f"重试策略: 最多 {MAX_RETRIES} 次, 指数退避")
    logger.info(f"回测超时: {BACKTEST_TIMEOUT}s")
    logger.info(f"传输方式: stdio（日志走 stderr）")
    logger.info(f"可用 Tool: get_capabilities, health_check, validate_strategy, search_symbols"
                f"{', run_backtest, create_payment' if HAS_AUTH else ''}")
    logger.info("=" * 50)
    logger.info("等待 AI Agent 连接...")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
