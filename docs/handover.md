# 交接对账（handover）

结算日，发仓交出已出库的领料单，收仓交出已入库的收料单，承运商还会补交、
改签或作废分段签收凭证。本入口按**指定凭证发布号**离线核对：

- 发出的哪些货能销账（已签收）；
- 哪些还要催收（待签收）；
- 哪些需要找双方核实（争议）。

**只生成清单，不动库存**：仓内流水始终按本次传入的结算快照解释，
本入口只读取交付行与收料原始段。

## 运行

```bash
# 两票争用同一笔收料的样例（先看上限 1 的争议，再看上限 2 改签后解除）
python3 -I run_slicer.py handover \
    --flow            samples/handover_dispute_flow.json \
    --handover        samples/handover_dispute.json \
    --publish-limit   1

# 同一份交接文件，用更早的上限重算，能重现改签前的对账
python3 -I run_slicer.py handover \
    --flow            samples/handover_dispute_flow.json \
    --handover        samples/handover_dispute.json \
    --publish-limit   2

# 退料、版本链与作废的样例
python3 -I run_slicer.py handover \
    --flow            samples/handover_flow.json \
    --handover        samples/handover_main.json \
    --publish-limit   4
```

- 成功：stdout 输出 JSON 清单，退出码 `0`；也可加 `--out 清单.json` 落盘。
- 失败：stderr 指出是哪条记录/哪笔单据，退出码 `1`，**不输出清单**。
- `--flow` 复用现有流水格式（`.json` 动作数组或旧系统 `.csv`）。

## 输入

### 交接文件 JSON

```json
{
  "transfers": [
    {"doc_no": "T1",
     "issue": {"warehouse": "WH-A", "material": "RM", "action_seq": 3},
     "dest_warehouse": "WH-C"}
  ],
  "vouchers": [
    {"voucher_no": "V1", "publish_no": 2, "status": "signed",
     "doc_no": "T1", "ship_segment": [0, 5],
     "receipt": {"warehouse": "WH-C", "material": "RM",
                 "action_seq": 10, "start": 0}},
    {"voucher_no": "V3", "publish_no": 4, "status": "void"}
  ]
}
```

| 字段 | 含义 |
|---|---|
| `transfers[].doc_no` | 调拨号，全文件唯一 |
| `transfers[].issue` | 发仓领料引用 `(仓, 物料, 动作序号)`；同一领料不得列在两票 |
| `transfers[].dest_warehouse` | 指定收仓；不能等于发仓 |
| `vouchers[].voucher_no` / `publish_no` | 凭证号 / 非负整数发布号 |
| `vouchers[].status` | `signed`（完整签收）或 `void`（作废） |
| `vouchers[].doc_no` / `ship_segment` | 签收的调拨号与发货半开区段 `[起点, 末端)` |
| `vouchers[].receipt` | 收料引用 + 收料区段**起点**；末端 = 起点 + 发货区段长度 |

作废记录只带凭证号、发布号与状态。未出现在 `transfers` 里的调拨号、
不存在的领料/收料引用、越界区段都直接失败。

### 发布号裁定

- `publish_no <= limit` 的记录才参与裁定；**上限外的记录完全不看**。
- 同一凭证号取上限内**最高发布号**的一条为准；作废表示该凭证不再认领任何货。
- 更正是**整条替换**，不是追加：更低发布号的旧内容即使仍在文件里也不生效。
- 同凭证号、同发布号、内容一致的重复记录只算一次；内容不同则报错。
- 文件行序不代表发布顺序；比较的是内容签名，与行序无关。

因此，历史签收即使已被替换或作废，**仍可用更早的上限重算**；
`--flow` 始终是本次传入的结算快照。

## 对账语义

### 发货坐标

一笔领料自首次取走起，按实际交付次序连续编号；换到下一批料（下一条交付行）
编号不断开。退料后原编号不变：退回的位置仍在清单上标为 `RETURNED`，
但**不能销账，也不占用收仓签收位置**。

### 签收映射

完整签收把发货区段 `[s, e)` **等长、顺序不变**地映射到目标收料从
`rec_start` 开始的区段：发货位置 `p` 对应收料位置 `rec_start + (p - s)`。
落在已退料位置的凭证只在这些位置失效，其余位置按上式继续对账（不压缩、不重排）。
跨原始交付行时来源坐标仍按上述偏移精确计算。

生效签收必须：

- 引用该票的**指定收仓**、同一物料；
- 两个区段都落在各自原始单据数量内（不把越界数据裁成合法签收）。

### 状态认定（按位置，不整单否决）

对每个**未退回**发货位置：

| 状态 | 含义 |
|---|---|
| `PENDING` | 待签收：没有生效认领 |
| `RECEIVED` | 已签收：恰有一个生效凭证，且映射到的收料位置也只有该凭证认领 |
| `DISPUTED` | 争议：发货或收料任一边被多个生效凭证认领 |
| `RETURNED` | 已退回：不参与销账 |

- 一笔收料可以合法地分段核销多票；**争议按实际重叠的位置认定**，不在冲突中挑赢家。
- 收料侧比较的是**所有凭证的未退料部分**，即使某凭证在发货侧已有争议，
  它的认领仍参与收料侧的冲突判断。

