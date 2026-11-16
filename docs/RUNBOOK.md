# 运行手册 meterback

## 1 启动

```bash
cd projects/gb-641
docker compose up -d --build
curl -sS http://localhost:18200/api/health
```

页面：<http://localhost:18200/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18200；SQLite 落在命名卷 `meterback-data` 的 `/data/meterback.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。
7. 若这次关账由多个采集终端分头交件（含打不开或表头坏的文件），走「来源采信」页：建包 → 逐件收件 → 发布版本；再在「来源追溯」页核对每段数据的来源与缺源表计。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18200/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18200/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18200/api/export/monthly.csv
```

多来源包：

```bash
PKG=$(curl -sS -X POST http://localhost:18200/api/packages -H 'Content-Type: application/json' \
  -d '{"expected_sources":[{"filename":"readings_terminal_a.csv"},
                           {"filename":"readings_terminal_b_truncated.csv"},
                           {"filename":"readings_terminal_c_bad_header.csv"}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["package_id"])')
for f in readings_terminal_a.csv readings_terminal_b_truncated.csv readings_terminal_c_bad_header.csv; do
  curl -sS -X POST http://localhost:18200/api/packages/$PKG/sources \
    -H 'Content-Type: application/json' -d "{\"sample\":\"$f\"}" >/dev/null
done
curl -sS -X POST http://localhost:18200/api/packages/$PKG/publish \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool
curl -sS http://localhost:18200/api/coverage | python3 -m json.tool
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **多来源关账（推荐路径）**：`/packages` 页建包 → 逐件收件（坏件也收，状态记「文件级拒绝」）→ 发布。发布后 `/sources` 页看每段数据来自哪个文件、哪只表缺源。
- **补件**：同一来源（同一文件名）再收一次即生成新快照；点「发布版本」后版本号 +1，其余终端结果不变。旧快照保留在来源历史里。
- **某个终端整份交错了**：`POST /api/packages/{id}/sources/{source_id}/retire` 作废该来源，下次发布不再参与，不影响其他来源。
- **发布失败**：接口返回 `E_PUBLISH_FAILED`，页面仍显示上一版（`published_version` 未变），旧版数据、导出与覆盖都可继续复核。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。（注意：清库会一起删掉 `source_blobs` 原件快照，历史版本就无法复算了。）

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 422 `E_NO_USABLE_SOURCE` | 作业包里所有来源都被文件级拒绝 | 先修好至少一个来源的表头/编码再发布；其余来源可先单独发布（作废坏来源或暂不收件） |
| 409 `E_PUBLISH_FAILED` | 发布写库中途失败，已整体回滚 | 看服务日志；旧版仍可复核与导出，修复后重试发布，版本号不会跳号 |
| 404 `E_VERSION_NOT_FOUND` | 查了不存在的版本号 | 用 `/api/packages/{id}/versions` 看现有版本 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；多来源包下按 `inputs[].source_id` / `snapshot_id` 对到具体终端那一次提交；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额；
5. 来源核对：`manifest.json` 的 `package.sources` 列出本版每个来源的状态与 sha256，`package.coverage` 列出每只表的覆盖状态；`readings.csv` / `intervals.csv` / `quarantine.csv` 都带来源列，可逐行追到文件。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `METERBACK_DB` | `/data/meterback.sqlite` | SQLite 路径 |
| `METERBACK_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
