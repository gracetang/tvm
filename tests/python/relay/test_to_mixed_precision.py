# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Unit tests for testing ToMixedPrecision pass"""
from typing import Any, Dict, List

import numpy as np
import pytest
import tvm
from tvm import relay
from tvm.relay.testing import lstm
from tvm.relay.transform import InferType, ToMixedPrecision, mixed_precision


def run_module(mod: tvm.runtime.Module, mod_params: Dict[str, Any]) -> List:
    dev = tvm.device("llvm", 0)
    intrp = relay.create_executor("debug", mod, device=dev, target="llvm")
    result = intrp.evaluate()(**mod_params)
    if isinstance(result, tvm.runtime.container.ADT):
        result = [r.asnumpy() for r in result]
        return result
    else:
        return [result.asnumpy()]


def verify_mixed_precision_output_close(
    mod: tvm.runtime.Module,
    mod_params: Dict[str, Any],
    mixed_precision_dtype="float16",
    rtol: float = 1e-3,
    atol: float = 0,
) -> tvm.runtime.Module:

    mod = InferType()(mod)
    result_fp32 = run_module(mod, mod_params)
    fp16_mod = ToMixedPrecision(mixed_precision_dtype)(mod)
    result_fp16 = run_module(fp16_mod, mod_params)

    # Ensure the results are close
    for fp32, fp16 in zip(result_fp32, result_fp16):
        np.testing.assert_allclose(fp32, fp16, rtol=rtol, atol=atol)

    return fp16_mod


def test_lstm():
    """A small stress test on a single unrolled lstm unit.

    Has internal functions and let statements the pass must work on.
    """
    # TODO(AndrewZhaoLuo): investigate why non-even units cause failure in codegen for CUDA
    # See discussion here: https://github.com/apache/tvm/issues/8294#issuecomment-866190408
    units = 4
    iterations = 5
    mod, mod_params = lstm.get_workload(iterations=iterations, num_hidden=units)

    # This is an unrolled lstm so each data should be the previous results but
    # we don't care, we just want to stress test things.
    for i in range(iterations):
        mod_params["data" if i == 0 else f"data{i}"] = np.random.uniform(
            -10, 10, (1, units)
        ).astype("float32")

    verify_mixed_precision_output_close(mod, mod_params, rtol=0.01, atol=0.01)


def test_lstm_float64():
    """Tests if can handle other mixed precision types.

    As a toy example show can convert graph to float64 and have it run.

    It doesn't really make sense to do it, this just shows we can change
    the target mixed_precision_dtype.
    """
    units = 3
    iterations = 5
    mod, mod_params = lstm.get_workload(iterations=iterations, num_hidden=units)

    # This is an unrolled lstm so each data should be the previous results but
    # we don't care, we just want to stress test things.
    for i in range(iterations):
        mod_params["data" if i == 0 else f"data{i}"] = np.random.uniform(
            -10, 10, (1, units)
        ).astype("float32")

    verify_mixed_precision_output_close(
        mod, mod_params, mixed_precision_dtype="float64", rtol=0.01, atol=0.01
    )


def test_convert_single_conv():
    """Conv is a green listed operation meaning it will always use fp16 workload.

    By default it accumulates to fp32 and outputs fp16.
    """
    data_shape = (1, 3, 32, 32)
    weight_shape = (5, 3, 3, 3)
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float32")
    mod = tvm.IRModule.from_expr(conv)
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "data": np.random.uniform(-1, 1, size=data_shape).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=weight_shape).astype("float32"),
    }
    fp16_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=1e-3)

    expected_mod = tvm.IRModule.from_expr(
        relay.nn.conv2d(
            relay.cast(data, "float16"),
            relay.cast(weight, "float16"),
            strides=(1, 1),
            padding=(1, 1),
            out_dtype="float16",
        ),
    )
    expected_mod = tvm.relay.transform.InferType()(expected_mod)

    assert not tvm.ir.structural_equal(fp16_mod, mod)
    assert tvm.ir.structural_equal(fp16_mod, expected_mod)


