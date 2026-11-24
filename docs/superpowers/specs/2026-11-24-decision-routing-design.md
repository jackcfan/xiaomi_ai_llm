# 决策路由与规则自学习 设计文档

日期：2026-11-24
状态：待实现（本文档已与你确认过取向，细节以本文为准）

## 1. 意图与动机

hub 现在的指令识别是 `hub/commands.py` 里 60 行硬编码正则：只有「重置对话」和「切换人设/模型」两类，且都是
`fullmatch` + 实体名与 personas/providers 的 name/aliases **完全相等**。结果是「将角色切换到银月」
（不以「切换到」开头）、「进入毒舌模式」这类正常说法根本匹配不上，识别不了的句子全部落到普通问答。

目标是把识别改成三级：**正则快路径 → 决策模型判定 → 普通问答**，并且每一次判定（含判成普通问答）都落库，
由离线学习任务把「正则没命中、但决策模型识别出来了」的样本反哺成新的正则规则入库，使系统随使用越来越准。

决策模型用百炼的 `decision-model-preview`。它不是聊天模型，而是**声明式判定接口**：你声明问题和候选标签，
它返回带置信度与概率分布的答案，约 50ms~1s。

## 2. 目标 / 非目标 / 反目标

**目标**

- 识别 `切换角色或模型`、`重置对话`、`列出能力` 三类意图，并解析参数。
- 正则命中的路径不产生任何网络与模型开销，行为与今天一致。
- 未命中时由决策模型判定，低置信度不得误执行。
- 每次判定落库，带来源、规则 id、意图、参数、置信度、学习状态。
- 提供 HTTP 接口手动触发学习：从近期样本生成新规则入库，并给出每条规则的文本长度上限。

**非目标**

- 不改设备端。设备直连 DeepSeek 的路径（hub 不在线时）没有这套识别，行为不变。
- 不让决策模型生成任何播报文本——它没有这个能力。
- 不做通用 NLU。意图集合是封闭的，代码里能执行多少种，就识别多少种。
- 不做定时调度。本期只暴露接口，cron 还是应用内定时器后续再定。
- 不做多意图并行执行。一句话里点两个实体，只执行第一个。

**反目标**

- 不能让普通问答的首句延迟变差。正则命中 0ms；决策调用硬超时（默认 800ms），超时即问答。
- 不能让学出来的规则抢走普通提问。规则必须 `fullmatch` + 长度上限 + 负例校验。
- 不能让模型编出系统执行不了的命令。运行时的候选标签来自数据库里的意图表，表由代码播种。

## 3. 约束

**决策模型接口（已由官方示例确认）**

```
POST {base_url}/compatible-mode/v1/systemone
authorization: {api_key}            # 注意：示例里没有 Bearer 前缀
content-type: application/json

{
  "model": "decision-model-preview",
  "state": {"ticket_id": "...", "content": "..."},
  "questions": {
    "<name>": {"type": "choice", "instructions": "...", "criteria": {"<label>": "<说明>"}},
    "<name>": {"type": "noul",   "instructions": "..."},
    "<name>": {"type": "score",  "instructions": "...", "criteria": ["...", "..."]}
  }
}
```

返回 `answers.<name> = {type, choice, confidence, probabilities}`（`noul`/`score` 型另有其字段）。
本期只用 `choice` 型。`state` 是自由对象，我们只放 `content`。

- 该 endpoint 不是 OpenAI 兼容格式，**不能复用 `hub/llm.py` 的 `ChatClient`**（它固定打 `/chat/completions`、
  固定发 `Bearer`）。需要独立的 `hub/decision.py`。
- `base_url` 与密钥存在 providers 表（你已加好），模型名取该行的 `model`。
- 学习环节要生成规则文本，决策模型做不到，必须用另一个生成式模型（默认复用当前回答模型）。

**工程约束**

- 测试不访问真实网络（`httpx.MockTransport`），新表在 SQLite 上用 `tempfile` 建库。
- SQL 同时兼容 SQLite 与 MySQL，差异收敛在 `store.py` 的占位符与建表语句。
- 设备端与 `server/` 不动；`hub/commands.py` 被本设计取代。
- 密钥不得进日志（沿用 `llm.py` 的错误体脱敏做法）。

