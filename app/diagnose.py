"""工作单换版诊断（``POST /worksheet-diagnose``）。

一次 HTTP 请求提交一份现有的 2～5 步工作单（与 ``/worksheet`` 相同的
初始变量与步骤）以及旧、新两份**已发布**单位定义集的标识或活动别名：

1. **开始时分别固定实际指向**：直接给标识则原样使用；给别名则在该侧
   执行开始时快照别名当前指向的标识。随后分别在两个独立的请求级解析
   上下文（:class:`~app.custom_units.UnitScope`）上用各自定义执行**同一
   份**工作单；不修改任何定义集或别名。即使别名在请求处理中被切换，
   两侧也各自完整使用开始时固定的那一版，不会出现同侧步骤混版。
2. **逐步返回两侧结果及首个含义变化**：比较的是每一步内部的 SI 口径
   精确分数（``value_si``）、量纲与**绝对温度属性**，而不是渲染出的
   十进制显示字符串。典型情形：旧版 ``span=2 m``、新版 ``span=3 m`` 时，
   输入 ``1 span``、输出单位 ``span``，两侧 ``value`` 都显示 ``"1 span"``，
   但内部物理值（2 m vs 3 m）已变，仍报告
   ``change_type=physical_value``。
3. **单侧失败保留部分对照**：一侧在第 k 步因量纲冲突等业务错误失败时，
   保留两侧此前已完成步骤的对照并报告该步错误；该侧失败后的步骤标记为
   ``not_run``（不计为成功）。两侧都正常时返回全部步骤的对照。

未知定义集、未知别名或非法请求在任何执行开始前返回 **422**（与既有
接口一致）；工作单本身的业务错误（量纲冲突、除零等）不改变 HTTP 状态
码，而是按侧/按步体现在 200 响应中——这正是诊断与 ``/worksheet``
（任一步错误只返回 422、不返回部分结果）的区别。
"""

from __future__ import annotations

from fractions import Fraction

from .errors import ConversionError
from .evaluator import Quantity
from .parser import canonical_dim
from .schemas import (
    SIDE_STATUS_ERROR,
    SIDE_STATUS_INPUT_ERROR,
    SIDE_STATUS_NOT_RUN,
    SIDE_STATUS_OK,
    DiagnoseError,
    DiagnoseSideInfo,
    DiagnoseStepComparison,
    DiagnoseStepSide,
    ResolvedUnitSetRef,
    UnitSetRef,
    WorksheetDiagnoseRequest,
    WorksheetDiagnoseResponse,
    WorksheetStepResult,
)
from .unit_sets import UnitSetStore, get_store
from .worksheet import execute_step, prepare_environment


def resolve_ref(
    ref: UnitSetRef, store: UnitSetStore | None = None
) -> tuple[ResolvedUnitSetRef, object]:
    """开始时固定单侧引用的实际指向，并构造该侧独立的解析上下文。

    给标识时直接按该已发布定义集构造上下文；给别名时先快照别名当前指向
    （不存在抛 422），再按快照到的标识构造——之后该侧全部步骤只使用这一
    份定义，别名在执行期间被切换也不影响本侧。
    """

    chosen = store or get_store()
    if ref.custom_unit_set_alias is not None:
        alias = ref.custom_unit_set_alias
        set_id = chosen.get_alias(alias)  # 别名不存在 -> 422
        resolved = ResolvedUnitSetRef(
            kind="alias", requested=alias, custom_unit_set_id=set_id
        )
        # 按已固定的标识构造上下文，不再经过别名（执行期间切换别名不影响本侧）
        scope = chosen.build_scope(set_id)
    else:
        set_id = ref.custom_unit_set_id
        scope = chosen.build_scope(set_id)  # 标识不存在 -> 422
        resolved = ResolvedUnitSetRef(
            kind="id", requested=set_id, custom_unit_set_id=set_id
        )
    return resolved, scope


def _fraction_str(fr: Fraction) -> str:
    """把内部 SI 口径精确分数渲染为可跨版逐位比较的字符串。"""

    # 整数显示为 "120"；非整数保留分子/分母（"2/3"），绝不做十进制舍入
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"


def _error_info(exc: ConversionError) -> DiagnoseError:
    return DiagnoseError(error=exc.code, message=exc.message)


def _ok_side(result: WorksheetStepResult, quantity: Quantity) -> DiagnoseStepSide:
    return DiagnoseStepSide(
        status=SIDE_STATUS_OK,
        value=result.value,
        unit=result.unit,
        dimension=result.dimension,
        value_si=_fraction_str(quantity.value_si),
        value_si_dimension=canonical_dim(quantity.dim),
        absolute=quantity.absolute,
    )