### 分段

每票完整覆盖 `[0, ship_qty)`，在交付行边界、退料边界、认领边界，以及
**收料侧他人认领的边界**（映射回发货坐标）处切开，输出最大连续段。
`others` / `other_claim_vouchers` 列出该收料段上其他认领凭证号。

## 输出

```json
{"publish_limit": 1,
 "transfers": [
  {"doc_no": "T1",
   "issue": {"warehouse": "WH-A", "material": "RM", "action_seq": 3},
   "dest_warehouse": "WH-C", "ship_qty": 12,
   "segments": [
     {"ship": {"start": 0, "end": 5, "qty": 5}, "status": "RECEIVED",
      "vouchers": [{"voucher_no": "V1", "publish_no": 1,
                    "ship": {"start": 0, "end": 5, "unreturned_qty": 5},
                    "receipt_segments": [
                      {"receipt": {"warehouse": "WH-C", "material": "RM",
                                   "action_seq": 10},
                       "start": 0, "end": 5}]}],
      "source": {"receipt": {"warehouse": "WH-C", "material": "RM",
                             "action_seq": 10},
                 "start": 0, "end": 5},
      "other_claim_vouchers": []},
     {"ship": {"start": 6, "end": 10, "qty": 4}, "status": "DISPUTED",
      "vouchers": [ ... ],
      "conflict": {"ship": {"start": 6, "end": 10, "vouchers": ["V1"]},
                   "receipts": [{"receipt": {"warehouse": "WH-C",
                                             "material": "RM",
                                             "action_seq": 10},
                                 "start": 6, "end": 10,
                                 "vouchers": ["V1", "V2"]}]},
      "other_claim_vouchers": ["V2"]}
   ]}
 ]}
```

- `transfers` 按调拨号稳定排序；同一票内段按发货起点排序。
- 发货与收料坐标全部为半开区段 `[start, end)`。
- `DISPUTED` 段给出 `conflict`：两边的重叠坐标与参与凭证，便于先核对再改签。
- 段内 `vouchers` 是该段全部生效凭证；`unreturned_qty` 是它在发货侧仍有效
  （未落退料区）的数量。

## 样例与手算

### 两票争用同一笔收料（`samples/handover_dispute_*`）

- T1：WH-A 领料 seq=3，12 件，指定收仓 WH-C。
- T2：WH-B 领料 seq=6，6 件，退料 2 件，指定收仓 WH-C。
- V1：T1 发货 `[0,10)` → WH-C 收料 seq=10 从 0 起（RC1 `[0,10)`）。
- V2（发布号 1）：T2 发货 `[0,4)` → 同一笔收料 seq=10 从 6 起（RC1 `[6,10)`）。

**上限 1**：RC1 的 `[6,10)` 同时被 V1、V2 认领，因此

- T1 发货 `[6,10)` 争议（自己的 V1 把货放到 RC1，而 RC1 上还有 V2）；
- T2 发货 `[0,4)` 争议（V2 把货放到 RC1，而 RC1 上还有 V1）；
- 两边 `conflict.receipts[0]` 都给出 `RC1[6,10)` 与 `["V1","V2"]`。

**上限 2**：只把 V2 改签一次（`ship_segment` 不变，收料改为 seq=11 从 0 起）。
V1 一个字没改，T1 的争议也随之解除：

- T1：`[0,5)`、`[5,10)` 已签收，`[10,12)` 待签收；
- T2：`[0,4)` 已签收，`[4,6)` 已退回。

再跑 `--publish-limit 1`，清单与改签前逐字相同。

### 退料与跨交付行（`samples/handover_main*`）

- WH-A 领料 seq=3 共 12 件 = 交付行 RA1 `[0,5)` + RA2 `[0,7)`；退料 3 件，
  发货坐标 `[9,12)` 为 `RETURNED`。
- V1 `[0,5)` 与 V2 `[2,6)` 在 RC1 上重叠，形成 `[2,5)` 争议；V3 先签 `[1,3)`，
  发布号 3 被发布号 4 的作废整条替换。
- 若把签收改到跨交付行的 `[3,10)`：`[3,5)` → 收料 `[0,2)`，
  `[5,9)` → 收料 `[2,6)`，来源坐标仍精确（见测试）。

`--publish-limit 0` 时所有签收都还不存在（只剩退料段），
`--publish-limit 3` 能重现 V3 被作废前的争议。

## 失败模式（退出码 1，不输出清单）

- 仓内流水存在断点（指出物料/仓与 `seq`）；
- 调拨引用无效（领料/收料动作序号不存在）、调拨号重复、同一领料列在两票、
  收仓等于发仓；
- 上限内同一凭证号同一发布号内容矛盾（指出两处记录位置）；
- 生效签收引用错误：调拨号不存在、收料仓不是指定收仓、物料不同、
  发货或收料区段越界（不裁剪）；
- 发布号上限不是非负整数。

## 测试

```bash
python3 -I -m unittest discover -s tests -t . -v
```

`tests/test_handover.py` 覆盖：版本裁定与行序无关、作废、退料坐标、
跨交付行来源坐标、两票争用与只改一份凭证、旧上限重现、全部失败模式，
以及三百万件发货只产生少量区段（`不按件展开`）。
