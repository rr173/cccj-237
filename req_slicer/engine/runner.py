"""批处理运行器：分组、前置校验、断点控制与结果收集。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (Action, MaterialLedger, FlowError,
                    NORMAL, ISOLATED, ISSUED, RECEIVE)


@dataclass
class GroupResult:
    material: str
    warehouse: str
    ledger: MaterialLedger
    processed: int = 0                 # 断点前成功执行的动作数
    skipped_duplicates: int = 0        # 完全相同的重复动作（幂等跳过）
    error: str | None = None           # 首个断点说明
    error_seq: int | None = None
    error_action_id: str | None = None


@dataclass
class RunResult:
    groups: list[GroupResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(g.error is not None for g in self.groups)


def _signature(act: Action) -> tuple:
    """用于判定“重复动作身份且内容完全相同”的内容签名。"""
    return (act.op, act.material, act.warehouse, act.batch, act.age_start,
            act.qty, act.location, act.from_location,
            act.isolate_id, act.ref_doc, act.minute)


def plan_groups(actions: list[Action]) -> dict[tuple[str, str], list[Action]]:
    """按 (仓, 物料) 分组，复刻 run() 的去重/序号裁定，产出按 seq 排序的待执行动作。

    完全相同（含 minute）的重复动作只保留一条；序号冲突或同身份内容不同会抛出
    FlowError，并在消息里定位到最先出现的冲突动作。供需要按时间重放流水的
    离线入口（如计费）复用，保证与 run() 看到的是同一条流水。
    """
    grouped: dict[tuple[str, str], list[Action]] = {}
    for act in actions:
        grouped.setdefault((act.warehouse, act.material), []).append(act)

    result: dict[tuple[str, str], list[Action]] = {}
    for key, acts in grouped.items():
        seen_ids: dict[str, Action] = {}
        seen_seq: dict[int, Action] = {}
        ordered: list[Action] = []
        for act in acts:
            if act.seq in seen_seq:
                raise FlowError(
                    f"顺序号冲突：seq={act.seq} 同时被动作 "
                    f"{seen_seq[act.seq].action_id!r} 与 {act.action_id!r} 占用")
            seen_seq[act.seq] = act
            if act.action_id in seen_ids:
                prev = seen_ids[act.action_id]
                if _signature(prev) == _signature(act):
                    continue  # 完全相同：只执行一次
                raise FlowError(
                    f"动作身份 {act.action_id!r} 重复但内容不同："
                    f"先出现 {prev.op}，后出现 {act.op}")
            seen_ids[act.action_id] = act
            ordered.append(act)
        ordered.sort(key=lambda a: a.seq)
        result[key] = ordered
    return result


def run(actions: list[Action]) -> RunResult:
    groups: dict[tuple[str, str], list[Action]] = {}
    for act in actions:
        groups.setdefault((act.warehouse, act.material), []).append(act)

    result = RunResult()
    for (warehouse, material), acts in sorted(groups.items()):
        gr = GroupResult(material=material, warehouse=warehouse,
                         ledger=MaterialLedger(material, warehouse))
        result.groups.append(gr)

        # 保持文件顺序；重复身份与序号冲突都按“最先出现处”报首个断点
        seen_ids: dict[str, Action] = {}
        seen_seq: dict[int, Action] = {}
        ordered: list[Action] = []
        try:
            for act in acts:
                if act.seq in seen_seq:
                    raise FlowError(
                        f"顺序号冲突：seq={act.seq} 同时被动作 "
                        f"{seen_seq[act.seq].action_id!r} 与 {act.action_id!r} 占用")
                seen_seq[act.seq] = act

                if act.action_id in seen_ids:
                    prev = seen_ids[act.action_id]
                    if _signature(prev) == _signature(act):
                        gr.skipped_duplicates += 1  # 完全相同：只执行一次
                        continue
                    raise FlowError(
                        f"动作身份 {act.action_id!r} 重复但内容不同："
                        f"先出现 {prev.op}，后出现 {act.op}")
                seen_ids[act.action_id] = act
                ordered.append(act)

            ordered.sort(key=lambda a: a.seq)  # 同物料同仓按序号执行
            for act in ordered:
                gr.ledger.apply(act)
                gr.processed += 1
        except FlowError as exc:
            gr.error = str(exc)
            gr.error_seq = act.seq
            gr.error_action_id = act.action_id
            # 断点前已执行的结果全部保留在 ledger 中，后续不再猜测
    return result