def _run_side(
    req: WorksheetDiagnoseRequest, scope
) -> tuple[str, DiagnoseError | None, list[DiagnoseStepSide]]:
    """在固定的单位上下文上执行一侧工作单，返回 (侧状态, 侧错误, 各步结果)。

    - 初始变量解析失败 -> 侧状态 ``input_error``（整侧未开始）：错误体现在
      第 1 步（该步 status=error、携带错误信息），其余步骤为 ``not_run``；
      这样逐步比较与首个含义变化可以统一在步骤层面表达；
    - 某步骤业务失败 -> 侧状态 ``error``：该步带错误信息、其后各步为
      ``not_run``；此前已完成步骤的结果全部保留；
    - 全部成功 -> 侧状态 ``ok``。
    """

    try:
        env = prepare_environment(req.variables, scope)
    except ConversionError as exc:
        info = _error_info(exc)
        steps_out = [DiagnoseStepSide(status=SIDE_STATUS_ERROR, error=info)]
        steps_out.extend(_not_run_side() for _ in req.steps[1:])
        return SIDE_STATUS_INPUT_ERROR, info, steps_out

    step_sides: list[DiagnoseStepSide] = []
    for step in req.steps:
        if step_sides and step_sides[-1].status in (
            SIDE_STATUS_ERROR,
            SIDE_STATUS_NOT_RUN,
        ):
            # 前一步已失败：本步不再执行（不计为成功）
            step_sides.append(_not_run_side())
            continue
        try:
            result, quantity = execute_step(step, env, scope)
        except ConversionError as exc:
            step_sides.append(
                DiagnoseStepSide(status=SIDE_STATUS_ERROR, error=_error_info(exc))
            )
        else:
            step_sides.append(_ok_side(result, quantity))

    failed = any(s.status == SIDE_STATUS_ERROR for s in step_sides)
    error = next((s.error for s in step_sides if s.error is not None), None)
    return (SIDE_STATUS_ERROR if failed else SIDE_STATUS_OK), error, step_sides


def _not_run_side() -> DiagnoseStepSide:
    return DiagnoseStepSide(status=SIDE_STATUS_NOT_RUN)


def _compare_step(
    index: int, name: str, old_side: DiagnoseStepSide, new_side: DiagnoseStepSide
) -> DiagnoseStepComparison:
    """比较同一步在两侧的内部结果，得到该步的含义变化结论。"""

    meaning_changed = False
    change_type: str | None = None

    old_ok = old_side.status == SIDE_STATUS_OK
    new_ok = new_side.status == SIDE_STATUS_OK
    if old_ok and new_ok:
        # 以内部精确物理值、量纲、绝对温度属性的顺序比较（不比较显示字符串）
        if old_side.value_si != new_side.value_si:
            meaning_changed, change_type = True, "physical_value"
        elif old_side.value_si_dimension != new_side.value_si_dimension:
            meaning_changed, change_type = True, "dimension"
        elif bool(old_side.absolute) != bool(new_side.absolute):
            meaning_changed, change_type = True, "absolute_temperature"
    elif old_side.status == SIDE_STATUS_NOT_RUN or new_side.status == SIDE_STATUS_NOT_RUN:
        # 失败步之后的未执行步骤：差异已由首个失败步报告，此处不再计为变化
        pass
    elif not old_ok and not new_ok:
        # 两侧都在该步失败：错误码相同视为含义一致；错误码不同仍是含义变化
        if (
            old_side.error is not None
            and new_side.error is not None
            and old_side.error.error != new_side.error.error
        ):
            meaning_changed, change_type = True, "error_code"
    else:
        # 一侧成功、另一侧在该步业务失败：执行状态（成功↔失败）变化
        meaning_changed, change_type = True, "execution_status"

    display_changed = (
        old_ok
        and new_ok
        and (old_side.value, old_side.unit) != (new_side.value, new_side.unit)
    )
    return DiagnoseStepComparison(
        index=index,
        name=name,
        old=old_side,
        new=new_side,
        meaning_changed=meaning_changed,
        change_type=change_type,
        display_changed=display_changed,
    )


def diagnose_worksheet(
    req: WorksheetDiagnoseRequest, store: UnitSetStore | None = None
) -> WorksheetDiagnoseResponse:
    """执行换版诊断：两侧固定指向、各自执行、逐步比较并报告首个含义变化。

    未知定义集/别名在任何执行开始前抛出 :class:`~app.errors.ConversionError`
    （HTTP 422）；两侧使用相互独立的解析上下文，不修改定义集或别名。
    """

    chosen = store or get_store()
    # 开始时分别固定两侧实际指向；任一引用未知即 422（不执行任何步骤）
    old_ref, old_scope = resolve_ref(req.old, chosen)
    new_ref, new_scope = resolve_ref(req.new, chosen)

    old_status, old_error, old_steps = _run_side(req, old_scope)
    new_status, new_error, new_steps = _run_side(req, new_scope)

    comparisons = [
        _compare_step(i + 1, step.name, old_steps[i], new_steps[i])
        for i, step in enumerate(req.steps)
    ]
    first_changed = next((c.index for c in comparisons if c.meaning_changed), None)
    return WorksheetDiagnoseResponse(
        old=DiagnoseSideInfo(ref=old_ref, status=old_status, error=old_error),
        new=DiagnoseSideInfo(ref=new_ref, status=new_status, error=new_error),
        meaning_changed=first_changed is not None,
        first_changed_step=first_changed,
        steps=comparisons,
    )
