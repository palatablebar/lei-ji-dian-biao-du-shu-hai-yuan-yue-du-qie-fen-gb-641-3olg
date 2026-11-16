# 深度登记 meterback（累计电表读数还原与月度切分）

- 规模：中型（8 个功能模块 / 8 个应用页 / 102 条测试）
- 核心规则：**区间还原 + 按日权重月度切分**。读数按 `(表号, 寄存器, UTC 时间)` 排序后逐对还原：区间用量 =（本次 − 上次）× 综合倍率，满量容量翻转时补容量，换表处拆成「旧表末段 + 新表首读起算」两段，估抄行不作锚点但给区间打标记；区间用量按覆盖天数切到自然月（`[月初, 下月初)` 半开），余数用最大余额法分到 0.001 kWh，同月「总表 = 峰 + 平 + 谷」的残差并入最大分量。
- 使用者与工作流：临江供电所抄表班 4 人、辖区 8 只示例表（实际 320 只），月度线损与电费原先靠 Excel 手拉；现在一次处理得到「区间用量 + 月度分摊 + 电费」，每个数字都能追回原始行号与原因码。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 读数采信 | 导入样例，看逐行诊断、编码与分段 | POST /api/readings/import、页面 /readings | `app/modules/m1_ingest/service.py` `app/modules/m1_ingest/reader.py` `app/modules/m1_ingest/decode.py` `app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py` |
| M2 表计契约 | 看容量、倍率、寄存器、单位与电价，试跑一行解析 | GET /api/contract、POST /api/contract/preview | `app/modules/m2_contract/contract.py` `app/modules/m2_contract/coerce.py` `app/modules/m2_contract/units.py` | `tests/test_m2_contract.py` |
| M3 质量隔离 | 看原因码分布与隔离行，导出隔离区 CSV | GET /api/quarantine、GET /api/quarantine.csv | `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` | `tests/test_m3_quality.py` |
| M4 区间还原 | 重算区间，看翻转、换表、估抄标记 | POST /api/intervals/rebuild、页面 /intervals | `app/modules/m4_intervals/restore.py` `app/modules/m4_intervals/rollover.py` `app/modules/m4_intervals/swaps.py` | `tests/test_m4_intervals.py` |
| M5 月度切分 | 重算分摊与账单，看每月合计与尾差 | POST /api/months/allocate、页面 /months | `app/modules/m5_months/allocate.py` `app/modules/m5_months/weights.py` `app/modules/m5_months/tou.py` | `tests/test_m5_months.py` |
| M6 批次与对账 | 跑全流程、查批次历史、看四账、失败重试 | POST /api/runs、GET /api/batches、GET /api/reconcile、页面 / 与 /batches | `app/modules/m6_batches/service.py` `app/modules/m6_batches/compare.py` `app/modules/m6_batches/history.py` `app/modules/m6_batches/resume.py` | `tests/test_m6_batches.py` |
| M7 导出 | 导出月度分摊、区间、隔离、读数证据、运行清单 | GET /api/export/monthly.csv、GET /api/export/manifest.json、页面 /export | `app/modules/m7_export/evidence.py` `app/modules/m7_export/csv_out.py` `app/modules/m7_export/manifest_out.py` | `tests/test_m7_export.py` |
| M8 多来源包 | 收件、逐来源采信、补件、覆盖核对、版本发布 | POST /api/packages、POST /api/packages/{id}/sources、POST /api/packages/{id}/publish、GET /api/coverage、页面 /packages 与 /sources | `app/modules/m8_package/service.py` `app/modules/m8_package/sources.py` `app/modules/m8_package/coverage.py` | `tests/test_m8_package.py` |

