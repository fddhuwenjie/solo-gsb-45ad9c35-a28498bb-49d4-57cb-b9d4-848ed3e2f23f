"""Pydantic 请求/响应模型。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import MAX_PRECISION


def _coerce_value(v: Any) -> Any:
    """JSON -> Decimal 预处理：拒绝布尔，float 取最短十进制表示避免二进制污染。"""
    if isinstance(v, bool):
        raise ValueError("value 必须是十进制数")
    if isinstance(v, float):
        v = repr(v)
    return v


def _check_value(d: Decimal) -> Decimal:
    if not d.is_finite():
        raise ValueError("value 必须为有限十进制数")
    if len(d.as_tuple().digits) > MAX_PRECISION:
        raise ValueError(f"value 有效数字超过 {MAX_PRECISION} 位的精度上限")
    return d


class CustomUnitItem(BaseModel):
    """请求级自定义线性单位声明：``1 symbol = factor 个 base_unit``。"""

    symbol: str = Field(min_length=1, max_length=32, description="自定义单位符号")
    factor: Decimal = Field(
        gt=0, description="正十进制比例：1 symbol = factor 个 base_unit"
    )
    base_unit: str = Field(
        min_length=1,
        max_length=200,
        description="基准单位表达式（可引用本次声明的其他自定义单位；"
        "不得引用 degC、degF 或裸 K）",
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("factor", mode="before")
    @classmethod
    def _coerce_factor(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("factor")
    @classmethod
    def _check_factor(cls, d: Decimal) -> Decimal:
        return _check_value(d)


#: 三个换算接口均可携带的请求级自定义单位声明（1～6 项）。
CustomUnits = list[CustomUnitItem]


#: 活动别名（custom unit set alias）的词法：与 unit_sets.ALIAS_NAME_RE 一致。
ALIAS_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class CustomUnitsSelectionModel(BaseModel):
    """三个换算接口共用的自定义单位来源选择。

    自定义单位可以来自请求内的 ``custom_units``（仅本次请求有效）、已发布的
    定义集标识 ``custom_unit_set_id``，或活动别名 ``custom_unit_set_alias``
    （请求开始时解析一次，解析到哪一版就整次请求使用哪一版）。三者两两互斥，
    同时出现整次返回 422；都缺省时三个接口保持原有行为。
    """

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits | None = Field(
        default=None,
        min_length=1,
        max_length=6,
        description="请求级自定义线性单位声明（1～6 项），仅在本次请求内有效；"
        "与 custom_unit_set_id、custom_unit_set_alias 互斥",
    )
    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布自定义单位定义集的服务端标识；与 custom_units、"
        "custom_unit_set_alias 互斥，标识不存在时整次请求返回 422",
    )
    custom_unit_set_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=ALIAS_NAME_PATTERN,
        description="已发布定义集的活动别名；请求开始时解析一次，源、目标及"
        "变量单位始终使用解析到的该版定义；与 custom_units、"
        "custom_unit_set_id 互斥，别名不存在时整次请求返回 422",
    )

    @model_validator(mode="after")
    def _check_custom_units_sources_exclusive(self) -> "CustomUnitsSelectionModel":
        chosen = [
            name
            for name in (
                "custom_units",
                "custom_unit_set_id",
                "custom_unit_set_alias",
            )
            if getattr(self, name) is not None
        ]
        if len(chosen) > 1:
            raise ValueError(
                "、".join(chosen)
                + " 不能同时出现：一次调用只能选用一种自定义单位来源"
            )
        return self


class ConvertRequest(CustomUnitsSelectionModel):
    """POST /convert 请求体。

    ``value`` 建议以 JSON 字符串传递以保证十进制精确性；
    JSON 数值也会被接受（按其最短十进制表示解释）。
    """

    value: Decimal = Field(description="十进制定值")
    from_unit: str = Field(alias="from", min_length=1, max_length=200, description="源单位表达式")
    to_unit: str = Field(alias="to", min_length=1, max_length=200, description="目标单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


class Variable(BaseModel):
    """POST /evaluate 中的单个变量：带 ``value`` 与 ``unit`` 的映射值。"""

    value: Decimal = Field(description="变量的十进制定值")
    unit: str = Field(min_length=1, max_length=200, description="变量单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


class EvaluateRequest(CustomUnitsSelectionModel):
    """POST /evaluate 请求体。"""

    variables: dict[str, Variable] = Field(
        min_length=1, max_length=20, description="变量映射（1～20 个），键为公式中的变量名"
    )
    formula: str = Field(min_length=1, max_length=500, description="求值公式")
    output_unit: str = Field(min_length=1, max_length=200, description="结果目标单位表达式")

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables


class SolveRequest(CustomUnitsSelectionModel):
    """POST /solve 请求体。"""

    variables: dict[str, Variable] = Field(
        default_factory=dict,
        max_length=20,
        description="变量映射（0～20 个，可省略），键为方程中的变量名；不得包含 solve_for",
    )
    equation: str = Field(
        min_length=1, max_length=500, description="含恰好一个等号的方程"
    )
    solve_for: str = Field(min_length=1, max_length=32, description="待解未知量的名称")
    output_unit: str = Field(
        min_length=1, max_length=200, description="结果目标单位表达式（须为线性单位）"
    )

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables

    @field_validator("equation")
    @classmethod
    def _check_single_equals(cls, equation: str) -> str:
        if equation.count("=") != 1:
            raise ValueError("equation 必须恰好包含一个等号 '='")
        return equation

    @field_validator("solve_for")
    @classmethod
    def _check_solve_for_name(cls, name: str) -> str:
        if not _VAR_NAME_RE.match(name):
            raise ValueError(
                f"未知量名称 {name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        return name

    @model_validator(mode="after")
    def _check_solve_for_not_in_variables(self) -> "SolveRequest":
        if self.solve_for in self.variables:
            raise ValueError(f"solve_for {self.solve_for!r} 不得出现在 variables 中")
        return self


class UnitSetRef(BaseModel):
    """换版诊断中单侧对已发布定义集的引用：标识或活动别名恰好给出一个。

    ``custom_unit_set_id`` 与 ``custom_unit_set_alias`` 互斥（同时给出或
    都缺省均为 422）；别名在该侧执行开始时解析一次，固定到实际标识后再
    执行该侧全部步骤，执行期间别名被切换也不会造成该侧步骤混版。
    """

    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布定义集标识；与 custom_unit_set_alias 互斥，不存在返回 422",
    )
    custom_unit_set_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=ALIAS_NAME_PATTERN,
        description="已发布定义集的活动别名；该侧执行开始时解析一次并固定实际指向；"
        "与 custom_unit_set_id 互斥，别名不存在返回 422",
    )

    @model_validator(mode="after")
    def _check_exactly_one_ref(self) -> "UnitSetRef":
        chosen = [
            name
            for name in ("custom_unit_set_id", "custom_unit_set_alias")
            if getattr(self, name) is not None
        ]
        if len(chosen) != 1:
            raise ValueError(
                "必须且只能提供 custom_unit_set_id 或 custom_unit_set_alias 之一"
            )
        return self


class WorksheetStep(BaseModel):
    """``POST /worksheet`` 中的单个步骤。

    两种步骤恰好出现一种：``formula`` 走公式求值，``equation`` + ``solve_for``
    走一次线性方程反求。两种参数混给或缺省都使整次请求返回 422。
    后续步骤可按 ``name`` 引用本步骤结果；``name`` 在整个工作单中唯一，
    且不得与初始变量重名。
    """

    name: str = Field(
        min_length=1,
        max_length=32,
        description="本步骤结果名：唯一，后续步骤按此名引用，不得覆盖已有变量",
    )
    output_unit: str = Field(
        min_length=1, max_length=200, description="结果目标单位表达式"
    )
    formula: str | None = Field(
        default=None, min_length=1, max_length=500, description="求值公式（求值步骤）"
    )
    equation: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="含恰好一个等号的方程（反求步骤）",
    )
    solve_for: str | None = Field(
        default=None, min_length=1, max_length=32, description="待解未知量名称（反求步骤）"
    )

    @model_validator(mode="after")
    def _check_exactly_one_step_kind(self) -> "WorksheetStep":
        is_evaluate = self.formula is not None
        is_solve = self.equation is not None or self.solve_for is not None
        if is_evaluate and is_solve:
            raise ValueError(
                "formula 与 equation/solve_for 不能同时出现：每个步骤只能是"
                "公式求值或一次方程反求"
            )
        if not is_evaluate and not is_solve:
            raise ValueError(
                "每个步骤必须提供 formula（求值）或 equation 与 solve_for（反求）"
            )
        if is_solve and (self.equation is None or self.solve_for is None):
            raise ValueError("反求步骤必须同时提供 equation 与 solve_for")
        if not _VAR_NAME_RE.match(self.name):
            raise ValueError(
                f"结果名 {self.name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        if is_solve and not _VAR_NAME_RE.match(self.solve_for):
            raise ValueError(
                f"未知量名称 {self.solve_for!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        if is_solve and self.equation.count("=") != 1:
            raise ValueError("equation 必须恰好包含一个等号 '='")
        return self


class WorksheetBodyBase(BaseModel):
    """工作单请求体共用部分：初始变量 + 2～5 个顺序执行的步骤。

    不含自定义单位来源字段：``POST /worksheet`` 经
    :class:`CustomUnitsSelectionModel` 支持三种来源；换版诊断
    (:class:`WorksheetDiagnoseRequest`) 的两侧各自携带定义集引用。
    """

    variables: dict[str, Variable] = Field(
        default_factory=dict,
        max_length=20,
        description="初始带单位变量映射（0～20 个，可省略）；步骤结果不得覆盖这些名字",
    )
    steps: list[WorksheetStep] = Field(
        min_length=2,
        max_length=5,
        description="按顺序执行的步骤（2～5 个）；后续步骤可按结果名引用先前步骤",
    )

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables

    @model_validator(mode="after")
    def _check_step_names_unique_and_not_overriding(self) -> "WorksheetBodyBase":
        names: set[str] = set(self.variables)
        for step in self.steps:
            if step.name in names:
                raise ValueError(
                    f"结果名 {step.name!r} 与初始变量或先前步骤结果重名："
                    "步骤不能引用未执行步骤，也不能覆盖已有变量"
                )
            names.add(step.name)
        return self


class WorksheetRequest(WorksheetBodyBase, CustomUnitsSelectionModel):
    """POST /worksheet 请求体：初始变量 + 2～5 个顺序执行的步骤。"""


class WorksheetDiagnoseRequest(WorksheetBodyBase):
    """POST /worksheet-diagnose 请求体：同一份工作单 + 旧/新两份定义集引用。

    两侧分别固定实际指向（标识原样、别名在该侧开始时快照一次）后用各自
    定义执行同一工作单；请求不修改任何定义集或别名。
    """

    old: UnitSetRef = Field(
        alias="old", description="旧版已发布定义集的标识或活动别名引用"
    )
    new: UnitSetRef = Field(
        alias="new", description="新版已发布定义集的标识或活动别名引用"
    )

    model_config = ConfigDict(populate_by_name=True)


class WorksheetStepResult(BaseModel):
    """单个步骤的结果。"""

    name: str = Field(description="该步骤的结果名")
    value: str = Field(description="换算结果的精确十进制表示")
    unit: str = Field(description="目标单位表达式的规范形式")
    dimension: str = Field(description="规范量纲表达式（SI 基本单位）")


class WorksheetResponse(BaseModel):
    """POST /worksheet 的成功响应：各步骤按提交顺序给出结果。"""

    steps: list[WorksheetStepResult] = Field(
        description="各步骤结果（顺序与提交一致），供核对完整计算过程"
    )


# ---------------------------------------------------------------------------
# POST /worksheet-diagnose（工作单换版诊断）
# ---------------------------------------------------------------------------

#: 单侧/单步执行状态：成功；该步业务错误；失败步之后未执行；
#: 初始变量在该侧定义集下无法解析（整侧未开始）。
SIDE_STATUS_OK = "ok"
SIDE_STATUS_ERROR = "error"
SIDE_STATUS_NOT_RUN = "not_run"
SIDE_STATUS_INPUT_ERROR = "input_error"


class ResolvedUnitSetRef(BaseModel):
    """单侧引用在开始时固定下来的实际指向（回显给调用方核对）。"""

    kind: str = Field(
        description="引用类型：'id' 为直接按标识，'alias' 为按活动别名解析"
    )
    requested: str = Field(description="请求中给出的标识或别名")
    custom_unit_set_id: str = Field(description="开始时固定下来的实际定义集标识")


class DiagnoseError(BaseModel):
    """一侧或一步执行业务失败时的错误信息（错误码与消息同 422 响应体）。"""

    error: str = Field(description="机器可读错误码（与统一 422 错误码一致）")
    message: str = Field(description="人类可读错误信息")


class DiagnoseSideInfo(BaseModel):
    """一侧（旧版或新版）的执行概况。"""

    ref: ResolvedUnitSetRef = Field(description="开始时固定的实际指向")
    status: str = Field(
        description="该侧状态：'ok' 全部步骤成功；'error' 某步骤业务失败；"
        "'input_error' 初始变量单位在该定义集下无法解析（整侧未开始）"
    )
    error: DiagnoseError | None = Field(
        default=None,
        description="该侧首个业务错误（步骤错误或初始变量解析错误）；成功时为 null",
    )


class DiagnoseStepSide(BaseModel):
    """单步在一侧的执行结果。"""

    status: str = Field(
        description="'ok' 成功；'error' 该步业务失败；'not_run' 该侧此前已失败、"
        "本步未执行（不计为成功）"
    )
    value: str | None = Field(
        default=None, description="该步目标单位下的精确十进制字符串（仅成功时给出）"
    )
    unit: str | None = Field(
        default=None, description="目标单位的规范形式（仅成功时给出）"
    )
    dimension: str | None = Field(
        default=None, description="规范量纲表达式（仅成功时给出）"
    )
    value_si: str | None = Field(
        default=None,
        description="内部精确物理值（SI 基单位口径分数，如 '120'、'2/3'）；"
        "跨版比较以此为准，而非显示字符串（仅成功时给出）",
    )
    value_si_dimension: str | None = Field(
        default=None,
        description="value_si 的内部量纲（SI 基本单位规范表达式，仅成功时给出）",
    )
    absolute: bool | None = Field(
        default=None,
        description="该步结果是否为绝对温度（携带仿射偏移）；仅成功时给出",
    )
    error: DiagnoseError | None = Field(
        default=None, description="该步业务错误（仅失败时给出）"
    )


class DiagnoseStepComparison(BaseModel):
    """同一步骤在两侧的执行结果与逐步比较结论。"""

    index: int = Field(description="步骤序号（从 1 开始，与提交顺序一致）")
    name: str = Field(description="该步骤的结果名")
    old: DiagnoseStepSide = Field(description="旧版定义集下的执行结果")
    new: DiagnoseStepSide = Field(description="新版定义集下的执行结果")
    meaning_changed: bool = Field(
        description="该步含义是否变化：内部物理值/量纲/绝对温度属性变化，"
        "或执行状态（成功↔失败、错误码）变化；仅显示字符串变化不算"
    )
    change_type: str | None = Field(
        default=None,
        description="首个含义变化类型：'physical_value'（内部精确物理值变化）、"
        "'dimension'（量纲变化）、'absolute_temperature'（绝对温度属性变化）、"
        "'execution_status'（一侧成功一侧失败）、'error_code'（两侧均失败但错误码不同）；"
        "未变化时为 null",
    )
    display_changed: bool = Field(
        description="两侧都成功时，目标单位下的显示结果（value/unit 字符串）是否不同；"
        "仅用于核对呈现差异：即使显示字符串相同，内部物理值变化仍以 "
        "meaning_changed/change_type=physical_value 报告",
    )


class WorksheetDiagnoseResponse(BaseModel):
    """POST /worksheet-diagnose 的成功响应：两侧概况、逐步对照与首个含义变化。"""

    old: DiagnoseSideInfo = Field(description="旧版一侧的执行概况")
    new: DiagnoseSideInfo = Field(description="新版一侧的执行概况")
    meaning_changed: bool = Field(
        description="整个工作单是否存在含义变化（任一步 meaning_changed 即为 true）"
    )
    first_changed_step: int | None = Field(
        default=None,
        description="首个含义变化的步骤序号（从 1 开始）；两侧完全一致时为 null。"
        "若差异在初始变量解析阶段（一侧 input_error），记为 1",
    )
    steps: list[DiagnoseStepComparison] = Field(
        description="逐步对照（覆盖提交的全部步骤；失败步之后的步骤标记为 not_run）"
    )


class ConvertResponse(BaseModel):
    """POST /convert、POST /evaluate 与 POST /solve 的成功响应。"""

    value: str = Field(description="换算结果的精确十进制表示")
    unit: str = Field(description="目标单位表达式的规范形式")
    dimension: str = Field(description="规范量纲表达式（SI 基本单位）")


class PublishUnitSetRequest(BaseModel):
    """POST /custom-unit-sets 请求体：提交 1～6 项 custom_units 格式定义。"""

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits = Field(
        min_length=1,
        max_length=6,
        description="待发布的自定义线性单位声明（1～6 项），格式与三个换算接口"
        "的请求级 custom_units 完全一致；发布前按现有规则校验并展开全部条目",
    )


class PublishedCustomUnitItem(BaseModel):
    """已发布定义集回显的单条定义（factor 为精确十进制字符串）。"""

    symbol: str = Field(description="自定义单位符号")
    factor: str = Field(description="正十进制比例：1 symbol = factor 个 base_unit")
    base_unit: str = Field(description="基准单位表达式")


class PublishedUnitSetResponse(BaseModel):
    """POST /custom-unit-sets 的成功响应。"""

    id: str = Field(description="服务生成的定义集标识（不可猜测；已发布内容不可修改）")
    custom_units: list[PublishedCustomUnitItem] = Field(
        description="已发布的定义项（factor 回显为精确十进制字符串），顺序与提交一致"
    )


class BindUnitSetAliasRequest(BaseModel):
    """PUT /custom-unit-set-aliases/{alias} 请求体。"""

    custom_unit_set_id: str = Field(
        min_length=1,
        max_length=64,
        description="别名要指向的已发布定义集标识；不存在时返回 422 且不改变指向",
    )
    expected_id: str | None = Field(
        description="乐观并发：调用方认为别名当前指向的标识；初建别名时必须为 "
        "null。仅当当前指向与此一致时切换才生效，否则返回 409",
    )


class UnitSetAliasInfo(BaseModel):
    """单个活动别名的当前状态。"""

    alias: str = Field(description="活动别名（调用方长期沿用的名称）")
    custom_unit_set_id: str = Field(
        description="当前指向的已发布定义集标识（不可变定义集的引用）"
    )


class UnitSetAliasListResponse(BaseModel):
    """GET /custom-unit-set-aliases 的成功响应。"""

    aliases: list[UnitSetAliasInfo] = Field(
        description="全部活动别名及其当前指向（按别名排序）"
    )


class ErrorDetail(BaseModel):
    error: str = Field(description="机器可读错误码")
    message: str = Field(description="人类可读错误信息")


class ErrorResponse(BaseModel):
    detail: ErrorDetail
