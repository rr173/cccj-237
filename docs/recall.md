# 召回影响分析（recall）

供应商通知某笔到货中的一段料号需要召回时，回答两个问题：

1. 哪些**生产收料**受影响（可以交给生产系统冻结的清单）；
2. 每笔收料具体**冻结哪些区段**（半开整数区间，按收料自身原料段坐标）。

只做离线解释，**不写回生产系统**；保留现有仓内加工入口，不增加实时库存、
工艺推断或返修闭环。

## 运行

```bash
python3 -I run_slicer.py recall \
    --flow       samples/recall_flow.json \
    --production samples/recall_production.json \
    --request    samples/recall_request_ab.json
```

- 成功：stdout 输出一份 JSON（只列受影响的生产收料），退出码 `0`；清单为空也成功。
- 失败：stderr 指出具体错误，退出码 `1`，**不输出清单**。
- `--flow` 复用现有流水格式（`.json` 动作数组或旧系统 `.csv`）。
- 相同数据输出顺序稳定：收料按 `(仓, 物料, 动作序号)`，区段按起点排序并合并。

输出形状：

```json
{"affected_receipts": [
  {"receipt": {"warehouse": "WH-A", "material": "MA", "action_seq": 6},
   "work_order": "WO-A",
   "segments": [{"start": 20, "end": 40, "qty": 20}],
   "qty_total": 20}
]}
```

## 召回请求

```json
{"receipt": {"warehouse": "WH-A", "material": "RM", "action_seq": 1},
 "segments": [[20, 40], [40, 60]]}
```

- `receipt` 必须是**外部到货**收料：生产清单里没有任何生产单把它声明为产出。
- 区段是原料段坐标系上的非空半开区间 `[起点, 末端)`，必须是整数、起点 < 末端、不越界。
- 相交或相邻的请求区段先合并成同一份覆盖，不重复计量（`[[20,40],[40,60]]` 等价于 `[[20,60]]`）。

## 生产清单

```json
{"work_orders": [
  {"doc_no": "WO-A", "process": "ordered",
   "issues":   [{"warehouse": "WH-A", "material": "RM", "action_seq": 2}],
   "receipts": [{"warehouse": "WH-A", "material": "MA", "action_seq": 6}]}
]}
```

| 字段 | 含义 |
|---|---|
| `doc_no` | 生产单号，清单内唯一 |
| `process` | `ordered`（保序加工）/ `mixed`（混料）；也接受 `保序` / `混料` |
| `issues` | 按**实际投料顺序**排列的领料引用 |
| `receipts` | 按**产出顺序**排列的收料引用 |

- 领料/收料都用 `(仓, 物料, 动作序号)` 定位。批次显示名、单据显示名、单独序号都不是全局键。
- 清单覆盖本次要追踪的全部生产：未被任何 `receipts` 声明为产出的收料视为**外部到货**。
- 每条领料最多归一个生产单；每笔收料只能被一个生产单声明为产出。重复即失败。

## 追踪语义

1. **实际投料**以账期结束时仍由该领料占用的原料段为准（交付行的 `outstanding_qty`）。
   已退回的尾部不再算给原领料；退回后又被另一笔领料取走，则跟随新领料的归属。
   单条领料内部按原交付行顺序使用仍有余量的区段。
2. **保序加工**：把该单各领料的实际投料区段按清单顺序（同单内按交付行顺序）连成
   一条连续坐标轴，再按产出顺序划给各笔收料；投入总量必须等于产出总量。
   召回只跟随相同位置的产出：召回区段在坐标轴上取交，跨产出边界时只冻结各自
   相交的部分。
3. **混料**：任一实际投料与召回覆盖相交，就冻结该单全部产出区段；不要求投入与
   产出数量相等。
4. **中间品**：再次领用时只传递实际取到的召回区段（与上游召回取交），不把整批
   中间品一律当作受影响。多条来路指向同一段时先合并再计量，不沿来路累加。
5. 每工单实际投料总量必须大于零（单笔领料可以全部退回，但不能让整单没有实际投料）。
6. 不同仓或物料的动作序号不能用来推断工序先后；先后只来自生产单里显式写出的
   "领料 -> 产出"引用。

## 失败情况（明确指出并退出 1）

- 仓内流水存在断点（顺序号冲突、数量不足、非法引用等；复用现有引擎报告）；
- 生产清单：JSON 结构/类型不符、引用缺失、领料或收料重复归属、工序依赖成环、
  无实际投料、保序加工投入/产出数量不符；
- 召回请求：收料不存在、不是外部到货、区段类型不符/为空/倒置/越界。

只要任一项失败，就**不输出一份看似完整的冻结清单**。

## 区段处理与规模

所有影响量都是 `(start, end)` 整数区段，绝不按件展开；合并相邻/重叠区段后
再计量。300 万件的收料在清单里仍是 1–2 个区段对象，内存与工作量只随区段
个数增长。

## 样例

| 文件 | 内容 |
|---|---|
| `samples/recall_flow.json` | 双支路汇合流水 + 另一仓的叶子支路；含一笔退料 |
| `samples/recall_flow_before_return.json` | 同上但**没有**那笔退料（用于对比） |
| `samples/recall_production.json` | 9 张生产单，含保序与混料 |
| `samples/recall_request_ab.json` | RM[20,60)（相邻区段合并） |
| `samples/recall_request_partial.json` | RM[20,40)（局部冻结示例） |
| `samples/recall_request_leaf.json` | 另一仓 RM[15,40)（叶子/跨产出边界示例） |
| `samples/recall_request_mixed.json` | RM[105,120)（退料前后混料对比） |

## 手算核对（双支路汇合，局部受影响）

流水要点（`samples/recall_flow.json`）：