## 4. 备选方案与取舍

**A. 只靠正则表**：后台可编辑正则，不引入模型。确定性强、零延迟，但长尾永远覆盖不到，
你说的「很多时候无法匹配」不会改善。否。

**B. 全量走决策模型**：每条话都调一次，正则只当缓存。实现最简单、判得最全，但每次提问都绑一个外部依赖，
模型挂了连普通问答都受影响，且正常提问白付一次调用。否。

**C. 三级路由 + 离线学习（选定）**：正则快路径 → 决策模型兜底 → 问答；学习任务把模型判过、
正则没接住的样本蒸馏成规则。满足「越用越聪明」，且模型故障时退化成今天的行为。**选它。**

**D. 把意图判定与现有联网规划合并成一次模型调用**：少一次往返，但两者模型、协议、失败语义都不同
（联网规划是 DeepSeek + JSON mode，判定是 systemone），合并会互相拖累。本期不做，记为后续可能。

## 5. 设计

### 5.1 数据模型

`hub/store.py` 新增三张表，两张既有表加列。所有播种/迁移必须幂等（按自然键 upsert，重复启动不改数据）。

**`intents`** — 意图注册表。key 归代码，文案与开关归后台。

| 列 | 说明 |
|---|---|
| id | 主键 |
| `key` | 唯一，如 `switch_target` / `reset_context` / `list_capabilities` |
| name | 中文名，用于后台与播报 |
| instructions | 给决策模型的判定说明（后台可改） |
| param_kind | `none` / `entity` |
| enabled | 0/1 |
| priority | 排序 |

三个 key 必须与 `hub/intents.py` 里的 handler 注册表一一对应。启动时 `sync_intents()` 从代码常量 upsert：
新增 key 插入，已有 key 只更新 name/instructions 的默认值（后台改过的文案不覆盖，用 `instructions` 非空判断），
**绝不因为表里有未知 key 就去执行它**。

**`decision_rules`** — 正则表。

| 列 | 说明 |
|---|---|
| id | 主键 |
| intent_key | 指向 intents.key |
| pattern | `re.fullmatch` 模式，匹配前会先做长度判断 |
| max_text_len | 该规则适用的最大文本长度（按 `_normalize` 之后的字符数） |
| source | `manual` / `learned` |
| status | `active` / `disabled` |
| priority | manual 默认 100，learned 默认 50；相同时按 id 升序 |
| hit_count / recheck_count / mismatch_count | 命中数 / 被复核次数 / 复核不一致数 |
| note | 备注（后台可见） |
| created_ts / last_hit_ts | 时间戳 |

规则模式里**只写句式，不写实体名**。带参数的意图用**一个**命名分组 `(?P<target>...)` 捕获，
运行时再去 personas/providers 里解析（先角色后模型，与今天的优先级一致）。这样改角色名、加角色都不需要动规则。

播种两条 manual 规则（等价于今天的 `commands.py`）：

```
^(?:忘掉刚才的对话|忘记刚才的对话|换个话题|重新开始)$        -> reset_context, max_text_len=16
^(?:切换到|切换成|换成|改成|换到|用)(?P<target>.+?)(?:模式|性格|风格|模型|的)?$ -> switch_target, max_text_len=24
```

`hub/commands.py` 删除，`tests/hub/test_commands.py` 的用例移植为 `test_rules.py` 的用例。

**`capabilities`** — 能力清单，`list_capabilities` 的播报内容与学习器的输入都来自这里。

| 列 | 说明 |
|---|---|
| id / name / description / sort / enabled | |

播种时只写**已实现**的能力：切换角色或音色、切换回答模型、忘掉刚才的对话。新增能力 = 加一行 + 注册 handler，
不能只加一行（否则会播报做不到的事）。

**`turns` 加列**（两个后端都要迁移）

| 列 | 说明 |
|---|---|
| `route` | `''`（历史行）/ `rule` / `model` / `none`。`model` = 决策模型被调用过（结论可能是命令也可能是问答）；`none` = 没调（长度闸门、超时、总开关关闭） |
| `intent_key` | 意图 key；决策模型判成普通问答时写 `qa` |
| `rule_id` | 命中的规则 id，可空 |
| `decision` | JSON 文本：`{confidence, probabilities, params, recheck:{choice,confidence}}` |
| `learn_state` | `''`（历史）/ `new` / `used` / `skipped` |
| `corrected` | 0/1，命中后用户立刻纠正 |

