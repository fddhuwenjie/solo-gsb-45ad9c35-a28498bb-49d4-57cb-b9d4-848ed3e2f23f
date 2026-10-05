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

``/worksheet-diagnose`` 的两侧对照复用 :func:`prepare_environment` 与
:func:`execute_step`：两者分别在各自固定的定义集上独立执行，单步的内部
精确量（SI 口径分数、量纲、绝对温度属性）直接用于两侧比较，而不是比较
渲染出的十进制字符串。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Mapping

from .converter import (
    convert_fraction,
    is_absolute_target,
    to_decimal_str,
)
from .errors import ConversionError, affine_mixing
from .evaluator import Quantity, evaluate, make_quantity
from .parser import ParsedExpression, canonical, canonical_dim, parse_expression
from .schemas import Variable, WorksheetRequest, WorksheetStep, WorksheetStepResult
from .solver import solve

#: 求值步骤中“绝对温标唯一项”结果在内部使用的源单位项（开尔文）。
_KELVIN_TERMS = (("K", 1),)


def prepare_environment(
    variables: Mapping[str, Variable], scope
) -> dict[str, Quantity]:
    """把初始带单位变量解析并折叠为 SI 口径的精确量（工作单开始时执行一次）。

    任一变量的单位在该定义集下无法解析（未知符号/量纲非法等）都抛出
    :class:`~app.errors.ConversionError`（422）；此时环境尚未建成。
    """

    return {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in variables.items()
    }


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


def execute_step(
    step: WorksheetStep, env: dict[str, Quantity], scope
) -> tuple[WorksheetStepResult, Quantity]:
    """在当前环境与固定单位上下文下执行单个步骤。

    成功时返回 ``(响应结果, 供后续步骤引用的精确量)`` 并就地把该精确量
    写入 ``env[step.name]``；任何业务错误（未知变量/符号、量纲冲突、除零、
    非线性、零系数、仿射混算、目标单位无法解析、精度溢出等）都抛出
    :class:`~app.errors.ConversionError`，且不会修改 ``env``——调用方可以
    据此停在失败步，此前已完成步骤的结果与环境保持不变。
    """

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
    # 十进制化在产出时完成：非有限十进制或超精度即抛 422，env 不被修改
    result = WorksheetStepResult(
        name=step.name,
        value=to_decimal_str(value_fr),
        unit=unit,
        dimension=dimension,
    )
    # 仅在全部成功后把精确量写入环境：步骤间传递精确分数与绝对温度属性
    # （而非上面渲染出的十进制字符串），失败步不污染先前环境
    env[step.name] = quantity
    return result, quantity


def run_worksheet(req: WorksheetRequest, scope) -> list[WorksheetStepResult]:
    """顺序执行工作单，返回各步骤结果；任一步失败即抛出（422，无部分结果）。

    ``scope`` 为工作单开始时解析好的单位上下文（三种来源之一或 ``None``），
    整个工作单共用，保证别名只解析一次、各步骤使用同一版自定义单位定义。
    """

    # 初始变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    env = prepare_environment(req.variables, scope)

    results: list[WorksheetStepResult] = []
    for step in req.steps:
        result, _ = execute_step(step, env, scope)
        results.append(result)
    return results
