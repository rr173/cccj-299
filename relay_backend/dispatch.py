"""跨会场补班的纯调度逻辑（不依赖 FastAPI / SQLite）。

输入是已在内存中整理好的班段与人员，输出是一份满足全部约束、且"更换已有班段
人员数量最少"的完整班表；无解时给出无法同时覆盖的班段清单。

约束
====
1. 每个班段恰有一名满足语言要求的口译员（擅长语言与班段所需语言有交集）。
2. 人员的可用时间窗（按并集合并后）必须完整覆盖班段的实际时间区间；首尾相接
   即可。已锁定（班段内任一格已发布，含静音）的班段不重新校验可用性。
3. 同一人员的两个班段在实际时间上不能重叠；一端结束时间等于另一端开始时间
   （首尾相接）允许。跨会场同样生效。
4. 已锁定班段的人员固定不动；只在其余班段上求解。
5. 所有可行方案中，更换已有班段人员（与当前归属不同）的数量最少；同优方案按
   人员 ID 的确定性顺序任选，保证结果可复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# 回溯节点安全阀：演示与小型调度规模远低于此，超限按"当前规模无解"处理。
_MAX_SEARCH_NODES = 200_000
# 逐段瓶颈诊断的规模上限；超过后不再做 n+1 次求解，直接返回全部待排段。
_DIAGNOSTIC_LIMIT = 60


@dataclass(frozen=True)
class Segment:
    """一个班段：某会场 [start_slot, end_slot) 的连续格。"""

    session_id: int
    start_slot: int
    end_slot: int
    start: datetime          # 班段实际开始时间（含），带时区
    end: datetime            # 班段实际结束时间（不含），带时区
    languages: frozenset[str]
    locked: bool = False
    current_person: Optional[str] = None
    # 已发布前缀之后、仍需覆盖的部分。锁定班段只需覆盖 [cover_start, end)，
    # 即"新可用时间只须覆盖尚未发布的部分"；未锁定班段从班段起点开始覆盖。
    cover_start: Optional[datetime] = None

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.session_id, self.start_slot, self.end_slot)

    @property
    def required_start(self) -> datetime:
        """可用性必须覆盖到的起点：锁定段为首个未发布格，否则为班段起点。"""
        return self.cover_start if self.cover_start is not None else self.start


@dataclass(frozen=True)
class Person:
    person_id: str
    languages: frozenset[str]
    windows: tuple[tuple[datetime, datetime], ...] = field(default_factory=tuple)


class InfeasibleSchedule(Exception):
    """不存在满足全部约束的完整班表。"""

    def __init__(self, issues: list[dict]):
        super().__init__("schedule is infeasible")
        self.issues = issues


def merged_windows(
    windows: tuple[tuple[datetime, datetime], ...],
) -> list[tuple[datetime, datetime]]:
    """把可用时间窗排序并合并相交/首尾相接的窗口。"""
    ordered = sorted(windows, key=lambda w: (w[0], w[1]))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def windows_cover(
    windows: tuple[tuple[datetime, datetime], ...],
    start: datetime,
    end: datetime,
) -> bool:
    """窗口并集是否完整覆盖闭开区间 [start, end)。"""
    cursor = start
    for win_start, win_end in merged_windows(windows):
        if win_start > cursor:
            return False
        if win_end >= end:
            return win_start <= cursor
        if win_end > cursor:
            cursor = win_end
    return cursor >= end


def language_ok(person: Person, segment: Segment) -> bool:
    return bool(person.languages & segment.languages)


def person_covers(person: Person, segment: Segment) -> bool:
    """语言合格且可用时间覆盖班段仍需覆盖的部分。

    未锁定班段要覆盖整段；锁定班段（已有格发布）只要求覆盖首个未发布格到
    班段结束，已发布前缀不重新校验。
    """
    return language_ok(person, segment) and windows_cover(
        person.windows, segment.required_start, segment.end
    )


def segments_overlap(a: Segment, b: Segment) -> bool:
    """实际时间重叠（首尾相接不算重叠）。"""
    return a.start < b.end and b.start < a.end


def _static_domain(segment: Segment, persons: dict[str, Person]) -> list[str]:
    return sorted(
        pid for pid, person in persons.items() if person_covers(person, segment)
    )


def solve(
    segments: list[Segment],
    persons: dict[str, Person],
) -> dict[tuple[int, int, int], str]:
    """求最小换人数量的完整班表；无解抛 :class:`InfeasibleSchedule`。"""
    assignment, _churn = _search(segments, persons)
    if assignment is None:
        raise InfeasibleSchedule(explain_infeasible(segments, persons))
    return assignment


def _search(
    segments: list[Segment],
    persons: dict[str, Person],
) -> tuple[Optional[dict[tuple[int, int, int], str]], int]:
    """分支限界回溯，返回 (班表, 换人数)；无解返回 (None, 0)。"""
    locked: dict[tuple[int, int, int], str] = {}
    unlocked: list[Segment] = []
    # 每个人员已占用（时间上不能再重叠）的班段，先放锁定班段。
    busy: dict[str, list[Segment]] = {}
    for segment in sorted(segments, key=lambda s: s.key):
        if segment.locked:
            if segment.current_person is None:
                # 防御：锁定班段必有当前归属；数据损坏时直接判无解。
                return None, 0
            holder = persons.get(segment.current_person)
            # 锁定班段人员不能更换，但其新可用时间仍须覆盖尚未发布的部分；
            # 覆盖不了则整组排班无解（已发布前缀不重新校验）。
            if holder is None or not person_covers(holder, segment):
                return None, 0
            locked[segment.key] = segment.current_person
            busy.setdefault(segment.current_person, []).append(segment)
        else:
            unlocked.append(segment)

    domains: dict[tuple[int, int, int], list[str]] = {
        segment.key: _static_domain(segment, persons) for segment in unlocked
    }
    # 静态域为空，立刻无解。
    if any(not domain for domain in domains.values()):
        return None, 0

    current = {
        segment.key: segment.current_person for segment in unlocked
    }
    best: Optional[dict[tuple[int, int, int], str]] = None
    best_churn = len(unlocked) + 1
    nodes = 0

    def feasible_for(person_id: str, segment: Segment) -> bool:
        return not any(
            segments_overlap(segment, other)
            for other in busy.get(person_id, ())
        )

    def forward_check(remaining: list[Segment]) -> bool:
        """给 remaining 中每个班段至少留一个当前不冲突的候选。"""
        for segment in remaining:
            if not any(
                feasible_for(pid, segment) for pid in domains[segment.key]
            ):
                return False
        return True

    def backtrack(
        remaining: list[Segment],
        chosen: dict[tuple[int, int, int], str],
        churn: int,
    ) -> None:
        nonlocal best, best_churn, nodes
        if churn >= best_churn:
            return  # 剪枝：换人次数已不可能更优
        nodes += 1
        if nodes > _MAX_SEARCH_NODES:
            raise InfeasibleSchedule([{
                "reason": "solver_limit_reached",
                "message": "scheduling search exceeded its node budget for this "
                           "request; split the change or enlarge the interpreter pool",
            }])
        if not remaining:
            best = dict(chosen)
            best_churn = churn
            return

        # MRV：候选（当前不冲突）最少的班段先排，平局按键序稳定排序。
        def candidate_count(segment: Segment) -> tuple[int, tuple]:
            count = sum(
                1 for pid in domains[segment.key] if feasible_for(pid, segment)
            )
            return count, segment.key

        head_index = min(range(len(remaining)),
                         key=lambda i: candidate_count(remaining[i]))
        segment = remaining[head_index]
        rest = remaining[:head_index] + remaining[head_index + 1:]

        candidates = [
            pid for pid in domains[segment.key] if feasible_for(pid, segment)
        ]
        # 优先尝试当前归属人员，帮助尽早找到低换人方案；其余按 ID 顺序。
        incumbent = current.get(segment.key)
        if incumbent in candidates:
            candidates.remove(incumbent)
            candidates.insert(0, incumbent)

        for person_id in candidates:
            extra_churn = 0 if current.get(segment.key) == person_id else 1
            chosen[segment.key] = person_id
            busy.setdefault(person_id, []).append(segment)
            if forward_check(rest):
                backtrack(rest, chosen, churn + extra_churn)
            busy[person_id].pop()
            del chosen[segment.key]
            if best_churn == 0:
                return  # 已经达到理论最优，无需再试

    try:
        backtrack(unlocked, {}, 0)
    except InfeasibleSchedule:
        return None, 0
    if best is None:
        return None, 0
    return {**locked, **best}, best_churn


def explain_infeasible(
    segments: list[Segment],
    persons: dict[str, Person],
) -> list[dict]:
    """无解诊断：指出哪些班段无法同时覆盖及原因。"""
    issues: list[dict] = []

    # 0) 锁定班段：人员固定，但新可用时间覆盖不了尚未发布的部分。
    for segment in sorted((s for s in segments if s.locked),
                          key=lambda s: s.key):
        holder = persons.get(segment.current_person) if segment.current_person \
            else None
        if holder is None or not person_covers(holder, segment):
            issues.append({
                "session_id": segment.session_id,
                "start_slot": segment.start_slot,
                "end_slot": segment.end_slot,
                "reason": "locked_segment_holder_unavailable",
                "message": "the segment already has published slots and cannot be "
                           "reassigned, but its current interpreter's new "
                           "availability no longer covers the unpublished part",
                "person_id": segment.current_person,
            })
    if issues:
        return issues

    unlocked = sorted(
        (s for s in segments if not s.locked), key=lambda s: s.key
    )
    # 1) 静态域为空：没有任何语言合格且时间可覆盖的人。
    empty_domain: list[Segment] = []
    for segment in unlocked:
        domain = _static_domain(segment, persons)
        if not domain:
            qualified = sorted(
                pid for pid, person in persons.items()
                if language_ok(person, segment)
            )
            issues.append({
                "session_id": segment.session_id,
                "start_slot": segment.start_slot,
                "end_slot": segment.end_slot,
                "reason": "no_qualified_available_interpreter",
                "message": "no interpreter both speaks a required language and has "
                           "availability covering the segment's uncovered part",
                "qualified_but_unavailable": qualified,
            })
            empty_domain.append(segment)
    if empty_domain:
        return issues

    # 2) 域非空但排不下来：逐个移除班段，移除后其余可解的即瓶颈班段——
    #    它们无法与彼此（及锁定班段）同时被覆盖。
    if len(unlocked) <= _DIAGNOSTIC_LIMIT:
        bottleneck_keys: set[tuple[int, int, int]] = set()
        for segment in unlocked:
            rest = [s for s in segments if s.key != segment.key]
            assignment, _ = _search(rest, persons)
            if assignment is not None:
                bottleneck_keys.add(segment.key)
        if not bottleneck_keys:
            # 移除任何单个班段都不可解：把所有待排段列为共同瓶颈。
            bottleneck_keys = {s.key for s in unlocked}
    else:
        bottleneck_keys = {s.key for s in unlocked}

    by_key = {s.key: s for s in unlocked}
    for key in sorted(bottleneck_keys):
        segment = by_key[key]
        candidates = _static_domain(segment, persons)
        blocked_by: list[dict] = []
        for person_id in candidates:
            clashing = sorted(
                (s.key for s in segments
                 if s.key != segment.key
                 and s.locked
                 and s.current_person == person_id
                 and segments_overlap(segment, s))
            )
            if clashing:
                blocked_by.append({"person_id": person_id,
                                   "locked_segments": clashing})
        issues.append({
            "session_id": segment.session_id,
            "start_slot": segment.start_slot,
            "end_slot": segment.end_slot,
            "reason": "cannot_be_covered_simultaneously",
            "message": "every qualified interpreter is taken by an overlapping "
                       "segment under the current availability; these segments "
                       "cannot be covered at the same time",
            "candidate_person_ids": candidates,
            "locked_conflicts": blocked_by,
        })
    return issues