- `RA@WH-A/RM = [0,120)`；四次领料依次取 `I-A[0,40) I-B[40,70) I-C[70,100) I-D[100,120)`。
- `I-D` 退 15 件（自末端），期末只占用 `[100,105)`，`[105,120)` 回到在库。
- WO-A ← I-A 的 40 件，产出 `PA@WH-A/MA = [0,40)`。
- WO-B ← I-B 的 30 件，产出 `PC@WH-C/MC = [0,30)`。
- WO-F 先投 MA 全 40 件、再投 MC 全 30 件，产出 `PF@WH-C/MP = [0,70)`：
  轴 `[0,40)` 映射 MA，`[40,70)` 映射 MC。
- WO-D1 取 `MP[0,20)` → `MD[0,20)`；WO-D2 取 `MP[20,70)` → `ME[0,50)`。
- 两条支路在此汇合：WO-D2 的投料同时含 MA 段 `[0,40)` 与 MC 段 `[40,70)`。

召回 `RM[20,40)`（`recall_request_partial.json`）：

| 环节 | 坐标推演 | 冻结结果 |
|---|---|---|
| WO-A | I-A 内偏移 20..40 | `MA[20,40)` |
| WO-F | MA 轴坐标 20..40 → `MP[20,40)` | `MP[20,40)` |
| WO-D2 | 只领走 `MP[20,70)`，轴 20..40 平移为产出 `ME[0,20)` | `ME[0,20)`（非整笔 50） |
| WO-D1 | 只领走 `MP[0,20)`，与召回不相交 | **不出现** |

因此 WO-D1 这类"后续只取该收料干净部分"的订单不会被误冻结，而 WO-D2 只冻结
`ME` 的局部 `[0,20)`。`recall_request_partial.json` 即 `RM[20,40)` 的实际输出：

```json
{"affected_receipts": [
  {"receipt": {"warehouse": "WH-A", "material": "MA", "action_seq": 6},
   "work_order": "WO-A", "segments": [{"start": 20, "end": 40, "qty": 20}], "qty_total": 20},
  {"receipt": {"warehouse": "WH-C", "material": "MP", "action_seq": 10},
   "work_order": "WO-F", "segments": [{"start": 20, "end": 40, "qty": 20}], "qty_total": 20},
  {"receipt": {"warehouse": "WH-E", "material": "ME", "action_seq": 14},
   "work_order": "WO-D2", "segments": [{"start": 0, "end": 20, "qty": 20}], "qty_total": 20}
]}
```

相邻区段合并的样例 `recall_request_ab.json` 给出同一条通知 `RM[20,60)`：
WO-A 不变；WO-F 的 `MP[20,60)` 合计 40；`MP[20,60)` 映射到 WO-D2 的 `ME[0,40)`
合计 40；WO-B 的 `MC[0,20)` 合计 20。

另有一层"干净/命中"叶子示例（`recall_request_leaf.json`，另一仓 WH-Z）：

- `RD@WH-Z/RM = [0,50)`，WO-E 投全 50，产出两笔各 25：P-E1 `[0,25)`、P-E2 `[0,25)`。
- 下游 WO-CLEAN 领 `MA[0,15)`（P-E1 的前 15 件），WO-HIT 领 `MA[15,25)`（后 10 件）。
- 召回 `RM[15,40)`：P-E1 的 `[15,25)` 与 P-E2 的 `[0,15)` 命中，
  WO-HIT 因取到 `MA[15,25)` 被冻结；WO-CLEAN 只碰 `MA[0,15)`，**不出现**。

```json
{"affected_receipts": [
  {"receipt": {"warehouse": "WH-Y", "material": "MQ2", "action_seq": 24},
   "work_order": "WO-HIT", "segments": [{"start": 0, "end": 10, "qty": 10}], "qty_total": 10},
  {"receipt": {"warehouse": "WH-Z", "material": "MA", "action_seq": 19},
   "work_order": "WO-E", "segments": [{"start": 15, "end": 25, "qty": 10}], "qty_total": 10},
  {"receipt": {"warehouse": "WH-Z", "material": "MA", "action_seq": 20},
   "work_order": "WO-E", "segments": [{"start": 0, "end": 15, "qty": 15}], "qty_total": 15}
]}
```

## 退料前后的混料对比

混料单 WO-M 投 `I-D` 的 RA 段，产出 `MM@WH-F = [0,60)`。

| 同一条通知 `RM[105,120)` | 结果 |
|---|---|
| **退料前**（`recall_flow_before_return.json`） | I-D 占用 `[100,120)`，与 `[105,120)` 相交 → 冻结 `MM[0,60)` |
| **退料后**（`recall_flow.json`） | I-D 只占用 `[100,105)`，`[105,120)` 是在库退回段 → 清单为空 |

```bash
python3 -I run_slicer.py recall --flow samples/recall_flow.json \
    --production samples/recall_production.json --request samples/recall_request_mixed.json
# {"affected_receipts": []}
python3 -I run_slicer.py recall --flow samples/recall_flow_before_return.json \
    --production samples/recall_production.json --request samples/recall_request_mixed.json
# WO-M / MM[0,60) 仍被列出（退料前）
```

即：一条只落在**已退回区段**内的通知，不会让原混料工单继续出现在清单里。

## 测试

```bash
python3 -I -m unittest discover -s tests -t . -v
```

`tests/test_recall.py` 覆盖：双支路汇合精确坐标、跨产出边界只冻结相交部分、
干净下游不误冻结、相邻/相交请求区段合并去重、退料前后混料对比、退料后被另一
领料重新取走的归属、各类输入/引用/类型/重复归属/无实际投料/保序数量不符/
成环/越界失败、CLI 成功与失败输出，以及 300 万件仍只有少量区段对象。