端到端：`tests/test_e2e.py`（脏样例 → 六页 200 → 四账 → 导出，含 4xx 失败输入与幂等重跑）。
不变量：`tests/test_invariants.py`（区间账 = 月度账、幂等重跑行数不变、月桶划分、账单闭合、读数分区）。

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 国产抄表软件导出的 GB18030 文件被声明成 UTF-8 | `open()` 直接抛 UnicodeDecodeError，或中文表头全乱码 | 声明只作首个候选，严格解码失败按 utf-8-sig → utf-8 → gb18030 回退，留 `K_CHARSET_FALLBACK` | `app/modules/m1_ingest/decode.py` | `tests/test_m1_ingest.py::test_gbk_headers_decoded_by_fallback` |
| TRAP-02 | Excel 另存的文件头带 UTF-8 BOM | 首列名变成 `\ufeffmeter_id`，按列名取值全空 | 解码即剥离 BOM，并对首格再 strip 一次，留 `K_BOM_STRIPPED` | `app/modules/m1_ingest/decode.py`、`app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py::test_bom_stripped_before_header_mapping` |
| TRAP-04 | 两次导出拼接，中间又出现一行表头 | 表头被当成一条读数，列数校验误报 | 逐格比对表头签名，命中即分段 +1 并跳过该行 | `app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py::test_repeated_header_split_into_segments` |
| TRAP-05 | 行尾多一个分隔符、列数与表头不一致 | 静默补空值，或后续列全部错位 | 严格列数校验，整行进隔离区并保留原始行文本 | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_column_count_mismatch_isolated_with_lineno` |
| TRAP-06 | `1,212.5`、`３，０５０.0` 这类千分位与全角写法 | `Decimal()` 抛异常，或整列变成空值 | NFKC 折半角 + 去千分位 + 去单位后缀，再 Decimal 化 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_thousands_and_fullwidth_normalized` |
| TRAP-07 | `-` / `N/A` / `未知` 混在读数里 | 被当成 0 参与区间计算，凭空多一段零 | 哨兵清单集中定义，返回 None；`0` 仍是有效读数 | `app/modules/m2_contract/coerce.py`、`app/config.py` | `tests/test_m2_contract.py::test_sentinel_reading_is_three_state` |
| TRAP-08 | 表号 `007` 被当成数字 7 | 对不上契约，或换表链断掉 | 全链路按字符串保形，`007` 与 `7` 是两个表号 | `app/modules/m2_contract/contract.py` | `tests/test_m2_contract.py::test_meter_id_leading_zero_preserved`、`tests/test_m3_quality.py::test_unknown_meter_and_leading_zero_mismatch` |
| TRAP-10 | 0.001 kWh 刻度上累加几十行 | float 累加掉尾差，月度合计差 0.001~0.01 | 全链路 Decimal，量化只在切分与账单两处显式发生 | `app/modules/m5_months/weights.py` | `tests/test_m5_months.py::test_decimal_totals_exact` |
| TRAP-11 | 同一时刻写成 `2026-01-01T00:00:00+08:00` 与 `2025-12-31T16:00:00Z` | 落进两个时间桶，区间多一段零用量 | 先归一 UTC 再排序，无偏移按 +08:00 处理 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_timezone_offsets_collapse_to_same_instant` |
| TRAP-14 | 同一 (表号, 寄存器, 时间) 两条不同读数 | 后写覆盖，前一条消失且不留痕 | 两条都进隔离区，带各自原值与行号，不猜哪条为准 | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py::test_duplicate_key_conflict_isolates_both_rows` |
| TRAP-15 | 同一块表一条 kWh 一条 MWh | 差值差 1000 倍，区间用量离谱 | 读数先折成契约单位，用量再折 kWh，单位跟着数字走 | `app/modules/m2_contract/units.py` | `tests/test_m2_contract.py::test_unit_mixing_normalized_to_contract_unit` |
| TRAP-16 | 传输截断，末行只剩半截且没有换行符 | 半行被当有效读数，凭空多一段 | 末行无换行 + 列数不齐 → `Q_TRUNCATED_ROW`，文件级留 `K_TRUNCATED_TAIL` | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_truncated_tail_row_isolated` |
| TRAP-18 | 0 字节文件、只有表头、数据行全是空行 | 三种都返回「成功导入 0 行」，或直接除零 | 三个文件级原因码分开返回，不落半批数据 | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_empty_states_return_distinct_codes` |
| TRAP-23 | 区间止点与下一段起点都算一次 | 相邻区间重复计入一天，月度用量偏高 | 区间与月桶都声明左闭右开 `[起, 止)`，相邻段首尾相接 | `app/modules/m5_months/weights.py`、`app/modules/m4_intervals/restore.py` | `tests/test_m5_months.py::test_month_slices_are_half_open`、`tests/test_m4_intervals.py::test_intervals_are_half_open_and_contiguous` |
| TRAP-24 | 各月/各分量各自四舍五入后合计不等 | 分完差 0.001~0.02，账永远差几分 | 最大余额法分月；分量残差并入最大分量；账单先合计再舍入，分项补差 | `app/modules/m5_months/weights.py`、`app/modules/m5_months/tou.py` | `tests/test_m5_months.py::test_largest_remainder_keeps_total_exact`、`tests/test_m5_months.py::test_tou_components_closed_to_total` |
| TRAP-25 | 多个终端同批交件，其中一个文件打不开或表头坏 | 整包退回拖住正常终端；或静默跳过、发布一个「看起来齐了」的结果 | 文件级失败只拒绝该来源（`rejected`），部分坏行标 `partial`，正常来源照常合并发布；整包无可采信行才 `E_NO_USABLE_SOURCE` | `app/modules/m8_package/service.py`、`app/modules/m8_package/sources.py` | `tests/test_m8_package.py::test_bad_source_does_not_block_the_package`、`tests/test_m8_package.py::test_package_with_no_usable_source_is_rejected` |
| TRAP-26 | 补件重跑把别的终端同表计的结果覆盖掉 | 后跑的批次直接replace整批，先前终端的行消失 | 补件只替换自己那个来源的快照；来源范围（scope）外的行隔离为 `Q_SOURCE_OUT_OF_SCOPE`，不改写别人 | `app/modules/m8_package/service.py`、`app/store.py` | `tests/test_m8_package.py::test_supplement_replaces_only_its_own_source`、`tests/test_m8_package.py::test_supplement_out_of_scope_is_isolated` |
| TRAP-27 | 补件发布失败（写库中途出错）把旧版结果写坏 | 先删旧行再插新行，失败留半批数据，页面四账全崩 | 结果行 + 发布指针同一事务；失败整体回滚，`published_version` 与旧版逐字段不变，且不占版本号 | `app/store.py::publish_version`、`app/modules/m8_package/service.py` | `tests/test_m8_package.py::test_publish_failure_mid_transaction_rolls_back_fully`、`tests/test_m8_package.py::test_publish_failure_keeps_previous_version` |
| TRAP-28 | 原件被新导出覆盖、旧文件无法恢复，历史版本无法复算 | 重算读工作目录，数字变了；核对时找不到当时那份字节 | 收到即按 sha256 落 `source_blobs`，重算只从快照读；补件追加新快照，旧快照保留 `superseded_by` | `app/modules/m8_package/sources.py`、`app/store.py` | `tests/test_m8_package.py::test_original_file_overwrite_does_not_affect_recompute`、`tests/test_m8_package.py::test_supplement_history_keeps_old_snapshot` |
| TRAP-29 | 复核问「这段数据来自哪个文件」「哪只表没交」 | 只有总数，无法逐段定位；缺源表计被当成零用量混过去 | 读数/区间/隔离/分摊都带来源标识；覆盖表按契约表计逐只算四态（covered/single_reading/quarantined_only/missing），预期来源缺件单独列出 | `app/modules/m8_package/coverage.py`、`app/modules/m4_intervals/restore.py` | `tests/test_m8_package.py::test_every_row_carries_its_source`、`tests/test_m8_package.py::test_interval_records_both_endpoints_sources`、`tests/test_m8_package.py::test_missing_meter_is_listed_with_expected_source` |