加索引 `(learn_state, ts)`，学习任务按它取样本。

**新增 settings**（默认值）

| key | 默认 | 说明 |
|---|---|---|
| `decision_provider_id` | 空 | 指向 providers 表里百炼那条 |
| `decision_enabled` | `1` | 总开关 |
| `decision_timeout_ms` | `800` | **两次判定合计**的超时预算（不是每次调用各自 800ms） |
| `decision_gate_len` | `40` | 超过此长度直接走问答，不调模型 |
| `decision_min_confidence` | `0.6` | 低于此值按问答处理 |
| `decision_confirm_band` | `1` | 置信度落在 `[min_confidence, 0.75)` 时播确认而不执行；设 0 则一律按 `min_confidence` 直接执行 |
| `decision_recheck_ratio` | `0.05` | 规则命中后抽样复核比例 |
| `learn_gen_provider_id` | 空（回退当前回答模型） | 生成规则用的模型 |
| `learn_max_samples` | `200` | 单次学习最多取多少条 |

### 5.2 运行时流程

`ChatService.answer()` 现在是：`parse_command` → 命令 or `_qa`。改为：

```
norm = normalize(text)                       # 去标点、去「小爱同学」前缀，沿用现有 _normalize
cmd  = ruleset.match(norm)                   # 纯内存，按 priority DESC/id ASC
if cmd:               route='rule'
elif decision_enabled and len(norm) <= decision_gate_len:
    cmd, meta = await decide(norm)           # 两阶段共享 decision_timeout_ms 的预算
    route = 'model'                          # 调过就是 model，结论可能是命令或普通问答
else:                 route='none'           # 没调模型（闸门拦下 / 开关关闭）
```

超时或报错时拿不到结论，同样记 `route='none'`。`route` 只描述「决策来自哪里」，判没判成命令看
`intent_key`（`qa` 表示调过模型但判成普通问答）。这个区分是学习环节取负例的唯一依据，不能省。

- **规则匹配**：先比 `len(norm) <= max_text_len`，再 `re.fullmatch`。命中后解析参数：`entity` 型取
  `target` 分组，按 `_find`（name/aliases，casefold）先查 personas 再查 providers；**解析不到就当作没命中**，
  继续下一条规则（不直接给问答）。全部不中才进模型。
- **决策模型两阶段**：
  - 第一次只问意图：`questions.intent = {type: "choice", instructions: "…", criteria: {<intent.key>: <intent.instructions>, …, "qa": "普通提问、闲聊、创作、翻译，或没有明确控制动作的话"}}`。
    `choice == "qa"` 或 `confidence < decision_min_confidence` → 问答。
  - 仅当所选意图的 `param_kind == "entity"` 时再问一次目标：
    `questions.target = {type: "choice", criteria: {<角色名>: "音色/说话风格", …}}`，providers 同理另起一组标签
    （标签冲突时加前缀 `model:`）。任一次调用超时/报错/返回不合法 → 整条按问答处理。
  - 第二次调用选中的实体名（或其别名）必须**在原话里出现过**（casefold 子串）；否则视为幻觉，按问答处理。
- **低置信播确认**：置信度落在 `[decision_min_confidence, 0.75)` 且意图会改状态时，播「你是要切换到银月吗」
  并记录 `intent_key`，不执行；下一条用户确认（「对」「是」「嗯」）才执行。确认只对最近 30 秒内的一条待确认生效。
  待确认状态放在 hub 进程内、hub 全局唯一：本设备是「一台音箱、一家人」的场景，`session_id` 是
  `BACKEND_native_<时间戳>` 的唤醒会话号，确认往往发生在下一次唤醒里，按会话号反而接不上，所以不按它区分。
  hub 重启即丢，可接受。
