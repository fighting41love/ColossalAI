from colossalai.tensor.shape_consistency import ShapeConsistencyManager
import torch
from torch.fx.node import Node
from colossalai.tensor.sharding_spec import ShardingSpec
from colossalai.device.device_mesh import DeviceMesh
from typing import Union, Dict, List, Optional
import warnings
from functools import reduce
import functools
import operator
from .constants import INFINITY_COST


def generate_sharding_spec(input_: Union[Node, torch.Tensor], device_mesh: DeviceMesh,
                           dim_partition_dict: Dict[int, List[int]]) -> ShardingSpec:
    """
    Generate the sharding spec of the tensor based on the given dim_partition_dict.
    

    Args:
        input_ (Union[Node, torch.Tensor]): the input can be a Node object or a PyTorch tensor. If a node is used, it will look for its meta data associated with this node.
        device_mesh (DeviceMesh): a DeviceMesh object which contains the meta information about the cluster.
        dim_partition_dict (Dict[int, List[int]]): a dictionary to specify the sharding specs, the key is the tensor dimension and the value is the mesh dimension for sharding.
    """

    if isinstance(input_, Node):
        assert hasattr(input_, '_meta_data'), f'The given node has no attribte _meta_data'
        meta_tensor = input_._meta_data
        assert meta_tensor is not None, "The given node's _meta_data attribute is None"
        shape = meta_tensor.shape
    elif isinstance(input_, torch.Tensor):
        shape = input_.shape
    else:
        raise TypeError(
            f'We cannot generate sharding spec for {type(input_)} type, only torch.fx.Node or torch.Tensor is expected.'
        )
    for dim_index, sharding_index_list in dim_partition_dict.items():
        sharding_list = [device_mesh.mesh_shape[sharding_index] for sharding_index in sharding_index_list]
        sharding_size = reduce(operator.mul, sharding_list, 1)
        assert shape[
            dim_index] % sharding_size == 0, f'we cannot shard the {dim_index} dimension of tensor into {sharding_size} partitions.'

    sharding_spec = ShardingSpec(device_mesh=device_mesh, entire_shape=shape, dim_partition_dict=dim_partition_dict)
    return sharding_spec


def generate_resharding_costs(nodes: List[Node],
                              sharding_specs: List[ShardingSpec],
                              count_backward: Optional[bool] = True,
                              dtype: Optional[torch.dtype] = None,
                              index=None):
    '''
    Compute the resharding costs with this specific strategy.

    Argument:
        nodes (List[Node]): a list of nodes
        sharding_spec_for_input(ShardingSpec): a list of ShardingSpec for the nodes.
        count_backward (Optional[bool]): whether to include the cost of resharding in the backward pass, default is True. False can be used for inference.
        dtype (Optional[torch.dtype]): the data type for cost calculation, default is None. 
    '''
    # The resharding_cost of weight is counted due to sharing weight cases.
    resharding_costs = {}
    size_per_elem_bytes = torch.tensor([], dtype=dtype).element_size()

    # shape consistency manager is a singleton class
    shape_consistency_manager = ShapeConsistencyManager()

    for input_node, input_spec in zip(nodes, sharding_specs):
        resharding_costs[input_node] = []
        for strategy in input_node.strategies_vector:
            input_sharding_spec = strategy.output_sharding_spec
            if not isinstance(input_sharding_spec, ShardingSpec):
                assert isinstance(input_sharding_spec, list), 'only ShardingSpec or List[ShardingSpec] is expected.'
                input_sharding_spec = input_sharding_spec[index]
            assert isinstance(input_sharding_spec, ShardingSpec), f'The input node should NOT be a tuple of tensor.'
            try:
                # compute the resharding cost
                _, _, total_resharding_cost = shape_consistency_manager.shape_consistency(
                    input_sharding_spec, input_spec)

                # we need multiply the size of elem dtype to get correct communication cost
                resharding_cost = total_resharding_cost["total"] * size_per_elem_bytes
            except AssertionError as e:
                warnings.warn(f'{e}')
                resharding_cost = INFINITY_COST
            resharding_costs[input_node].append(resharding_cost)
    return resharding_costs


def exception_handler(func):
    """
    A function wrapper which executes the function with a specified seed.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except AssertionError as e:
            warnings.warn(f'{e}')

    return wrapper


def enumerate_all_possible_2d_sharding(mesh_dim_0, mesh_dim_1, dim_size):
    dim_partition_list = []
    # enumerate all the 2D sharding cases
    for i in range(dim_size):
        for j in range(i + 1, dim_size):
            dim_partition_dict_0 = {i: [mesh_dim_0], j: [mesh_dim_1]}
            dim_partition_dict_1 = {i: [mesh_dim_1], j: [mesh_dim_0]}
            dim_partition_list.append(dim_partition_dict_0)
            dim_partition_list.append(dim_partition_dict_1)
    for i in range(dim_size):
        dim_partition_dict_flatten = {i: [mesh_dim_0, mesh_dim_1]}
        dim_partition_list.append(dim_partition_dict_flatten)

    return dim_partition_list


def enumerate_all_possible_1d_sharding(mesh_dim_0, dim_size):
    dim_partition_list = []
    # enumerate all the 1D sharding cases
    for i in range(dim_size):
        dim_partition_dict_0 = {i: [mesh_dim_0]}
        dim_partition_list.append(dim_partition_dict_0)

    return dim_partition_list


def generate_sharding_size(dim_partition_dict, device_mesh):
    total_sharding_size = 1
    for mesh_dim_list in dim_partition_dict.values():
        mesh_dim_sharding_size = [device_mesh.shape[mesh_dim] for mesh_dim in mesh_dim_list]
        sharding_size = reduce(operator.mul, mesh_dim_sharding_size)
        total_sharding_size *= sharding_size

    return total_sharding_size