## 对账规则

- 读数账：原始数据行 = 有效读数 + 估抄 + 行级隔离行 + 空行（样例 60 = 45 + 1 + 14 + 0）。多来源包下文件级拒绝的来源没有数据行，不进两侧，只在说明列单列个数。
- 区间账：主寄存器（总表优先）区间用量合计 = 月度分摊用量合计（0.001 kWh，逐位相等）。
- 月度账：同月同表「总表 = 峰 + 平 + 谷」，闭合后的残差必须为 0，闭合前的尾差条数单独计数。
- 账单账：账单金额 = 分项金额之和（分项先截尾再补分），四账在 `/` 与 `/batches` 页、`/api/reconcile`、运行清单里同源显示。
- 覆盖口径：按契约表计逐只算，`missing` / `quarantined_only` / `single_reading` 计入「需关注」，不当作零用量。

## 反例与不变量

- 反例（朴素实现会挂）：`tests/test_m4_intervals.py::test_naive_subtraction_would_drop_rollover`（直接相减得负数，翻转段被丢）、`tests/test_m5_months.py::test_bill_rounding_closed_not_naive`（逐项四舍五入多 1 分）、`tests/test_m1_ingest.py::test_naive_splitlines_would_break_records`（带千分位的单元格被切成两列）。
- 不变量：`tests/test_invariants.py::test_conservation_intervals_equal_months`（守恒）、`tests/test_invariants.py::test_idempotent_run_keeps_rows`（幂等）、`tests/test_invariants.py::test_month_slices_partition_every_interval`（划分）、`tests/test_invariants.py::test_bill_components_sum_equals_amount`（账单闭合）、`tests/test_invariants.py::test_readings_partition_holds`（分区）。
- 端到端：`tests/test_e2e.py::test_full_pipeline_over_http`、`tests/test_m8_package.py::test_http_package_flow`（多来源包走完整 HTTP）。

## 已知不覆盖

- 只支持单表计的单向有功读数；双向表（正/反向分别累计）、需量寄存器、无功寄存器一律按 `Q_BAD_REGISTER` 隔离。
- 电价固定读 `sample_data/prices.csv`（峰 1.05 / 平 0.65 / 谷 0.32 元每 kWh），没有分时日历、阶梯电价与容量电费。
- 换表链只支持一段（旧表 → 新表）；链式多次换表能识别但只在最后一段起算，中间段会缺口。
- 时区按固定 +08:00 处理，不引入 DST；跨时区台区不支持。
- 并行抄表同一时刻多条读数只按「取值是否一致」判定冲突，没有设备侧时钟偏移校正。
- 页面只做展示与触发，没有登录与权限；批次历史最多展示 20 条。
- 大文件未做分块导入（样例 60 行；按 320 只表月均 700 行估算，单文件仍在内存可接受范围）。
- 多来源包的冲突判定仍是「同键取值是否一致」：两个终端对同一时刻给出不同读数时两条都隔离，不做设备时钟偏移校正；「谁对」由人工复核后作废来源或补件处理，系统不猜。
- 作业包的来源范围（scope）首件按文件里出现的表计自动推定；如果某个终端首件就漏交了一部分表计，后续补件补这些表计会被 `Q_SOURCE_OUT_OF_SCOPE` 拦下，需显式调整该来源的范围（目前只能重建来源）。
- 包级并发发布没有锁：两个发布请求同时到达时，后提交的事务可能基于旧版本号重算；单机单使用者场景下可接受，多人同时发布需要外部串行化。