def test_convert_single_conv_fp64():
    """As above but checks choosing a mixed_precision_type other than FP16 works"""
    data_shape = (1, 3, 32, 32)
    weight_shape = (5, 3, 3, 3)
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float32")
    mod = tvm.IRModule.from_expr(conv)
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "data": np.random.uniform(-1, 1, size=data_shape).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=weight_shape).astype("float32"),
    }
    fp16_mod = verify_mixed_precision_output_close(
        mod, mod_params, mixed_precision_dtype="float64", atol=0.01, rtol=1e-3
    )

    # Note we still accumulate to FP32 by default, a user would need to overwrite default
    # behavior to make this make more sense.
    expected_mod = tvm.IRModule.from_expr(
        relay.nn.conv2d(
            relay.cast(data, "float64"),
            relay.cast(weight, "float64"),
            strides=(1, 1),
            padding=(1, 1),
            out_dtype="float64",
        ),
    )
    expected_mod = tvm.relay.transform.InferType()(expected_mod)

    assert not tvm.ir.structural_equal(fp16_mod, mod)
    assert tvm.ir.structural_equal(fp16_mod, expected_mod)


def test_convert_conv_bn():
    """Conv is green and batch norm is gray. As Conv should output fp16 batch_norm should be green."""
    data_shape = (1, 3, 32, 32)
    weight_shape = (5, 3, 3, 3)
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float32")

    bn_shape = [5]
    gamma = relay.var("gamma", shape=bn_shape)
    beta = relay.var("beta", shape=bn_shape)
    moving_mean = relay.var("moving_mean", shape=bn_shape)
    moving_var = relay.var("moving_var", shape=bn_shape)
    bn = relay.nn.batch_norm(conv, gamma, beta, moving_mean, moving_var)
    mod = tvm.IRModule.from_expr(bn[0])
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "data": np.random.uniform(-1, 1, size=data_shape).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=weight_shape).astype("float32"),
        "gamma": np.random.uniform(-1, 1, size=bn_shape).astype("float32"),
        "beta": np.random.uniform(-1, 1, size=bn_shape).astype("float32"),
        "moving_mean": np.random.uniform(-1, 1, size=bn_shape).astype("float32"),
        "moving_var": np.random.uniform(-1, 1, size=bn_shape).astype("float32"),
    }
    fp16_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.025, rtol=0.01)

    # Creating expected module
    data = relay.cast(relay.var("data", shape=data_shape), "float16")
    weight = relay.cast(relay.var("weight", shape=weight_shape), "float16")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float16")

    bn_shape = [5]
    gamma = relay.cast(relay.var("gamma", shape=bn_shape), "float16")
    beta = relay.cast(relay.var("beta", shape=bn_shape), "float16")
    moving_mean = relay.cast(relay.var("moving_mean", shape=bn_shape), "float16")
    moving_var = relay.cast(relay.var("moving_var", shape=bn_shape), "float16")
    bn = relay.nn.batch_norm(conv, gamma, beta, moving_mean, moving_var)

    expected_mod = tvm.IRModule.from_expr(bn[0])
    expected_mod = tvm.relay.transform.InferType()(expected_mod)
    assert not tvm.ir.structural_equal(fp16_mod, mod)
    assert tvm.ir.structural_equal(fp16_mod, expected_mod)


def test_do_not_convert_softmax():
    """Softmax is a red listed operation and therefore should never be fp16."""
    shape = [1, 2, 3]
    a = relay.var("a", shape=shape)
    b = relay.nn.softmax(a)
    mod = tvm.IRModule.from_expr(b)
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "a": np.random.uniform(-1, 1, size=shape).astype("float32"),
    }
    output_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.0, rtol=0)
    assert tvm.ir.structural_equal(mod, output_mod)


