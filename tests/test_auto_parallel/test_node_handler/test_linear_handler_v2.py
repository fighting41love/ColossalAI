from colossalai.fx.tracer.meta_patch.patched_module import linear
import torch
import torch.nn as nn
from colossalai.fx import ColoTracer, ColoGraphModule
from colossalai.auto_parallel.solver.op_handler.dot_handler_v2 import LinearModuleHandler, LinearFunctionHandler
from colossalai.auto_parallel.solver.sharding_strategy import OperationData, OperationDataType, StrategiesVector
from colossalai.device.device_mesh import DeviceMesh


def test_linear_module_handler():
    model = nn.Sequential(nn.Linear(16, 32).to('meta'))
    tracer = ColoTracer()
    graph = tracer.trace(model, meta_args={"input": torch.rand(4, 16).to('meta')})
    gm = ColoGraphModule(model, graph)
    physical_mesh_id = torch.arange(0, 4)

    print(graph)
    mesh_shape = (2, 2)
    device_mesh = DeviceMesh(physical_mesh_id, mesh_shape)
    linear_mod_node = list(graph.nodes)[1]
    strategies_vector = StrategiesVector(linear_mod_node)

    # build handler
    handler = LinearModuleHandler(node=linear_mod_node, device_mesh=device_mesh, strategies_vector=strategies_vector)

    # check operation data mapping
    mapping = handler.get_operation_data_mapping()

    for name, op_data in mapping.items():
        op_data: OperationData
        # make sure they have valid values
        assert op_data.logical_shape is not None
        assert op_data.data is not None

    assert mapping['input'].name == "input_1"
    assert mapping['input'].data.is_meta
    assert mapping['input'].data.shape == torch.Size([4, 16])
    assert mapping['input'].type == OperationDataType.ARG
    assert mapping['input'].logical_shape == torch.Size([4, 16])

    assert mapping['other'].name == "weight"
    assert mapping['other'].data.is_meta
    assert mapping['other'].data.shape == torch.Size([32, 16])
    assert mapping['other'].type == OperationDataType.PARAM
    assert mapping['other'].logical_shape == torch.Size([16, 32])

    assert mapping['bias'].name == "bias"
    assert mapping['bias'].data.is_meta
    assert mapping['bias'].data.shape == torch.Size([32])
    assert mapping['bias'].type == OperationDataType.PARAM
    assert mapping['bias'].logical_shape == torch.Size([32])

    assert mapping['output'].name == "_0"
    assert mapping['output'].data.is_meta
    assert mapping['output'].data.shape == torch.Size([4, 32])
    assert mapping['output'].type == OperationDataType.OUTPUT

    strategies_vector = handler.register_strategy()
    strategy_name_list = [val.name for val in strategies_vector]

    # SS = SR x RS
    assert 'S0S1 = S0R x RS1' in strategy_name_list
    assert 'S1S0 = S1R x RS0' in strategy_name_list

    # SR = SS x SR
    assert 'S0R = S0S1 x S1R' in strategy_name_list
    assert 'S1R = S1S0 x S0R' in strategy_name_list

    # RS = RS x SS
    assert 'RS0 = RS1 x S1S0' in strategy_name_list
    assert 'RS1 = RS0 x S0S1' in strategy_name_list

    # RR = RS x SR
    assert 'RR = RS0 x S0R' in strategy_name_list
    assert 'RR = RS1 x S1R' in strategy_name_list

    # RS= RR x RS
    assert 'RS0 = RR x RS0' in strategy_name_list
    assert 'RS1 = RR x RS1' in strategy_name_list


def test_linear_function_handler():
    model = nn.Linear(16, 32).to('meta')
    tracer = ColoTracer()
    graph = tracer.trace(model, meta_args={"input": torch.rand(4, 16).to('meta')})
    gm = ColoGraphModule(model, graph)
    physical_mesh_id = torch.arange(0, 4)

    print(graph)
    mesh_shape = (2, 2)
    device_mesh = DeviceMesh(physical_mesh_id, mesh_shape)
    linear_func_node = list(graph.nodes)[3]
    strategies_vector = StrategiesVector(linear_func_node)

    # build handler
    handler = LinearFunctionHandler(node=linear_func_node, device_mesh=device_mesh, strategies_vector=strategies_vector)

    # # check operation data mapping
    mapping = handler.get_operation_data_mapping()

    assert mapping['input'].name == "input_1"
    assert mapping['input'].data.is_meta
    assert mapping['input'].data.shape == torch.Size([4, 16])
    assert mapping['input'].type == OperationDataType.ARG
    assert mapping['input'].logical_shape == torch.Size([4, 16])

    assert mapping['other'].name == "weight"
    assert mapping['other'].data.is_meta
    assert mapping['other'].data.shape == torch.Size([32, 16])
    assert mapping['other'].type == OperationDataType.PARAM
    assert mapping['other'].logical_shape == torch.Size([16, 32])

    assert mapping['bias'].name == "bias"
    assert mapping['bias'].data.is_meta
    assert mapping['bias'].data.shape == torch.Size([32])
    assert mapping['bias'].type == OperationDataType.PARAM
    assert mapping['other'].logical_shape == torch.Size([16, 32])

    assert mapping['output'].name == "linear"
    assert mapping['output'].data.is_meta
    assert mapping['output'].data.shape == torch.Size([4, 32])
    assert mapping['output'].type == OperationDataType.OUTPUT

    strategies_vector = handler.register_strategy()
    strategy_name_list = [val.name for val in strategies_vector]

    # SS = SR x RS
    assert 'S0S1 = S0R x RS1' in strategy_name_list
    assert 'S1S0 = S1R x RS0' in strategy_name_list

    # SR = SS x SR
    assert 'S0R = S0S1 x S1R' in strategy_name_list
    assert 'S1R = S1S0 x S0R' in strategy_name_list

    # RS = RS x SS
    assert 'RS0 = RS1 x S1S0' in strategy_name_list
    assert 'RS1 = RS0 x S0S1' in strategy_name_list

    # RR = RS x SR
    assert 'RR = RS0 x S0R' in strategy_name_list
    assert 'RR = RS1 x S1R' in strategy_name_list

    # RS= RR x RS
    assert 'RS0 = RR x RS0' in strategy_name_list
    assert 'RS1 = RR x RS1' in strategy_name_list


if __name__ == '__main__':
    test_linear_module_handler()
    test_linear_function_handler()
