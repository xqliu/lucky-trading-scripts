# 开发铁律 (Non-Negotiable Rules)

这些规则经过实盘事故验证，违反任何一条都可能导致资金损失。

---

## 1. 单一真相源 (Single Source of Truth)

**每个函数只在一个文件中定义。**

- **通用数学指标**（EMA、RSI 等）→ 只在 `indicators.py`
- **策略函数**（detect_signal、get_trend_4h、should_tighten_tp 等）→ 只在 `strategy.py`
- 其他文件（backtest、signal、execute、chart）一律 import，不重新实现
- **绝对禁止**在任何非定义文件中重新实现指标或策略逻辑

**为什么**：2026-02 BB 系统有 4 个独立的 detect_signal 实现，每个 BB 窗口计算方式不同（v1 含当前 bar，v2/v3 不含），导致回测结论与实盘行为不一致。

**CI 强制**：`test_critical_paths.py::TestNoIndicatorDuplication` 会扫描所有 .py 文件，发现重复定义直接 fail。

---

## 2. 回测必须调用实盘代码

**回测信号生成必须调用 `strategy.detect_signal()`，不准手写信号逻辑。**

- 回测脚本只负责：加载数据 → 调 detect_signal → 模拟持仓/出场 → 统计
- 信号判断的每一行代码都在 strategy.py，回测不碰

**为什么**：每次回测结论不一致的根因就是自己重写了信号生成逻辑。

**CI 强制**：`test_critical_paths.py::TestNoIndicatorDuplication::test_backtest_imports_detect_signal_from_strategy`

---

## 3. 链上状态优先 (Chain-First)

**任何涉及仓位变更的重试逻辑，每次重试前必须查链上状态。**

- `emergency_close()` 每次重试前先 `get_position()` 确认仓位还在
- `close_position()` 同理
- 仓位已平 → 立即停止，不再尝试

**为什么**：2026-03 事故：emergency_close 重试没查链上 → 第 2 次平了 LONG → 第 3 次开了反向 SHORT → 无止损 SHORT 暴露。

**CI 强制**：`test_critical_paths.py::TestChainFirstSafety`

---

## 4. API 调用必须有 429 重试

**所有交易所 API 调用必须经过 `_retry_on_429()` 包装。**

- `trade.py` 中的 `place_market_order`、`place_limit_order`、`cancel_order` 等已包装
- 新增 API 调用时必须用同样的包装

**为什么**：多币种同时开仓时，第二个币种的 SL 下单被 429 拒绝，导致裸仓。

---

## 5. 止损价格必须 round()

**所有发送到交易所的价格必须 `round()` 到整数（BTC）或合理精度。**

- `execute.py` 的 SL/TP 价格：`round(price * (1 ± pct))`
- `trailing.py` 的移动止损：同样 `round()`

**为什么**：`$2,116.816` 被 Hyperliquid 拒绝为 "invalid price"，导致止损永远设不上。

---

## 6. 安全原则确立后必须全面扫描

**确立新安全原则后，必须扫描所有相关代码路径确认一致性。**

- 不能只修当前出问题的函数
- 同模块的类似函数（如 close_position 和 emergency_close）必须同步检查
- 用 grep 搜索所有调用点

**为什么**：`close_position()` 早就有链上检查，但同模块的 `emergency_close()` 是更早写的代码，没有回头补。

---

## 7. 测试覆盖关键路径

**每次修改必须确认 `pytest tests/` 全部通过。**

关键测试文件：
- `test_critical_paths.py` — 结构性约束（指标唯一性、参数一致性、禁止 bare except）
- `test_emergency_close.py` — 紧急平仓逻辑
- `test_execute_signal.py` — 开仓/平仓完整流程

---

## 8. 小样本不能推翻回测

**修改策略参数前**：
1. 查 `memory/trading/baselines/` 的历史验证
2. 调用 `strategy.detect_signal()` 做回测（规则 #2）
3. 包含真实交易成本（8.64bps/round-trip）
4. Walk-forward 4 段验证
5. ≥20 笔实盘才有统计显著性，5 笔不能说明任何问题

---

## 9. 生命周期场景必须全覆盖

**每个系统组件的代码审查必须覆盖以下所有场景，不是"边界情况"，是正常运维：**

### 核心场景清单

| # | 场景 | 要验证的问题 |
|---|------|-------------|
| 1 | **有持仓时重启服务** | SL/TP 是否保留？cleanup 是否只取消 trigger？reconciliation 是否验证 SL 存在？ |
| 2 | **有持仓时部署新代码** | 同上 + 新代码是否兼容旧 state 文件格式？ |
| 3 | **有挂单时重启服务** | trigger 是否正确取消和重建？是否会出现重复挂单？ |
| 4 | **WS 断线时有持仓** | SL/TP 是交易所端维护的不受影响？重连后状态同步是否正确？ |
| 5 | **WS 断线时 trigger 触发** | 私有频道收不到 fill 事件，重连后是否检测到新仓位并补设 SL/TP？ |
| 6 | **SL/TP 设置失败** | 是否紧急平仓？是否通知？是否不会静默失败？ |
| 7 | **API 返回错误/超时** | 每个 REST 调用是否有超时和错误处理？是否有重试？ |
| 8 | **服务 OOM/SIGKILL** | ExecStop 不会运行，重启后 reconciliation 能否恢复？ |
| 9 | **部分成交** | 挂的 2.53 张只成交 1.0 张，SL/TP 的数量是否匹配实际持仓？ |
| 10 | **SL 和 TP 同时触发** | 两个单都发到交易所，会不会导致反向开仓？TP 是否 reduceOnly？ |
| 11 | **手动在 OKX App 平仓** | 系统能否检测到仓位消失并清理本地状态和剩余挂单？ |
| 12 | **账户余额不足** | calculate_size 返回 None 时，系统是否优雅处理而非崩溃？ |
| 13 | **价格剧烈波动穿透 SL** | SL 是市价触发，滑点可能超过 2%，最大亏损预估是否准确？ |
| 14 | **资金费率结算时刻** | 8h 结算影响保证金率，是否可能触发强平？ |
| 15 | **同时收到多个 WS 事件** | 单线程 asyncio 保证顺序，但 _rest() 调用是否会交错？ |

### 事故教训（2026-03-01）

**cleanup.py 取消 SL/TP 导致裸仓 3 小时：**
- 入场成交 → SL/TP 正确设置 → 重启服务 → cleanup 取消所有单 → 新进程启动 → reconciliation 看到本地状态有记录 → 跳过 SL 验证 → 裸仓
- 根因 1：cleanup 不区分 trigger 和 SL/TP
- 根因 2：reconciliation 信任本地状态，不验证交易所实际状态
- **教训：交易所状态是唯一真相源，本地状态只是缓存**

---

## 检查清单（PR/改动前）

- [ ] 新增的指标计算是否在 `strategy.py`？
- [ ] 回测是否调用 `strategy.detect_signal()`？
- [ ] 涉及仓位变更的重试是否查链上状态？
- [ ] API 调用是否有 429 重试包装？
- [ ] 价格是否 round？
- [ ] 类似函数是否同步检查了？
- [ ] `pytest tests/` 全部通过？
- [ ] **生命周期场景 #1-#15 是否全覆盖？**（特别是有持仓时的各种操作）
- [ ] **重启变量审查**：逐一检查所有 `__init__` 中的 `self._xxx` 实例变量——在有持仓时，初始值是否安全？是否需要从恢复逻辑中补值？（2026-03-01 教训：`_early_validation_done` 重启清零导致误平仓 -1.31%）