- **一句话多个实体**：只执行第一个匹配到的，其余写入 `decision` JSON 的 `ignored` 字段并记日志。
- **抽样复核**：`route == 'rule'` 时按 `decision_recheck_ratio` 概率异步调一次意图判定（只比意图，不比实体），
  结果写进该 turn 的 `decision.recheck`，与规则不一致则 `decision_rules.mismatch_count += 1`。
  复核必须 fire-and-forget，不得拖慢首块 PCM。
  **自动停用**：按**复核样本**算，不按命中数算——`recheck_count >= 10` 且
  `mismatch_count / recheck_count >= 0.25` 时置 `disabled` 并记日志。
  新上的 `learned` 规则在 `hit_count < 50` 时复核比例提到 20%，之后回落到 `decision_recheck_ratio`
  （5% 的抽样要上百次命中才凑得出结论，不能作为上线初期的唯一保险）。
- **纠正检测**：一条命令之后 60 秒内，下一条话（hub 全局，同样不区分设备）若是同一意图但参数不同，或匹配
  `^(不对|不是|我说的是|搞错)`，把上一条 `corrected = 1`。
- **落库**：所有路径都写 `turns`。`route IN ('rule','model')` 写 `learn_state='new'`（有标签，可供学习）；
  `route='none'` 写 `skipped`（没有标签，学习任务不碰）。

`_QUESTION_RE`（含「吗/什么/怎么」就判非命令）**从决策链里删除**，否则「能切换成银月吗」永远进不来。
问句式**不做特殊处理**：「换成毒舌吗」「换成毒舌模式好吗」都会被当成命令直接执行（你的决定：减少交互次数，
误切换一句话就能改回来）。原有 `tests/hub/test_commands.py:40-41` 的两条「不算命令」断言随之反转为
「执行切换」。

### 5.3 学习任务

入口先做 HTTP：`POST /api/v1/decision/learn`，请求头 `X-Device-Token`（复用现有 `device_token`），
返回 JSON 摘要；`/admin/rules` 页面上有个按钮调同一个函数。**不做调度。**

算法：

1. 取 `learn_state='new'` 的最近 `learn_max_samples` 条。
2. 正例 = `route IN ('rule','model')` 且 `intent_key` 非空、不是 `qa`、`corrected=0`；
   负例 = `route='model'` 且 `intent_key='qa'` 的文本（模型确实调过、且判成普通问答）。
3. 按意图分组，每组的正例去重后最多取 30 条，负例随机取 30 条，连同：
   - 该意图的 `instructions`、`param_kind`；
   - 当前 personas/providers 的 name+aliases；
   - 已有 `active` 规则的模式与该意图下已正确命中的样例（≤20 条）；
   一起发给生成模型（JSON 模式，非流式），要求返回：
   ```json
   {"rules": [{"intent_key": "switch_target", "pattern": "...", "max_text_len": 18}]}
   ```
   提示词里写死约束：`fullmatch` 语义、最多一个 `(?P<target>...)`、禁用 lookaround/反向引用/嵌套量词、
   长度 ≤ 200 字符、必须含中文、不要写死实体名。
4. **代码侧校验（模型输出一律不可信）**：
   - `re.compile` 成功；模式只含白名单语法（字面量、字符类、分组、`?`/`*`/`+`/`{m,n}`、`|`、`^$`、`\s` 等）；
   - 至少命中该组 50% 的正例；
   - **不得命中负例全集**：最近 30 天内所有 `route='model' AND intent_key='qa'` 的文本（上限 500 条），
     不是只校验提示词里抽的那 30 条——只验 30 条，规则今天能过、明天就可能吞掉别的短问句；
   - 与该意图下已有 active 规则不重复（在样本语料上行为等价即拒绝）；
   - `max_text_len = clamp(max(观测到的正例最大长度 + 6, 模型给的值), 8, 40)`。
   校验不通过的一律丢弃并记原因。
5. 通过的新规则 `source='learned'`、`status='active'`、`priority=50` 入库。
6. 处理过的样本 `learn_state='used'`；被排除的 `skipped`。
7. 返回摘要：扫描数、每个意图的正/负例数、提出/采纳/拒绝条数与拒绝原因。

**可选（默认关闭）**：从 `target` 分组里收集近似写法（与现有别名编辑距离 ≤1 且长度 ≥2）追加到该实体的 aliases，
用于吸收 ASR 同音错字。风险是污染别名，所以默认关，后台开。

