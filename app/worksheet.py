"""可顺序执行的量纲计算工作单（``POST /worksheet``）。

一次请求提交初始带单位变量与 2～5 个步骤，步骤按提交顺序执行：

- 每个步骤要么是公式求值（``formula``），要么是一次线性方程反求
  （``equation`` + ``solve_for``），并把结果精确换算到 ``output_unit``；
- 后续步骤可按结果名引用**先前步骤**的结果；引用未执行步骤或覆盖已有
  变量（初始变量或先前结果名）都在 schema 层被拒绝为 422；
- 步骤间传递的是 :class:`~app.evaluator.Quantity`——SI 基单位口径的
  **精确分数数值**与**绝对温度属性**（``absolute`` 标记），不经十进制
  序列化，因此没有中间精度损失；求值与反求沿用各自既有的量纲、仿射与
  精度规则；
- 任一步骤失败都抛出 :class:`~app.errors.ConversionError`（HTTP 422），
  端点不返回任何部分结果。

自定义单位来源（请求内 ``custom_units`` / 已发布定义集 / 活动别名）与
三个既有接口完全一致：单位上下文（:class:`~app.custom_units.UnitScope`）
在工作单开始时构造一次，所有步骤的单位解析都使用同一份定义；活动别名
因此在整个工作单内只解析一次——即使执行期间别名被切换，各步骤也使用
同一版定义，不会跨版本混用。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .converter import (
    convert_fraction,
    is_absolute_target,
    to_decimal_str,
)
from .errors import ConversionError, affine_mixing
from .evaluator import Quantity, evaluate, make_quantity
from .parser import ParsedExpression, canonical, canonical_dim, parse_expression
from .schemas import WorksheetRequest, WorksheetStepResult
from .solver import solve

#: 求值步骤中“绝对温标唯一项”结果在内部使用的源单位项（开尔文）。
_KELVIN_TERMS = (("K", 1),)


@dataclass(frozen=True)
class _StepOutcome:
    """单个步骤的内部结果：响应字段 + 供后续步骤引用的精确量。"""

    result: WorksheetStepResult
    quantity: Quantity


def _convert_step_result(
    quantity: Quantity, dst: ParsedExpression
) -> tuple[Fraction, str, str]:
    """把步骤结果（SI 口径精确量）精确换算到目标单位。

    仿射及精度规则与 ``/evaluate``、``/solve`` 完全一致：
    绝对温标结果只能换算到绝对温标；反求结果只能换算到线性单位。
    返回 ``(目标单位下的精确分数, 规范单位, 规范量纲)``。
    """

    if quantity.absolute:
        if not is_absolute_target(dst):
            raise affine_mixing(
                "绝对温度结果只能换算到绝对温标（degC/degF/K），"
                "不能换算到温差单位或复合单位"
            )
        src = ParsedExpression(dim=quantity.dim, scale=Fraction(1), terms=_KELVIN_TERMS)
    else:
        src = ParsedExpression(dim=quantity.dim, scale=Fraction(1), terms=())
    value_fr = convert_fraction(quantity.value_si, src, dst)
    return value_fr, canonical(dst.terms), canonical_dim(dst.dim)


def run_worksheet(req: WorksheetRequest, scope) -> list[WorksheetStepResult]:
    """顺序执行工作单，返回各步骤结果；任一步失败即抛出（422，无部分结果）。

    ``scope`` 为工作单开始时解析好的单位上下文（三种来源之一或 ``None``），
    整个工作单共用，保证别名只解析一次、各步骤使用同一版自定义单位定义。
    """

    # 初始变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    env: dict[str, Quantity] = {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }

    outcomes: list[_StepOutcome] = []
    for step in req.steps:
        dst = parse_expression(step.output_unit, scope)
        if step.formula is not None:
            # 求值：env 只含初始变量与先前步骤结果，引用未执行步骤会因名字
            # 尚不存在而以 unknown_variable 返回 422
            quantity = evaluate(step.formula, env)
        else:
            # 反求：未知量不得与初始变量/先前结果同名（schema 已保证结果名
            # 唯一，但 solve_for 独立于结果名，需在此排除覆盖）
            if step.solve_for in env:
                raise ConversionError(
                    "invalid_request",
                    f"solve_for {step.solve_for!r} 不得与初始变量或先前步骤结果重名",
                )
            if dst.affine is not None:
                raise affine_mixing(
                    "求解结果只能换算到线性单位，不能换算到绝对温标（degC/degF）"
                )
            value_si, dim = solve(step.equation, step.solve_for, env)
            if dim is None:
                # 解恒为零且量纲不受方程约束（如 2*x = x）：接受任意线性目标
                # 单位，量纲取目标单位
                quantity = Quantity(Fraction(0), dst.dim)
            else:
                quantity = Quantity(value_si, dim)

        value_fr, unit, dimension = _convert_step_result(quantity, dst)
        # 十进制化在每步产出时完成：非有限十进制或超精度即 422，无部分结果
        outcome = _StepOutcome(
            result=WorksheetStepResult(
                name=step.name,
                value=to_decimal_str(value_fr),
                unit=unit,
                dimension=dimension,
            ),
            quantity=quantity,
        )
        outcomes.append(outcome)
        # 步骤间传递精确分数与绝对温度属性（而非上面渲染出的十进制字符串）
        env[step.name] = quantity

    return [outcome.result for outcome in outcomes]
