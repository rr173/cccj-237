# 领料批次切片加工器（req_slicer）

把旧系统导出的仓内动作流水，离线解释成：

1. **领料交付表**——每笔领料实际取了哪些到货批次的哪些原料段，带原始货龄；
2. **期末未消费切片**——按状态（在库 / 隔离 / 仍被领料占用）和持有单据分开列出；
3. **对账与断点报告**——数量守恒、原料段连续覆盖校验，以及每个物料的首个非法断点。

纯 Python 标准库实现，无第三方依赖，可重复跑；切片用整数区段表达，
**数量数百万的切片始终是少量对象，不会展开成一百万行**。

另有离线**召回影响分析**入口：给定召回的外部到货与区段，列出受影响的生产收料
及各自要冻结的区段，不写回生产系统。见 [docs/recall.md](docs/recall.md)。

另有离线**交接对账**入口：结算日给定凭证发布号上限，按调拨清单与签发/改签/作废
的签收凭证，列出哪些发货已签收、哪些待签收、哪些有争议；只生成清单、不动库存。
见 [docs/handover.md](docs/handover.md)。

另有离线**仓储计费**入口：财务输入带整数分钟的 JSON 流水、JSON 价目表和账期
`[start, end)`，按原收料与库位核对正常在库的两档件分钟与微元费用；不改库存、
不做跨仓归集或发票。见 [docs/billing.md](docs/billing.md)。

## 运行

```bash
# JSON 流水
python3 -I run_slicer.py run --input samples/flow_main.json --out-dir out/main

# 旧系统风格 CSV（中文表头、中文动作名也可识别）
python3 -I run_slicer.py run --input samples/flow_legacy.csv --out-dir out/legacy

# 召回影响分析（成功输出 JSON 清单；失败退出 1 且不输出清单）
python3 -I run_slicer.py recall --flow samples/recall_flow.json \
    --production samples/recall_production.json \
    --request samples/recall_request_ab.json

# 交接对账（发布号上限内取最高版本；只生成清单，不动库存）
python3 -I run_slicer.py handover \
    --flow samples/handover_dispute_flow.json \
    --handover samples/handover_dispute.json --publish-limit 1

# 离线仓储计费（按账期 [start,end) 核对正常在库费用，不动库存）
python3 -I run_slicer.py billing \
    --flow samples/billing_flow.json \
    --prices samples/billing_prices.json --start 3 --end 12
```

退出码：`0` 全部物料跑通；`1` 存在物料断点（断点前结果仍正常产出）；`2` 输入文件无法解析。

`recall` 子命令：`0` 成功（清单可为空）；`1` 输入/引用/工艺校验失败，此时只向
stderr 报错、不输出清单。详见 [docs/recall.md](docs/recall.md)。

`handover` 子命令：`0` 成功（清单可为空）；`1` 输入/引用/凭证版本/签收校验失败，
此时只向 stderr 报错、不输出清单。详见 [docs/handover.md](docs/handover.md)。

`billing` 子命令：`0` 成功（无占用时为空明细、零费用）；`1` 流水断点、时间/价目
非法、账期非法或应收费时间缺价，此时只向 stderr 报错并定位动作或库位与分钟时段、
不输出账单。详见 [docs/billing.md](docs/billing.md)。

## 测试

```bash
python3 -I -m unittest discover -s tests -t . -v
```

## 输入

一个文件包含全部动作，支持 `.json`（动作对象数组）或 `.csv`（表头见下）。

| 字段 | 含义 | 说明 |
|---|---|---|
| `seq` | 仓内顺序号 | 同物料同仓按它执行；同物料内冲突即断点 |
| `action_id` | 动作身份 | 同身份且内容完全相同只执行一次；同身份内容不同即断点 |
| `op` | 动作类型 | receive/transfer/isolate/release/issue/return（中英文均可） |
| `material` / `warehouse` | 物料 / 仓 | 不同（仓，物料）完全独立 |
| `batch` / `age_start` | 批次 / 货龄起点 | 仅收料动作建立，之后随切片永久保留 |
| `qty` | 数量 | 正整数 |
| `location` | 库位 | 收料库位、移位目标、放行回到库位、退料库位 |
| `from_location` | 来源库位 | 移位、隔离的裁取库位 |
| `isolate_id` | 隔离动作身份 | 放行必须引用一次真实隔离动作 |
| `ref_doc` | 领料单号 | 退料必须引用一笔真实领料 |

CSV 中文表头别名（序号/动作编号/动作/物料/仓库/批次/收料日期/数量/库位/来源库位/隔离动作/领料单号）
可直接使用，未知列忽略。

## 解释规则（与流水语义一一对应）

- **收料 receive**：建立批次身份、货龄起点与原料段 `[0, qty)`。
- **选取顺序**：正常库存按 `(货龄起点, 原收料序号, 原料段起点)`；部分裁取一律从被选切片的**区段起点**开始。
- **移位 transfer / 隔离 isolate**：只从指定库位的正常库存按同一顺序裁取；移动后保留原收料身份、批次与货龄。
- **放行 release**：只允许指定隔离动作**尚持有**的切片，在该动作持有库存中按原顺序取，回到指定库位正常库存。
- **领料 issue**：从全仓正常库存按顺序裁取，每个裁片在交付表留一行，记录批次、原始货龄与原料段位置。
- **退料 return**：必须引用一笔领料；按该领料尚未退回的明细，**从最后实际取走的切片向前退，切片内由区段末端向前退**，退到指定库位。禁止引用别的物料、禁止超量退。
- **拆分可核验**：任何拆分只产生带 `(原收料序号, 段起点, 段末端)` 的子片；已领数量不会被复制成在库数量。
- **断点**：顺序号冲突、数量不足、非法引用等，报告该物料首个断点，保留断点前结果，不继续猜测后续拆分。

## 产物（输出目录）

| 文件 | 内容 |
|---|---|
| `delivery.csv` | 领料交付表：单据 × 批次切片，含已退/未退数量 |
| `period_end_on_hand.csv` | 期末在库切片（带库位） |
| `period_end_isolated.csv` | 期末隔离切片（`holder` 为隔离动作身份） |
| `period_end_issued.csv` | 期末仍被领料占用的切片（`holder` 为领料单号） |
| `period_end_slices.csv` | 以上三类的合并视图 |
| `reconciliation.csv` / `.json` | 收料合计、各状态合计、守恒与原料段覆盖校验 |
| `errors.json` | 各物料首个断点（无断点时为空数组） |

## 样例

- `samples/flow_main.json`：老批次部分进隔离 → 期间领用 → 分批放行 → 再领用 → 部分退料，完整手算见 `docs/hand_calc.md`。
- `samples/flow_legacy.csv`：中文表头/中文动作的旧系统导出风格。
- `samples/flow_breakpoints.json`：重复身份幂等、非法退料引用断点。
- `samples/flow_seq_conflict.json`：顺序号冲突断点。
- `samples/flow_million.json`：300 万数量切片，全流程后期末只有 4 行切片数据。
- `samples/recall_*.json`：召回影响分析样例，手算见 `docs/recall.md`。
- `samples/handover_flow.json` + `samples/handover_main.json`：版本链、作废与退料；手算见 `docs/handover.md`。
- `samples/handover_dispute_flow.json` + `samples/handover_dispute.json`：两票争用同一笔收料的局部区段，只改一份凭证即解除两边争议。
- `samples/billing_flow.json` + `samples/billing_prices.json`：离线计费手算样例（同一收料部分隔离后返回、另一部分移位；账期中途开始，覆盖跨档、改价与同分钟进出），手算见 [docs/billing.md](docs/billing.md)。
