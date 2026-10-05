"""POST /worksheet-diagnose（工作单换版诊断）端到端测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app, raise_server_exceptions=True)


def var(value, unit):
    return {"value": value, "unit": unit}


def ev_step(name, formula, output_unit):
    return {"name": name, "formula": formula, "output_unit": output_unit}


def solve_step(name, equation, solve_for, output_unit):
    return {
        "name": name,
        "equation": equation,
        "solve_for": solve_for,
        "output_unit": output_unit,
    }


def publish(custom_units):
    r = client.post("/custom-unit-sets", json={"custom_units": custom_units})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def diag(steps, old, new, variables=None):
    body: dict = {"steps": steps, "old": old, "new": new}
    if variables is not None:
        body["variables"] = variables
    return client.post("/worksheet-diagnose", json=body)


def by_id(set_id):
    return {"custom_unit_set_id": set_id}


def by_alias(name):
    return {"custom_unit_set_alias": name}


# ---------------------------------------------------------------------------
# 逐步对照：比较内部精确物理值而非显示字符串
# ---------------------------------------------------------------------------


def test_display_identical_but_internal_physical_value_changed():
    # 旧版 span=2 m、新版 span=3 m：输入 1 span、输出 span，
    # 两侧 value/unit 都显示 "1"/"span"，但物理值（2 m vs 3 m）已变
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x", "span"), ev_step("b", "x", "m")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span")},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["meaning_changed"] is True
    assert j["first_changed_step"] == 1

    s1 = j["steps"][0]
    assert s1["old"]["value"] == "1" and s1["new"]["value"] == "1"
    assert s1["old"]["unit"] == "span" and s1["new"]["unit"] == "span"
    # 显示字符串完全相同……
    assert s1["display_changed"] is False
    # ……但内部精确物理值不同，仍须报告含义变化
    assert s1["old"]["value_si"] == "2"
    assert s1["new"]["value_si"] == "3"
    assert s1["change_type"] == "physical_value"
    assert s1["meaning_changed"] is True

    # 第二步输出 m：显示字符串与内部物理值都不同
    s2 = j["steps"][1]
    assert (s2["old"]["value"], s2["new"]["value"]) == ("2", "3")
    assert s2["display_changed"] is True
    assert s2["change_type"] == "physical_value"


def test_value_si_is_exact_fraction_not_rounded_decimal():
    # 1 ft = 0.3048 m = 381/1250 m：内部值以精确分数字符串逐位给出
    v1 = publish([{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}])
    v2 = publish([{"symbol": "ft", "factor": "0.30480", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x", "ft"), ev_step("b", "x*1", "ft")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "ft")},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["meaning_changed"] is False
    assert j["first_changed_step"] is None
    assert j["steps"][0]["old"]["value_si"] == "381/1250"
    assert j["steps"][0]["new"]["value_si"] == "381/1250"
    # 显示值为十进制字符串 "1"（输出单位 ft），与内部分数并存
    assert j["steps"][0]["old"]["value"] == "1"


def test_identical_definitions_mean_no_change_and_echoes_resolved_refs():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    r = diag(
        [ev_step("a", "x", "m"), ev_step("b", "a*2", "m")],
        by_id(v1),
        by_alias("lens"),
        variables={"x": var("1", "span")},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["meaning_changed"] is False
    assert j["first_changed_step"] is None
    assert all(s["meaning_changed"] is False for s in j["steps"])
    # 两侧开始时固定的实际指向都被回显（别名解析到标识）
    assert j["old"]["ref"] == {
        "kind": "id",
        "requested": v1,
        "custom_unit_set_id": v1,
    }
    assert j["new"]["ref"] == {
        "kind": "alias",
        "requested": "lens",
        "custom_unit_set_id": v1,
    }


def test_dimension_change_detected():
    # 同名符号 span：旧版是时间（=1 s），新版是长度（=3 m）
    v1 = publish([{"symbol": "span", "factor": "1", "base_unit": "s"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x*2", "span"), ev_step("b", "x", "span")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span")},
    )
    s = r.json()["steps"][0]
    assert s["old"]["value_si_dimension"] == "s"
    assert s["new"]["value_si_dimension"] == "m"
    # 物理值（1*1*2=2 s vs 3*2=6 m）与量纲都不同，首个差异按物理值报告
    assert s["meaning_changed"] is True
    assert s["change_type"] == "physical_value"
    assert r.json()["first_changed_step"] == 1


def test_dimension_only_change_when_value_coincides():
    # 因子都为 1、输入都为 1、输出同符号：value_si 都是 "1"，但量纲 s vs m
    v1 = publish([{"symbol": "u", "factor": "1", "base_unit": "s"}])
    v2 = publish([{"symbol": "u", "factor": "1", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x", "u"), ev_step("b", "x*1", "u")],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "u")},
    )
    s = r.json()["steps"][0]
    assert s["old"]["value_si"] == "1" and s["new"]["value_si"] == "1"
    assert s["old"]["value_si_dimension"] == "s"
    assert s["new"]["value_si_dimension"] == "m"
    assert s["change_type"] == "dimension"
    assert r.json()["first_changed_step"] == 1


def test_absolute_temperature_attribute_is_carried_and_compared():
    # 绝对温度传递步：两侧定义集不同但温度变量与目标单位相同，
    # 两侧都应报告 absolute=true 且含义不变
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    r = diag(
        [
            ev_step("s", "v*t", "km"),
            ev_step("T", "T0", "degC"),
        ],
        by_id(v1),
        by_id(v2),
        variables={
            "v": var("60", "km/h"),
            "t": var("2", "h"),
            "T0": var("98.6", "degF"),
        },
    )
    assert r.status_code == 200, r.text
    s1, s2 = r.json()["steps"]
    assert s1["meaning_changed"] is False
    assert s1["old"]["absolute"] is False
    assert s2["old"]["absolute"] is True and s2["new"]["absolute"] is True
    assert s2["old"]["value"] == "37" and s2["new"]["value"] == "37"
    assert s2["meaning_changed"] is False
    assert r.json()["meaning_changed"] is False


def test_mixed_evaluate_and_solve_steps_compared():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "4", "base_unit": "m"}])
    r = diag(
        [
            ev_step("w", "x", "m"),
            # 反求 k：2*k*span = w（span 由先前步 x 提供精确量）；
            # 旧版 k = 2/(2*2) = 1/2，新版 k = 4/(2*4) = 1/2，第二步两侧一致
            solve_step("k", "2*k*x = w", "k", "1"),
        ],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span")},
    )
    j = r.json()
    assert j["steps"][0]["old"]["value_si"] == "2"
    assert j["steps"][0]["new"]["value_si"] == "4"
    assert j["steps"][1]["old"]["status"] == "ok"
    assert j["steps"][1]["new"]["status"] == "ok"
    assert j["steps"][1]["old"]["value_si"] == "1/2"
    assert j["steps"][1]["new"]["value_si"] == "1/2"
    assert j["steps"][1]["meaning_changed"] is False
    assert j["first_changed_step"] == 1


# ---------------------------------------------------------------------------
# 单侧失败：保留此前步骤对照，报告该步错误，失败后的步骤不计为成功
# ---------------------------------------------------------------------------


def test_one_side_fails_at_step_k_prior_comparison_retained():
    # span 旧版是时间、新版是长度：
    # 第 1 步两侧成功；第 2 步 x+t 旧版（s+s）成功，新版（m+s）量纲冲突；
    # 第 3 步旧版成功，新版因第 2 步失败而 not_run
    v1 = publish([{"symbol": "span", "factor": "1", "base_unit": "s"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    r = diag(
        [
            ev_step("a", "x", "span"),
            ev_step("b", "x+t", "s"),
            ev_step("c", "a*2", "span"),
        ],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span"), "t": var("1", "s")},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["old"]["status"] == "ok" and j["old"]["error"] is None
    assert j["new"]["status"] == "error"
    assert j["new"]["error"]["error"] == "dimension_mismatch"

    s1, s2, s3 = j["steps"]
    assert s1["old"]["status"] == "ok" and s1["new"]["status"] == "ok"
    assert s1["meaning_changed"] is True  # 物理值/量纲已不同

    assert s2["old"]["status"] == "ok"
    assert s2["new"]["status"] == "error"
    assert s2["new"]["error"]["error"] == "dimension_mismatch"
    assert s2["change_type"] == "execution_status"

    # 失败后的步骤：旧版正常返回，新版 not_run，不计为成功，也不重复报变化
    assert s3["old"]["status"] == "ok"
    assert s3["new"]["status"] == "not_run"
    assert s3["meaning_changed"] is False
    assert s3["change_type"] is None


def test_both_sides_fail_same_step_same_error_is_not_a_change():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])
    r = diag(
        [
            ev_step("a", "x", "m"),
            ev_step("b", "x+t", "m"),  # 两侧都是 m+s 量纲冲突
            ev_step("c", "a*2", "m"),
        ],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span"), "t": var("1", "s")},
    )
    j = r.json()
    assert j["old"]["status"] == "error" and j["new"]["status"] == "error"
    s1, s2, s3 = j["steps"]
    # 第 1 步物理值已变（2 vs 3）
    assert s1["change_type"] == "physical_value"
    # 第 2 步两侧同一错误码：不视为新的含义变化
    assert s2["old"]["error"]["error"] == "dimension_mismatch"
    assert s2["new"]["error"]["error"] == "dimension_mismatch"
    assert s2["meaning_changed"] is False
    assert s2["change_type"] is None
    # 失败后的步骤两侧均未执行
    assert s3["old"]["status"] == "not_run"
    assert s3["new"]["status"] == "not_run"


def test_input_variable_unit_unknown_in_one_set_is_side_input_error():
    old_id = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    new_id = publish([{"symbol": "qq", "factor": "2", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x", "m"), ev_step("b", "x", "m")],
        by_id(old_id),
        by_id(new_id),
        variables={"x": var("1", "zz")},  # zz 只在旧版定义集中存在
    )
    j = r.json()
    assert j["old"]["status"] == "ok"
    assert j["new"]["status"] == "input_error"
    assert j["new"]["error"]["error"] == "unknown_symbol"
    s1, s2 = j["steps"]
    assert s1["old"]["status"] == "ok" and s1["new"]["status"] == "error"
    assert s1["new"]["error"]["error"] == "unknown_symbol"
    assert s1["change_type"] == "execution_status"
    assert s2["old"]["status"] == "ok" and s2["new"]["status"] == "not_run"
    assert j["first_changed_step"] == 1


def test_input_error_same_code_on_both_sides_is_not_a_change():
    old_id = publish([{"symbol": "aa", "factor": "2", "base_unit": "m"}])
    new_id = publish([{"symbol": "bb", "factor": "2", "base_unit": "m"}])
    r = diag(
        [ev_step("a", "x", "m"), ev_step("b", "x", "m")],
        by_id(old_id),
        by_id(new_id),
        variables={"x": var("1", "zz")},  # zz 在两侧都不存在
    )
    j = r.json()
    assert j["old"]["status"] == "input_error"
    assert j["new"]["status"] == "input_error"
    assert j["meaning_changed"] is False
    assert j["first_changed_step"] is None
    assert j["steps"][0]["old"]["error"]["error"] == "unknown_symbol"
    assert j["steps"][0]["new"]["error"]["error"] == "unknown_symbol"


# ---------------------------------------------------------------------------
# 422：未知定义集/别名、非法请求（在任何执行开始前拒绝）
# ---------------------------------------------------------------------------


def test_unknown_set_id_is_422():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [ev_step("a", "x", "m"), ev_step("b", "x", "m")],
        "old": by_id(v1),
        "new": by_id("cus_does_not_exist"),
    }
    r = client.post("/worksheet-diagnose", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"


def test_unknown_alias_is_422():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [ev_step("a", "x", "m"), ev_step("b", "x", "m")],
        "old": by_id(v1),
        "new": by_alias("missing"),
    }
    r = client.post("/worksheet-diagnose", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"


def test_ref_must_have_exactly_one_of_id_or_alias():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    base = [ev_step("a", "x", "m"), ev_step("b", "x", "m")]
    vars_ = {"x": var("1", "span")}

    bad_refs = [
        {},  # 两者都缺省
        {"custom_unit_set_id": v1, "custom_unit_set_alias": "lens"},  # 同时给出
    ]
    for ref in bad_refs:
        r = diag(base, by_id(v1), ref, variables=vars_)
        assert r.status_code == 422, r.text


def test_invalid_worksheet_body_is_422():
    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])

    # 步骤数必须为 2～5
    r = diag([ev_step("a", "x", "m")], by_id(v1), by_id(v2),
             variables={"x": var("1", "span")})
    assert r.status_code == 422

    # 缺少 old/new
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [ev_step("a", "x", "m"), ev_step("b", "x", "m")],
    }
    assert client.post("/worksheet-diagnose", json=body).status_code == 422


# ---------------------------------------------------------------------------
# 别名在两侧各自开始时固定一次：执行中切换不造成同侧混版
# ---------------------------------------------------------------------------


def test_each_side_pins_alias_at_its_own_start(monkeypatch):
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "m"}])
    client.put(
        "/custom-unit-set-aliases/mov",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    store = unit_sets.get_store()
    orig_build = store.build_scope

    # 每一侧的 scope 构造完成后立即把别名切走：该侧仍须使用构造时的版本，
    # 两个步骤不得混版；另一侧开始时解析到切换后的新版本
    def switched_build(set_id):
        scope = orig_build(set_id)
        client.put(
            "/custom-unit-set-aliases/mov",
            json={
                "custom_unit_set_id": v2 if set_id == v1 else v1,
                "expected_id": set_id,
            },
        )
        return scope

    monkeypatch.setattr(store, "build_scope", switched_build)
    r = diag(
        [ev_step("p", "x", "m"), ev_step("q", "x*2", "m")],
        by_alias("mov"),
        by_alias("mov"),
        variables={"x": var("1", "zz")},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    # 旧侧开始时别名 -> v1，新侧开始时别名已被切到 v2
    assert j["old"]["ref"]["custom_unit_set_id"] == v1
    assert j["new"]["ref"]["custom_unit_set_id"] == v2
    # 旧侧两步都按 v1（2、4）；新侧两步都按 v2（3、6），无同侧混版
    assert [s["old"]["value_si"] for s in j["steps"]] == ["2", "4"]
    assert [s["new"]["value_si"] for s in j["steps"]] == ["3", "6"]


def test_different_error_codes_at_same_step_is_a_change(monkeypatch):
    # 两侧在同一步都失败但错误码不同（如一侧量纲冲突、另一侧除零）：
    # 该步记为含义变化 error_code。通过让新版（span=3 m）一侧的第一步
    # 改抛 division_by_zero，模拟“定义差异导致同一步触发不同业务规则”。
    from fractions import Fraction

    from app import diagnose as diag_mod
    from app.errors import ConversionError

    v1 = publish([{"symbol": "span", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "span", "factor": "3", "base_unit": "m"}])

    orig_execute = diag_mod.execute_step
    call = {"n": 0}

    def patched_execute(step, env, scope):
        # 仅新版作用域（span 展开为 3 m）的第一个步骤改报除零
        is_new_scope = scope.resolve_symbol("span").scale == Fraction(3)
        if is_new_scope and call["n"] == 0:
            call["n"] += 1
            raise ConversionError("division_by_zero", "除数为零")
        return orig_execute(step, env, scope)

    monkeypatch.setattr(diag_mod, "execute_step", patched_execute)

    r = diag(
        [
            ev_step("a", "x+t", "m"),  # 旧版 m+s 量纲冲突；新版被强制为除零
            ev_step("b", "x", "m"),
        ],
        by_id(v1),
        by_id(v2),
        variables={"x": var("1", "span"), "t": var("1", "s")},
    )
    j = r.json()
    s1 = j["steps"][0]
    assert s1["old"]["status"] == "error"
    assert s1["new"]["status"] == "error"
    assert s1["old"]["error"]["error"] == "dimension_mismatch"
    assert s1["new"]["error"]["error"] == "division_by_zero"
    assert s1["change_type"] == "error_code"
    assert s1["meaning_changed"] is True
    assert j["first_changed_step"] == 1
    assert j["steps"][1]["old"]["status"] == "not_run"
    assert j["steps"][1]["new"]["status"] == "not_run"


def test_worksheet_endpoint_still_all_or_nothing_422():
    # 对照：/worksheet 任一步错误仍只返回 422、不返回部分结果（行为不变）
    body = {
        "variables": {"x": var("1", "m"), "t": var("1", "s")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x+t", "m"),  # 量纲冲突
            ev_step("c", "a*2", "m"),
        ],
    }
    r = client.post("/worksheet", json=body)
    assert r.status_code == 422
    assert "steps" not in r.json()
    assert r.json()["detail"]["error"] == "dimension_mismatch"
