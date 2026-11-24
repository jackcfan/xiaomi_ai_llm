# 决策路由与规则自学习 — 测试说明

日期：2026-11-24

## 验证范围与分层

按 `TESTING.md` 的分层验证原则：以下全部是**主机回归与文件/协议级验证**，未访问真实网络、真实百炼决策接口、真实音箱，**不是真人现场验收**。真实识别效果、学习闭环的「越用越聪明」、以及音箱端到端出声，都待实机验收。

## 主机回归

```sh
sh scripts/run_tests.sh
```

结果：hub 全部 unittest 通过（含 12 个任务的测试：store / intents / rules / chat / decision / learn / app / admin 共 88+ 项）；三条嵌入产物一致性 `--check` 与 `sh -n` 语法检查不受本改动影响（本改动零接触 `device/` 与 `server/`）。

## 单元测试覆盖

| 模块 | 覆盖点 |
|---|---|
| `tests/hub/test_store.py` | 三张新表、`turns` 六列迁移、新默认设置补种、`bump_rule_stats` |
| `tests/hub/test_intents.py` | 意图注册表与 handler 一一对应、`sync_intents` 不覆盖后台文案、执行器 |
| `tests/hub/test_rules.py` | 长度闸门、`fullmatch` 语义、优先级、实体解析、黄金语料（rule/none） |
| `tests/hub/test_chat.py` | 三级路由、规则命中免模型、确认带、抽样复核、自动停用、纠正检测 |
| `tests/hub/test_decision.py` | systemone 请求形状（无 Bearer）、两阶段判定、置信度、实体须在原话、超时 |
| `tests/hub/test_learn.py` | 候选规则校验（白名单/负例/去重/长度）、样本挖掘、幂等、空语料 |

## 待实机验收（真实接口）

1. **决策模型真实识别**：`scripts/eval_routing.py` 读同一份黄金语料打真实接口，输出逐条 `OK/XX` 与准确率。前置：MySQL 里 `decision_provider_id` 指向百炼决策模型行、`FN_MYSQL_PWD` 已设置。
2. **学习闭环**：`POST /api/v1/decision/learn`（或后台「立即学习」）在有真实 `model` 路由样本后跑一次，确认新规则入库、命中后可被后续请求直接匹配，且 `max_text_len` 生效。
3. **音箱端到端**：hub 挂真实百炼密钥后，对音箱说「将角色切换到银月」「你有哪些技能」，确认识别、播报、切换生效。

## 已决取舍（记录，非验收项）

- 问句式命令（「换成毒舌吗」）直接执行，不额外确认（用户选 B，减少交互次数）。
- 学习先只暴露 HTTP 接口人工触发，调度方式（cron / 应用内定时）后续再定。
- 学到的规则生效即用，靠抽样复核 + 不一致率自动停用兜底，不做人工待审队列。