### 5.4 后台

- `/admin/intents`：意图列表，可改 name/instructions/enabled；能力清单的增删改也在这里（或独立小节）。
- `/admin/rules`：规则列表（意图、模式、来源、状态、长度上限、命中/不一致数），可新增/编辑/停用/删除，
  「立即学习」按钮显示返回摘要。
- `/admin/history` 增列显示 route 与 intent。

### 5.5 失败与降级

| 情况 | 行为 |
|---|---|
| 决策模型超时/5xx/返回坏 JSON | 按问答处理，`learn_state='skipped'`，日志记原因，不抛异常 |
| 规则命中但实体解析不到 | 视为未命中，继续下一条规则 |
| 决策选中的实体不在原话里 | 视为未命中 |
| 置信度低于阈值 | 按问答处理 |
| 学习用的生成模型失败 | 接口返回 500 + 原因，样本保持 `new`，可重跑 |

### 5.6 测试

- `tests/hub/test_decision.py`：`MockTransport` 断言请求体（url 路径、`authorization` 无 Bearer、`state.content`、
  `criteria` 的 key 集合），解析 `choice/confidence/probabilities`，超时/4xx/坏 JSON 的降级。
- `tests/hub/test_rules.py`：长度闸门、`fullmatch` 语义、优先级、参数解析（精确/别名/casefold/解析不到继续）、
  多实体只执行第一个。移植原 `test_commands.py` 用例。
- `tests/hub/test_learn.py`：假生成模型返回固定正则；采纳、拒绝（正则非法、命中负例、与已有规则重复、
  命中率不足）、`max_text_len` 计算、重复运行幂等、`learn_state` 迁移。
- `tests/hub/test_chat.py`：`answer()` 三条路径（rule / model / qa）各一条，断言决策模型没被调 / 被调一次 /
  超时后仍返回问答音频。`ChatService` 增加可注入的 `decision` 参数（与现有 `web_ground` 一致）。
- **黄金语料** `tests/hub/fixtures/routing_cases.json`：约 30 条真实口吻的句子 + 期望 route/intent。
  `rule` 与 `none` 两类由 unittest 直接断言（纯本地、无网络）；`model` 类在 unittest 里只能配桩，断言的是桩本身，
  等于自证，所以另写 `scripts/eval_routing.py`（不在 `tests/` 下，手动执行、允许联网）读同一份语料打真实接口，
  输出逐条对比与准确率。第一次人工跑学习后补真实样本。
- `docs/status.md` 同步本节能力；交付 `docs/testing/2026-11-24-decision-routing.md` 记录实际验证。

## 6. 已确定的取舍（你拍的）

- 模板 + 实体槽，不学裸正则，不把实体名写死。
- 问句式的命令**直接执行**，不额外播确认（你的决定：减少交互次数，误切换可一句话改回）。
- 学到即生效（`active`），靠 `mismatch_count` 自动停用，不做人工待审队列。
- 长度闸门 + `fullmatch`，长问句不进识别。
- 规则命中按 5% 抽样调决策模型复核，成本接受。
- 一句话点多个实体只执行第一个，其余记日志。
- 学习先只暴露 HTTP 接口，人工触发；调度方式后续再定，不影响本设计。

## 7. 留给实现者决定

- `hub/decision.py` 客户端的具体结构（建议 `DecisionClient(base_url, api_key, model, transport=None, timeout=…)`
  与 `decide()` 门面函数，风格对齐 `hub/web.py`）。
- 规则集是否加进程内缓存（先直接每请求查库，规则数很少；有性能问题再加）。
- 后台页面的 HTML 细节（沿用 `admin.py` 现有表单风格）。
- 中文播报文案。
- 确认态（低置信播确认）的状态存哪里（建议 settings 里一条 JSON，30 秒过期）。

## 8. 体量

项目级改动：三张新表 + `turns` 六列迁移、两个新模块（`hub/decision.py`、`hub/learn.py`）、
`hub/rules.py` 与 `hub/intents.py` 取代 `hub/commands.py`、两个后台页、一个 HTTP 接口、
五个测试文件。设备端与 `server/` 零改动。