def test_do_not_convert_arange():
    """Arange is a red listed operation and therefore should never be fp16."""
    dtype = "float32"
    arange = relay.arange(relay.const(1, dtype), relay.const(128, dtype))
    mod = tvm.IRModule.from_expr(arange)
    mod = tvm.relay.transform.InferType()(mod)

    output_mod = verify_mixed_precision_output_close(mod, {}, atol=0.0, rtol=0)
    assert tvm.ir.structural_equal(mod, output_mod)


def test_green_gray_propagates_simple():
    """Conv is a green listed operation, while addition is gray.

    As Conv outputs fp16 the add should be done in fp16.
    """
    data_shape = (1, 3, 32, 32)
    weight_shape = (5, 3, 3, 3)
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float32")
    conv = conv + conv
    mod = tvm.IRModule.from_expr(conv)
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "data": np.random.uniform(-1, 1, size=data_shape).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=weight_shape).astype("float32"),
    }
    fp16_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=0.01)

    conv_expr = relay.nn.conv2d(
        relay.cast(data, "float16"),
        relay.cast(weight, "float16"),
        strides=(1, 1),
        padding=(1, 1),
        out_dtype="float16",
    )
    expected_mod = tvm.IRModule.from_expr(conv_expr + conv_expr)
    expected_mod = tvm.relay.transform.InferType()(expected_mod)

    assert not tvm.ir.structural_equal(fp16_mod, mod)
    assert tvm.ir.structural_equal(fp16_mod, expected_mod)


def test_green_red_not_use_extraneous_cast():
    """Conv. is a green listed operation, while softmax is red.

    Conv. also by default accumulates to fp32 but outputs fp16.

    We want to avoid a situation where we have extraneous casts.
    E.g. because softmax wants to operate on FP32 we might have

    conv (FP32) -> cast (FP16) -> cast (FP32) -> softmax (FP32)

    To get around this internally when we cast in the pass we cache
    the output nodes and the reverse of the cast back to the original
    node. For example casting the `conv (FP32)` to FP16 would produce:

    `conv (FP32) -> cast (FP16)`

    As the outputs. Now anytime we try to cast the `conv (FP32)` node
    to FP16 it would return the cached result instead of a new cast node:

    `conv (FP32) -> cast (FP16)`

    Furthermore, if we try to cast the `cast (FP16)` node back to FP32 it
    would just return

    `conv (FP32)`.

    This test makes sure this behavior occurs.
    """
    data_shape = (1, 3, 32, 32)
    weight_shape = (5, 3, 3, 3)
    data = relay.var("data", shape=data_shape, dtype="float32")
    weight = relay.var("weight", shape=weight_shape, dtype="float32")
    conv = relay.nn.conv2d(data, weight, strides=(1, 1), padding=(1, 1), out_dtype="float32")
    result = relay.nn.softmax(conv)
    mod = tvm.IRModule.from_expr(result)

    mod_params = {
        "data": np.random.uniform(-1, 1, size=data_shape).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=weight_shape).astype("float32"),
    }
    fp16_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=1e-3)

    # Construct expected structure
    conv = relay.cast(
        relay.nn.conv2d(
            relay.cast(data, "float16"),
            relay.cast(weight, "float16"),
            strides=(1, 1),
            padding=(1, 1),
            out_dtype="float16",
        ),
        "float32",
    )
    result = relay.nn.softmax(conv)
    expected_mod = tvm.IRModule.from_expr(result)
    expected_mod = InferType()(expected_mod)

    assert tvm.ir.structural_equal(expected_mod, fp16_mod)


def test_red_gray_propagates_simple():
    """Everything after a softmax should be in FP32 (exception green colored ops)"""
    shape = [1, 2, 3]
    a = relay.var("a", shape=shape)
    b = relay.nn.softmax(a)
    c = b + b
    mod = tvm.IRModule.from_expr(c)
    mod = tvm.relay.transform.InferType()(mod)

    mod_params = {
        "a": np.random.uniform(-1, 1, size=shape).astype("float32"),
    }
    output_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.0, rtol=0.0)

    assert tvm.ir.structural_equal(mod, output_mod)


