from dataclasses import dataclass
from abc import ABC, abstractmethod
from enum import Enum
import operator
import torch
from functools import reduce

from colossalai.device.device_mesh import DeviceMesh
from colossalai.tensor.sharding_spec import ShardingSpec
from colossalai.tensor.shape_consistency import CollectiveCommPattern, CommSpec
from typing import Dict, List, Union, Tuple, Any
from torch.fx.node import Node
from .constants import *

__all__ = ['ShardingStrategy', 'StrategiesVector']


@dataclass
class ShardingStrategy:
    '''
    ShardingStrategy is a structure containing sharding strategies of inputs and output of this node
    and costs information using in solver.

    Argument:
        name(str): express the sharding strategies in string, such as 'S0S1 = S0R x RS1'.
        output_sharding_spec(ShardingSpec): ShardingSpec of the output node.
        compute_cost(float): Computation cost to complete this strategy.(default to 0)
        communication_cost(float): Communication cost to complete this strategy.(default to 0)
        memory_cost(float): Memory cost of the output node using this strategy.(default to 0)
        resharding_costs(Dict[int, List[float]]): resharding_cost[i][j] means the cost of i-th argument in the output node argument list
                                                  with j-th strategy in its strategies_vector transforms to sharding spec wanted in this
                                                  strategy.(default to None)
        input_shardings(List(ShardingSpec)): The ShardingSpecs of the input nodes.
    '''

    name: str
    # TODO: output of fx node,such as torch.var_mean, could be a tuple, so we cannot simply suppose it is a tensor.
    output_sharding_spec: Union[ShardingSpec, Tuple[ShardingSpec]]
    compute_cost: float = 0.
    communication_cost: float = 0.
    memory_cost: float = 0.
    resharding_costs: Dict[Node, List[float]] = None
    # sometimes the input node could be a tuple of nodes, but most of op won't accept tuple of node as input.
    # Therefore, we could process them at the specific op(operator.getitem)
    input_shardings: List[ShardingSpec] = None


class OperationDataType(Enum):
    """
    An operation can come from the argument list of an operator or the parameter list of a module.
    """
    INPUT = 0
    ARG = 1
    PARAM = 2
    OUTPUT = 3


@dataclass
class OperationData:
    """
    OperationData is the data related to an operator, the data can be the operand or the output.

    Args:
        name (str): the name of the operation-related data
        type (OperationDataType): the type of the operation data
        data (torch.Tensor): the value for this data, usually it is a meta tensor.
        logical_shape (Tuple[int]): the logical shape of the data, it can be different from the its actual shape in memory.
    """
    name: str
    type: OperationDataType
    data: torch.Tensor
    logical_shape: Tuple[int] = None

    def __post_init__(self):
        # if no logical shape is specified, use the data shape as the logical shape
        if self.logical_shape is None:
            self.logical_shape = self.data.shape

    def __repr__(self) -> str:
        return f'OperationData(name={self.name}, type={self.type})'

    def __hash__(self) -> int:
        return hash(f'{self.name}-{self.type}')


@dataclass
class TrainCycleItem:
    """
    TrainCycleItem is a dataclass to store the items which have different values for the forward and backward pass
    in a training iteration.

    Args:
        fwd (float): the item for the forward pass
        bwd (float): the item for the backward pass
    """
    fwd: Any
    bwd: Any
    total: Any


@dataclass
class MemoryCost:
    """
    """
    activation: int = 0
    parameter: int = 0


