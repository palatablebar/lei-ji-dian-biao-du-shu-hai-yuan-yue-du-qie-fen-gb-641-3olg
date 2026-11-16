# 数据定义 meterback

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`、`E_NO_USABLE_SOURCE`、`E_SOURCE_NOT_IN_PACKAGE`、`E_PUBLISH_FAILED`、`E_VERSION_NOT_FOUND`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`、`Q_SOURCE_OUT_OF_SCOPE`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`、`K_CROSS_SOURCE_ANCHOR`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 作业包与来源（多来源批次）

同一次关账可能收到多个采集终端的文件，正常件与坏件在同一个包里。**发布单位是包，采信单位是来源快照**。

| 概念 | 定义 |
|---|---|
| `package_id` | 一次关账的作业包，`pkg-` + 预期来源集合的摘要前 12 位 |
| `source_id` | 来源标识，由文件名派生（`src-` + sha256(文件名) 前 10 位）；同一终端补件沿用同一 `source_id` |
| `snapshot_id` | 来源的一次提交；文件内容按 sha256 存进 `source_blobs`，**永不覆盖** |
| 版本 `version_no` | 每次发布 +1，指向一个独立的 `batch_id`；已发布版本不可改写 |

来源快照三态：

| 状态 | 条件 | 对整包的影响 |
|---|---|---|
| `accepted` 可采信 | 解码成功、表头合格、没有行落进隔离区 | 不阻断 |
| `partial` 部分采信 | 解码与表头都合格，但有个别行进了隔离区（如末行截断） | 不阻断，好行照常参与 |
| `rejected` 文件级拒绝 | 0 字节 / 仅表头 / 全空行 / 表头缺列 / 编码不识 | 只有这一个来源被拒；整包里有别的可采信来源就照常发布 |

三条硬规则：

1. **坏来源不阻断整包**：只有「整包没有任何可采信数据行」才返回 `E_NO_USABLE_SOURCE`。
2. **补件只替换自己那个来源**：`POST /api/packages/{id}/sources` 给同一 `source_id` 追加快照，其余来源沿用当前快照重算，不覆盖别的终端。旧快照保留，`superseded_by` 记录被哪一版接替。
3. **发布失败保留旧版**：结果行与发布指针在同一个事务里写；任一步失败整体回滚，`published_version` 与旧版数据逐字段不变，原件快照仍能复算出同样的结果（不读工作目录）。

来源范围（scope）：首件按文件里出现的表计自动声明；补件超出范围的表计行进隔离区（`Q_SOURCE_OUT_OF_SCOPE`），不会改写其他终端的结果。

每行数据的来源追溯字段：

| 位置 | 字段 | 含义 |
|---|---|---|
| 读数 | `source_id` / `source_name` / `snapshot_id` | 唯一来源（同键重复行合并后，`source_ids` 列出全部提供者） |
| 区间 | `prev_source_id` / `cur_source_id` / `source_ids` | 两端锚点各自来自哪个来源；两端不同打 `K_CROSS_SOURCE_ANCHOR` |
| 隔离行 | `source_id` / `source_name` / `snapshot_id` | 坏行出自哪个文件 |
| 分摊 | `source_ids` | 参与该月该表该寄存器的区间来源集合 |

表计覆盖四态（按契约表计逐只算，不按收到的行数算）：

| 状态 | 含义 |
|---|---|
| `covered` 已覆盖 | 主寄存器至少一个锚点、至少一段区间 |
| `single_reading` 仅一锚点 | 只有一条有效读数，无法与上期构成区间 |
| `quarantined_only` 只有隔离行 | 有数据但全部被隔离，等于没交 |
| `missing` 缺源文件 | 没有任何来源提供这只表的读数 |

预期来源里没交件的用「缺失预期来源」单独列出，不混进正常结果。