def test_let_statement_simple():
    """A 'simple' let statement example.

    Noticeable is the mutation of the bound variable types.
    """
    var1 = relay.var("var1", shape=[1, 20])
    var2 = relay.var("var2", shape=[1, 20])

    data = relay.var("data", shape=[1, 20])
    weight = relay.var("weight", shape=[20, 20])

    r1 = var1 + var1

    r2 = var2 + var2
    let2 = relay.Let(var2, relay.nn.dense(r1, weight, units=20), r2)
    let1 = relay.Let(var1, relay.nn.dense(data, weight, units=20), let2)

    mod = tvm.IRModule.from_expr(let1)
    mod_params = {
        "data": np.random.uniform(-1, 1, size=[1, 20]).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=[20, 20]).astype("float32"),
    }
    output_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=0.01)

    # Construct expected structure
    var1 = relay.var("var1", shape=[1, 20], dtype="float16")
    var2 = relay.var("var2", shape=[1, 20], dtype="float16")
    data = relay.cast(relay.var("data", shape=[1, 20]), "float16")
    weight = relay.cast(relay.var("weight", shape=[20, 20]), "float16")
    r1 = var1 + var1
    r2 = var2 + var2
    let2 = relay.Let(
        var2,
        relay.nn.dense(r1, weight, units=20, out_dtype="float16"),
        r2,
    )
    let1 = relay.Let(
        var1,
        relay.nn.dense(data, weight, units=20, out_dtype="float16"),
        let2,
    )
    expected_mod = tvm.IRModule.from_expr(let1)
    expected_mod = InferType()(expected_mod)

    assert tvm.ir.structural_equal(expected_mod, output_mod)


def test_where_simple():
    data = relay.var("data", shape=[1, 20])
    weight = relay.var("weight", shape=[20, 20])
    a = relay.nn.dense(data, weight, units=20)
    b = relay.where(data, a, a)
    mod = tvm.IRModule.from_expr(b)
    mod_params = {
        "data": np.random.uniform(-1, 1, size=[1, 20]).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=[20, 20]).astype("float32"),
    }

    output_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=0.01)

    # Create expected module
    data = relay.cast(relay.var("data", shape=[1, 20]), "float16")
    weight = relay.cast(relay.var("weight", shape=[20, 20]), "float16")
    a = relay.nn.dense(data, weight, units=20, out_dtype="float16")
    b = relay.where(data, a, a)
    expected_mod = tvm.IRModule.from_expr(b)
    expected_mod = InferType()(expected_mod)

    assert tvm.ir.structural_equal(expected_mod, output_mod)


def test_batch_matmul_simple():
    """Batch matmul is a special case where we try to accumulate to fp16.

    This is due to the fact heterogenous accumulation dtypes does not work
    on all platforms at the moment.
    """
    data = relay.var("data", shape=[1, 1, 20])
    weight = relay.var("weight", shape=[1, 20, 20])
    a = relay.nn.batch_matmul(data, weight)
    mod = tvm.IRModule.from_expr(a)
    mod_params = {
        "data": np.random.uniform(-1, 1, size=[1, 1, 20]).astype("float32"),
        "weight": np.random.uniform(-1, 1, size=[1, 20, 20]).astype("float32"),
    }
    output_mod = verify_mixed_precision_output_close(mod, mod_params, atol=0.01, rtol=0.01)
    # Create expected module
    data = relay.cast(relay.var("data", shape=[1, 1, 20]), "float16")
    weight = relay.cast(relay.var("weight", shape=[1, 20, 20]), "float16")
    a = relay.nn.batch_matmul(data, weight, out_dtype="float16")
    expected_mod = tvm.IRModule.from_expr(a)
    expected_mod = InferType()(expected_mod)
    assert tvm.ir.structural_equal(expected_mod, output_mod)


if __name__ == "__main__":
    pytest.main([__file__])