@dataclass
class ShardingStrategy_V2:
    """
    ShardingStrategy is a dataclass to store the meta information on tensor sharding for a node.

    Args:
        name (str): express the sharding strategies in string, such as 'S0S1 = S0R x RS1'.
        output_sharding_spec (ShardingSpec): ShardingSpec of the output node.
        compute_cost (TrainCycleItem): Computation cost to complete this strategy. (default to None)
        communication_cost (TrainCycleItem): Communication cost to complete this strategy. (default to None)
        memory_cost (TrainCycleItem): Memory cost of the output node using this strategy. (default to None)
        input_sharding_specs (List(ShardingSpec)): The ShardingSpecs of the input nodes.
        input_resharding_costs (Dict[int, List[float]]): resharding_cost[i][j] means the cost of i-th argument in the output node argument list
                                                  with j-th strategy in its strategies_vector transforms to sharding spec wanted in this
                                                  strategy.(default to None)
    """
    name: str
    sharding_specs: Dict[OperationData, ShardingSpec] = None
    compute_cost: TrainCycleItem = None
    communication_cost: TrainCycleItem = None
    memory_cost: TrainCycleItem = None
    input_resharding_costs: Dict[OperationData, List[float]] = None
    communication_actions: Dict[OperationData, CommSpec] = None
    resharding_costs: Dict[OperationData, Dict[ShardingSpec, TrainCycleItem]] = None

    @property
    def input_sharding_specs(self) -> Dict[OperationData, ShardingSpec]:
        specs = {}
        specs.update(self._get_sharding_spec(OperationDataType.ARG))
        specs.update(self._get_sharding_spec(OperationDataType.PARAM))
        return specs

    @property
    def argument_sharding_specs(self) -> Dict[OperationData, ShardingSpec]:
        return self._get_sharding_spec(OperationDataType.ARG)

    @property
    def param_sharding_specs(self) -> Dict[OperationData, ShardingSpec]:
        return self._get_sharding_spec(OperationDataType.PARAM)

    @property
    def output_sharding_specs(self) -> Dict[OperationData, ShardingSpec]:
        return self._get_sharding_spec(OperationDataType.OUTPUT)

    def _get_sharding_spec(self, operation_data_type: OperationDataType):
        specs = {k: v for k, v in self.sharding_specs.items() if k.type == operation_data_type}
        return specs

    def get_op_data_by_name(self, name: str):
        for op_data in self.sharding_specs.keys():
            if op_data.name == name:
                return op_data
        raise KeyError(f"Could not find the OperationData with name {name}")

    def get_sharding_spec_by_name(self, name: str):
        for op_data, sharding_spec in self.sharding_specs.items():
            if op_data.name == name:
                return sharding_spec
        raise KeyError(f"Could not find the ShardingSpec for OperationData with name {name}")


class StrategiesVector(list):
    '''
    Each node in fx graph will have a corresponding StrategiesVector, to store all the possible
    strategies of the node.

    Argument:
        node (Node): node for which the list of sharding strategies are generated.
    '''

    def __init__(self, node: Node):
        super().__init__()
        self.node = node
        # fetch its input and output nodes
        # TODO: placeholder input nodes
        self.predecessor_nodes = list(node._input_nodes.keys())
        if self.node.op == 'output':
            self.predecessor_nodes = list(node._input_nodes.keys())[:1]
        self.successor_nodes = list(node.users.keys())

    def check_merge(self):
        merge_label = False
        if self.node.op == 'call_module':
            target = self.node.target
            root_module = self.node.graph.owning_module
            submod = root_module.get_submodule(target)
            submod_type = type(submod)
            # merge elementwise module node into source nodes
            # we could merge element-wise op, because the output sharding spec is always same as the input sharding spec.
            if submod_type in ELEMENTWISE_MODULE_OP:
                merge_label = True

        if self.node.op == 'call_function':
            # we could merge element-wise op, because the output sharding spec is always same as the input sharding spec.
            if self.node.target in ELEMENTWISE_FUNC_OP:
                merge_label = True
            # we could merge bcast op if the rhs is a scalar, because it will fall back to the element-wise case.
            if self.node.target in BCAST_FUNC_OP and len(self.predecessor_nodes) == 1:
                merge_label = True
            # we could merge reshape op, because the output sharding spec of reshape op is always fully replicated.
            if self.node.target in RESHAPE_FUNC_OP:
                merge_label = True

        return merge_label
