# 夜班交接只剩每小时用量

清晨换班时，逐事件日志已被覆盖，只剩三小时的资源占用、完成计数和几段跨小时转移。复核组要从匿名汇总中还原作业组的路线、作业族和事件，再比较候选策略的反事实吞吐。入口为 `/app/warehouse_policy.py`，按 `TRACE_JSON RESULT_JSON LEDGER_JSONL` 三个位置参数调用。

## 交上来的班次表

`/app/data/trace.json` 是默认现场，同目录另有最小和最大快照；程序只使用镜像内自带依赖。
班次表以 UTF-8 文本保存，记为 JSON；`schema` 固定为 `warehouse-shift-aggregate/v3`，根键恰好为 `schema`、`snapshot_id`、`run_id`、`resources`、`crews`、`jobs`、`hourly`、`transition_support`、`strategies`；身份号匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{2,63}`。

`resources` 恰好八项，每项只有 `resource_id` 和 `role`；编号匹配 `RC-[A-Z0-9]{4}` 且唯一，角色为 `entry` 的恰好四项、为 `shared` 的恰好四项。`crews` 恰好四项，每项只有 `crew_ref` 和 `job_refs`，编号匹配 `CRW-[A-Z0-9]{3}`；每个 `job_refs` 恰好两项、按字典序排列并恰好划分全部作业。

`jobs` 恰好八项，每项只有 `job_ref`、`crew_ref`、`release_hour`；编号匹配 `JOB-[A-Z0-9]{5}` 且唯一，每组两项，`release_hour` 为 `0` 或 `1`。`hourly` 恰好三项，`hour` 依次为 `0`、`1`、`2`；每项只有 `hour`、`busy_resources`、`completed_count`，资源数组无重复且按字典序排列，计数为 `0` 到 `8` 的整数。

`transition_support` 每项只有 `cut_hour`、`from_resource`、`to_resource`、`count`；`cut_hour` 为 `0` 或 `1`，两端资源必须登记，`count` 为正整数，同一四元组不重复。某个已列小时给出该小时完整的非零转移集合。
`strategies` 有两至三项，每项只有 `strategy_id`、`start_hour`、`horizon_hours`、`job_refs`、`family_order`；策略号为 `STR-[A-Z0-9]{4}`，窗口起点为 `0` 到 `2`、长度为 `1` 到 `4`，作业数组非空且唯一，族顺序是 `FAM-A` 至 `FAM-D` 的排列。

所有记录和数组项必须是对象；数字不得写成字符串或布尔值。重复键、截断 JSON、非 UTF-8、缺失键、额外键、未知资源或未登记引用均拒绝，不进入恢复。

## 恢复与吞吐

每个作业组共享一条 `[entry, shared]` 路线，但组与资源、组与族的对应没有交出来。八小时负载已不存在，只能把三小时 `busy_resources` 的并集逐项累计：每个资源应出现两次。
先用访问总数约束选出未分配资源，再依据已列转移把入口资源接到唯一共享资源；仍有多个合法路线时，将路线按字典序排好，依次配给按 `crew_ref` 排好的作业组。

路线排序后，最小的作业组归 `FAM-A`，随后到 `FAM-D`。每项作业沿本组路线执行两个连续的一小时阶段，第一阶段的开始小时不得早于 `release_hour`；同一路线上的两项作业按 `job_ref` 先后执行。恢复出的四组事件必须恰好覆盖三小时负载、两项完成计数和三处转移核对，否则返回 `RECOVERY_INFEASIBLE`。

反事实模拟重新从策略起点排工，不再沿用已发生班次的开始小时；作业在 `start_hour + release_hour` 后可用。每个小时按 `family_order` 的名次、再按 `job_ref` 尝试作业：路线两项资源在连续两小时都空闲时立即占用并完成，否则等到下一小时。
完成时刻等于窗口结束仍计入，`throughput_per_hour` 是完成数除以窗口长度并按十进制 half-up 保留三位小数。

## 交回与台账

结果根键恰好为 `schema_version`、`snapshot_id`、`run_id`、`input_digest`、`crews`、`events`、`simulation`。`schema_version` 为整数 `1`，`input_digest` 是现场原始字节的小写 SHA-256。`crews` 按族号排列，每项只有 `crew_ref`、`family_id`、`resources`；`events` 按 `(hour, resource_id, job_ref)` 排列，每项只有 `job_ref`、`crew_ref`、`family_id`、`stage`、`hour`、`resource_id`；`simulation` 按策略号排列，每项只有 `strategy_id`、`completed_count`、`throughput_per_hour`、`last_finish_hour`。

结果只认紧凑 JSON：键名升序、无多余空格、末尾恰好一个换行。复核账每行是 UTF-8 对象，键恰好为 `run_id`、`snapshot_id`、`input_digest`、`result_digest`、`previous_hash`、`entry_hash`；首行前链为六十四个零，后续前链取上一行 `entry_hash`，本行摘要按去掉 `entry_hash` 后的规范字节计算。同一运行号、同一输入重跑必须保持结果和账本字节不变；同号异输入返回 `LEDGER_CONFLICT`，新运行号只能追加同一现场的新链尾。

成功时标准输出只有 `OK <run_id>\n`，标准错误为空。业务拒收退出码为 `3`，标准输出为空，标准错误只有一行 `REFUSED <代码>\n`；公开完整输出为 `REFUSED BAD_INPUT\n`、`REFUSED RECOVERY_INFEASIBLE\n`、`REFUSED LEDGER_CONFLICT\n`、`REFUSED LEDGER_CORRUPT\n`。解析、恢复或账本核对失败时，旧结果和旧账本保持原状；账本空行、重复键、未知键或断链均按损坏处理。

默认现场应在数秒内结束。公开上限固定为四个作业组、八项作业、八个资源和三个时段，从读取到发布必须在五秒内完成。
