import functools
import math
import re
import warnings
from contextlib import contextmanager
from numbers import Number
from typing import Tuple, Callable, Any

import numpy as np

from . import extrapolation as e_
from ._shape import (BATCH_DIM, CHANNEL_DIM, SPATIAL_DIM, COLLECTION_DIM, Shape, EMPTY_SHAPE,
                     spatial, batch, channel, collection, merge_shapes, parse_dim_order, concat_shapes)
from ._tensors import Tensor, wrap, tensor, broadcastable_native_tensors, NativeTensor, TensorStack, CollapsedTensor, \
    custom_op2, compatible_tensor, TensorLike, copy_with, variable_attributes, disassemble_tensors, \
    assemble_tensors, disassemble_tree, assemble_tree, value_attributes
from .backend import default_backend, choose_backend, Backend, get_precision, convert as b_convert, BACKENDS
from .backend._dtype import DType, combine_types


def choose_backend_t(*values, prefer_default=False) -> Backend:
    """ Choose backend for given `Tensor` or native tensor values. """
    natives = sum([v._natives() if isinstance(v, Tensor) else (v,) for v in values], ())
    return choose_backend(*natives, prefer_default=prefer_default)


def convert(x, backend: Backend = None, use_dlpack=True):
    """
    Convert the native representation of a `Tensor` or `TensorLike` to the native format of `backend`.

    *Warning*: This operation breaks the automatic differentiation chain.

    See Also:
        `phi.math.backend.convert()`.

    Args:
        x: `Tensor` to convert. If `x` is a `TensorLike`, its variable attributes are converted.
        backend: Target backend. If `None`, uses the current default backend, see `phi.math.backend.default_backend()`.

    Returns:
        `Tensor` with native representation belonging to `backend`.
    """
    if isinstance(x, Tensor):
        return x._op1(lambda native: b_convert(native, backend, use_dlpack=use_dlpack))
    elif isinstance(x, TensorLike):
        return copy_with(x, **{a: convert(getattr(x, a), backend, use_dlpack=use_dlpack) for a in variable_attributes(x)})
    else:
        return choose_backend(x).as_tensor(x)


def all_available(*values: Tensor) -> bool:
    """
    Tests if the values of all given tensors are known and can be read at this point.
    Tracing placeholders are considered not available, even when they hold example values.

    Tensors are not available during `jit_compile()`, `jit_compile_linear()` or while using TensorFlow's legacy graph mode.
    
    Tensors are typically available when the backend operates in eager mode and is not currently tracing a function.

    This can be used instead of the native checks

    * PyTorch: `torch._C._get_tracing_state()`
    * TensorFlow: `tf.executing_eagerly()`
    * Jax: `isinstance(x, jax.core.Tracer)`

    Args:
      values: Tensors to check.

    Returns:
        `True` if no value is a placeholder or being traced, `False` otherwise.
    """
    from phi.math._functional import is_tracer
    for value in values:
        if is_tracer(value):
            return False
        natives = value._natives()
        natives_available = [choose_backend(native).is_available(native) for native in natives]
        if not all(natives_available):
            return False
    return True


def seed(seed: int):
    """
    Sets the current seed of all backends and the built-in `random` package.

    Calling this function with a fixed value at the start of an application yields reproducible results
    as long as the same backend is used.

    Args:
        seed: Seed to use.
    """
    for backend in BACKENDS:
        backend.seed(seed)
    import random
    random.seed(0)


def native(value: Tensor or Number or tuple or list or Any):
    """
    Returns the native tensor representation of `value`.
    If `value` is a `phi.math.Tensor`, this is equal to calling `phi.math.Tensor.native()`.
    Otherwise, checks that `value` is a valid tensor object and returns it.

    Args:
        value: `Tensor` or native tensor or tensor-like.

    Returns:
        Native tensor representation

    Raises:
        ValueError if the tensor cannot be transposed to match target_shape
    """
    if isinstance(value, Tensor):
        return value.native()
    else:
        choose_backend(value)  # check that value is a native tensor
        return value


def numpy(value: Tensor or Number or tuple or list or Any):
    """
    Converts `value` to a `numpy.ndarray` where value must be a `Tensor`, backend tensor or tensor-like.
    If `value` is a `phi.math.Tensor`, this is equal to calling `phi.math.Tensor.numpy()`.

    *Note*: Using this function breaks the autograd chain. The returned tensor is not differentiable.
    To get a differentiable tensor, use `Tensor.native()` instead.

    Transposes the underlying tensor to match the name order and adds singleton dimensions for new dimension names.
    If a dimension of the tensor is not listed in `order`, a `ValueError` is raised.

    If `value` is a NumPy array, it may be returned directly.

    Returns:
        NumPy representation of `value`

    Raises:
        ValueError if the tensor cannot be transposed to match target_shape
    """
    if isinstance(value, Tensor):
        return value.numpy()
    else:
        backend = choose_backend(value)
        return backend.numpy(value)


def reshaped_native(value: Tensor,
                    groups: tuple or list,
                    force_expand: Any = False,
                    to_numpy=False):
    """
    Returns a native representation of `value` where dimensions are laid out according to `groups`.

    See Also:
        `native()`, `join_dimensions()`, `reshaped_tensor()`.

    Args:
        value: `Tensor`
        groups: Sequence of dimension names as `str` or groups of dimensions to be joined as `Shape`.
        force_expand: `bool` or sequence of dimensions.
            If `True`, repeats the tensor along missing dimensions.
            If `False`, puts singleton dimensions where possible.
            If a sequence of dimensions is provided, only forces the expansion for groups containing those dimensions.
        to_numpy: If True, converts the native tensor to a `numpy.ndarray`.

    Returns:
        Native tensor with dimensions matching `groups`.
    """
    assert isinstance(value, Tensor), f"value must be a Tensor but got {type(value)}"
    order = []
    for i, group in enumerate(groups):
        if isinstance(group, Shape):
            present = value.shape.only(group)
            if force_expand is True or present.volume > 1 or (force_expand is not False and group.only(force_expand).volume > 1):
                value = expand(value, group)
            value = join_dimensions(value, group, batch(f"group{i}"))
            order.append(f"group{i}")
        else:
            assert isinstance(group, str), f"Groups must be either str or Shape but got {group}"
            order.append(group)
    return value.numpy(order) if to_numpy else value.native(order)


def reshaped_tensor(value: Any,
                    groups: tuple or list,
                    check_sizes=False,
                    convert=True):
    """
    Creates a `Tensor` from a native tensor or tensor-like whereby the dimensions of `value` are split according to `groups`.

    See Also:
        `phi.math.tensor()`, `reshaped_native()`, `split_dimension()`.

    Args:
        value: Native tensor or tensor-like.
        groups: Sequence of dimension groups to be joined as `tuple[Shape]` or `list[Shape]`.
        check_sizes: If True, group sizes must match the sizes of `value` exactly. Otherwise, allows singleton dimensions.
        convert: If True, converts the data to the native format of the current default backend.
            If False, wraps the data in a `Tensor` but keeps the given data reference if possible.

    Returns:
        `Tensor` with all dimensions from `groups`
    """
    assert all(isinstance(g, Shape) for g in groups), "groups must be a sequence of Shapes"
    dims = [batch(f'group{i}') for i, group in enumerate(groups)]
    value = tensor(value, *dims, convert=convert)
    for i, group in enumerate(groups):
        if value.shape.get_size(f'group{i}') == group.volume:
            value = split_dimension(value, f'group{i}', group)
        elif check_sizes:
            raise AssertionError(f"Group {group} does not match dimension {i} of value {value.shape}")
        else:
            value = split_dimension(value, f'group{i}', group)
    return value


def copy(value: Tensor):
    """
    Copies the data buffer and encapsulating `Tensor` object.

    Args:
        value: `Tensor` to be copied.

    Returns:
        Copy of `value`.
    """
    if value._is_tracer:
        warnings.warn("Tracing tensors cannot be copied.")
        return value
    return value._op1(lambda native: choose_backend(native).copy(native))


def native_call(f: Callable, *inputs: Tensor, channels_last=None, channel_dim='vector'):
    """
    Calls `f` with the native representations of the `inputs` tensors in standard layout and returns the result as a `Tensor`.

    All inputs are converted to native tensors depending on `channels_last`:

    * `channels_last=True`: Dimension layout `(total_batch_size, spatial_dims..., total_channel_size)`
    * `channels_last=False`: Dimension layout `(total_batch_size, total_channel_size, spatial_dims...)`

    All batch dimensions are compressed into a single dimension with `total_batch_size = input.shape.batch.volume`.
    The same is done for all channel dimensions.

    Additionally, missing batch and spatial dimensions are added so that all `inputs` have the same batch and spatial shape.

    Args:
        f: Function to be called on native tensors of `inputs`.
            The function output must have the same dimension layout as the inputs and the batch size must be identical.
        *inputs: Uniform `Tensor` arguments
        channels_last: (Optional) Whether to put channels as the last dimension of the native representation.
            If `None`, the channels are put in the default position associated with the current backend,
            see `phi.math.backend.Backend.prefers_channels_last()`.
        channel_dim: Name of the channel dimension of the result.

    Returns:
        `Tensor` with batch and spatial dimensions of `inputs` and single channel dimension `channel_dim`.
    """
    if channels_last is None:
        backend = choose_backend_t(*inputs, prefer_default=True)
        channels_last = backend.prefers_channels_last()
    batch = merge_shapes(*[i.shape.batch for i in inputs])
    spatial = merge_shapes(*[i.shape.spatial for i in inputs])
    natives = []
    for i in inputs:
        groups = (batch, *i.shape.spatial.names, i.shape.channel) if channels_last else (batch, i.shape.channel, *i.shape.spatial.names)
        natives.append(reshaped_native(i, groups))
    output = f(*natives)
    if isinstance(output, (tuple, list)):
        raise NotImplementedError()
    else:
        groups = (batch, *spatial.names, channel_dim) if channels_last else (batch, channel_dim, *spatial.names)
        return reshaped_tensor(output, groups)


def print_(obj: Tensor or TensorLike or Number or tuple or list or None = None, name: str = ""):
    """
    Print a tensor with no more than two spatial dimensions, slicing it along all batch and channel dimensions.
    
    Unlike NumPy's array printing, the dimensions are sorted.
    Elements along the alphabetically first dimension is printed to the right, the second dimension upward.
    Typically, this means x right, y up.

    Args:
        obj: tensor-like
        name: name of the tensor

    Returns:

    """
    def variables(obj) -> dict:
        if hasattr(obj, '__variable_attrs__') or hasattr(obj, '__value_attrs__'):
            return {f".{a}": getattr(obj, a) for a in variable_attributes(obj)}
        elif isinstance(obj, (tuple, list)):
            return {f"[{i}]": item for i, item in enumerate(obj)}
        elif isinstance(obj, dict):
            return obj
        else:
            raise ValueError(f"Not TensorLike: {type(obj)}")

    if obj is None:
        print()
    elif isinstance(obj, Tensor):
        _print_tensor(obj, name)
    elif isinstance(obj, TensorLike):
        for n, val in variables(obj).items():
            print_(val, name + n)
    else:
        value = wrap(obj)
        _print_tensor(value, name)


def _print_tensor(value: Tensor, name: str or None):
    if name:
        print(" " * 16 + name)
    dim_order = tuple(sorted(value.shape.spatial.names, reverse=True))
    if value.shape.spatial_rank == 0:
        print(value.numpy())
    elif value.shape.spatial_rank == 1:
        for index_dict in value.shape.non_spatial.meshgrid():
            if value.shape.non_spatial.volume > 1:
                print(f"--- {', '.join('%s=%d' % (name, idx) for name, idx in index_dict.items())} ---")
            text = np.array2string(value[index_dict].numpy(dim_order), precision=2, separator=', ', max_line_width=np.inf)
            print(' ' + re.sub('[\\[\\]]', '', text))
    elif value.shape.spatial_rank == 2:
        for index_dict in value.shape.non_spatial.meshgrid():
            if value.shape.non_spatial.volume > 1:
                print(f"--- {', '.join('%s=%d' % (name, idx) for name, idx in index_dict.items())} ---")
            text = np.array2string(value[index_dict].numpy(dim_order)[::-1], precision=2, separator=', ', max_line_width=np.inf)
            print(' ' + re.sub('[\\[\\]]', '', re.sub('\\],', '', text)))
    else:
        raise NotImplementedError('Can only print tensors with up to 2 spatial dimensions.')


def map_(function, *values: Tensor) -> Tensor:
    """
    Calls `function` on all elements of `value`.

    Args:
        function: Function to be called on single elements contained in `value`. Must return a value that can be stored in tensors.
        values: Tensors to iterate over. Number of tensors must match `function` signature.

    Returns:
        `Tensor` of same shape as `value`.
    """
    shape = merge_shapes(*[v.shape for v in values])
    values_reshaped = [CollapsedTensor(v, shape) for v in values]
    flat = [flatten(v) for v in values_reshaped]
    result = []
    for items in zip(*flat):
        result.append(function(*items))
    if None in result:
        assert all(r is None for r in result), f"map function returned None for some elements, {result}"
        return
    return wrap(result).vector.split(shape)


def _initialize(uniform_initializer, shapes: tuple, dtype=None):
    shape = concat_shapes(*shapes)
    if shape.is_non_uniform:
        stack_dim = shape.shape.without('dims')[0:1]
        shapes = shape.unstack(stack_dim.name)
        tensors = [_initialize(uniform_initializer, s, dtype) for s in shapes]
        return stack(tensors, stack_dim)
    else:
        return uniform_initializer(shape, dtype)


def zeros(*shape: Shape, dtype: DType = None):
    """
    Define a tensor with specified shape with value 0 / False everywhere.
    
    This method may not immediately allocate the memory to store the values.

    Args:
      shape: base tensor shape (Default value = EMPTY_SHAPE)
      dtype: data type (Default value = None)

    Returns:
      tensor of specified shape

    """
    return _initialize(lambda shape, dtype: CollapsedTensor(NativeTensor(default_backend().zeros((), dtype=dtype), EMPTY_SHAPE), shape), shape, dtype)


def zeros_like(obj):
    nest, values = disassemble_tree(obj)
    values0 = [zeros(t.shape, dtype=t.dtype) for t in values]
    return assemble_tree(nest, values0)


def ones(*shape: Shape, dtype: DType = None):
    """
    Define a tensor with specified shape with value 1 / True everywhere.
    
    This method may not immediately allocate the memory to store the values.

    Args:
      shape: base tensor shape (Default value = EMPTY_SHAPE)
      dtype: data type (Default value = None)

    Returns:
      tensor of specified shape

    """
    return _initialize(lambda shape, dtype: CollapsedTensor(NativeTensor(default_backend().ones((), dtype=dtype), EMPTY_SHAPE), shape), shape, dtype)


def ones_like(tensor: Tensor):
    return zeros(tensor.shape, dtype=tensor.dtype) + 1


def random_normal(*shape: Shape, dtype: DType = None):
    """
    Creates a `Tensor` with the specified shape, filled with random values distributed according to a normal / Gaussian distribution.

    Implementations:

    * NumPy: [`numpy.random.standard_normal`](https://numpy.org/doc/stable/reference/random/generated/numpy.random.standard_normal.html)
    * PyTorch: [`torch.randn`](https://pytorch.org/docs/stable/generated/torch.randn.html)
    * TensorFlow: [`tf.random.normal`](https://www.tensorflow.org/api_docs/python/tf/random/normal)
    * Jax: [`jax.random.normal`](https://jax.readthedocs.io/en/latest/_autosummary/jax.random.normal.html)

    Args:
        shape: (optional) Base `Shape`
        dtype: (optional) `DType`. If `None`, a float tensor with the current default precision is created, see `get_precision()`.

    Returns:
        `Tensor`
    """

    def uniform_random_normal(shape, dtype):
        native = choose_backend(*shape.sizes, prefer_default=True).random_normal(shape.sizes)
        native = native if dtype is None else native.astype(dtype)
        return NativeTensor(native, shape)

    return _initialize(uniform_random_normal, shape, dtype)


def random_uniform(*shape: Shape, dtype: DType = None):

    def uniform_random_uniform(shape, dtype):
        native = choose_backend(*shape.sizes, prefer_default=True).random_uniform(shape.sizes)
        native = native if dtype is None else native.astype(dtype)
        return NativeTensor(native, shape)

    return _initialize(uniform_random_uniform, shape, dtype)


def transpose(x, axes):
    """
    Swap the dimension order of `x`.
    This is done implicitly if `x` is a `Tensor`.

    Implementations:

    * NumPy: [`numpy.transpose`](https://numpy.org/doc/stable/reference/generated/numpy.transpose.html)
    * PyTorch: [`x.permute`](https://pytorch.org/docs/stable/tensors.html#torch.Tensor.permute)
    * TensorFlow: [`tf.transpose`](https://www.tensorflow.org/api_docs/python/tf/transpose)
    * Jax: [`jax.numpy.transpose`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.transpose.html)

    Args:
        x: `Tensor` or native tensor.
        axes: `tuple` or `list`

    Returns:
        `Tensor` or native tensor, depending on `x`.
    """
    if isinstance(x, Tensor):
        return CollapsedTensor(x, x.shape[axes])  # TODO avoid nesting
    else:
        return choose_backend(x).transpose(x, axes)


def fftfreq(resolution: Shape, dx: Tensor or float = 1, dtype: DType = None):
    """
    Returns the discrete Fourier transform sample frequencies.
    These are the frequencies corresponding to the components of the result of `math.fft` on a tensor of shape `resolution`.

    Args:
        resolution: Grid resolution measured in cells
        dx: Distance between sampling points in real space.
        dtype: Data type of the returned tensor (Default value = None)

    Returns:
        `Tensor` holding the frequencies of the corresponding values computed by math.fft
    """
    k = meshgrid(**{dim: np.fft.fftfreq(int(n)) for dim, n in resolution.spatial.named_sizes})
    k /= dx
    return to_float(k) if dtype is None else cast(k, dtype)


def meshgrid(dim_type=spatial, stack_dim=channel('vector'), **dimensions: int or Tensor) -> Tensor:
    """
    Generate a mesh-grid `Tensor` from keyword dimensions.

    Args:
        **dimensions: Mesh-grid dimensions, mapping names to values.
            Values may be `int`, 1D `Tensor` or 1D native tensor.
        dim_type: Dimension type of mesh-grid dimensions, one of `spatial`, `channel`, `batch`, `collection`.
        stack_dim: Vector dimension along which grids are stacked.

    Returns:
        Mesh-grid `Tensor`
    """
    assert 'vector' not in dimensions
    dim_values = []
    dim_sizes = []
    for dim, spec in dimensions.items():
        if isinstance(spec, int):
            dim_values.append(tuple(range(spec)))
            dim_sizes.append(spec)
        elif isinstance(spec, Tensor):
            assert spec.rank == 1, f"Only 1D sequences allowed, got {spec} for dimension '{dim}'."
            dim_values.append(spec.native())
            dim_sizes.append(spec.shape.volume)
        else:
            backend = choose_backend(spec)
            shape = backend.staticshape(spec)
            assert len(shape) == 1, "Only 1D sequences allowed, got {spec} for dimension '{dim}'."
            dim_values.append(spec)
            dim_sizes.append(shape[0])
    backend = choose_backend(*dim_values, prefer_default=True)
    indices_list = backend.meshgrid(*dim_values)
    grid_shape = dim_type(**{dim: size for dim, size in zip(dimensions.keys(), dim_sizes)})
    channels = [NativeTensor(t, grid_shape) for t in indices_list]
    return stack(channels, stack_dim)


def linspace(start, stop, number: int, dim: Shape = channel('linspace')):
    assert dim.rank == 1
    native = choose_backend(start, stop, number, prefer_default=True).linspace(start, stop, number)
    return NativeTensor(native, dim.with_sizes([number]))


def arange(dim: Shape, start_or_stop: int, stop: int or None = None, step=1):
    if stop is None:
        start, stop = 0, start_or_stop
    else:
        start = start_or_stop
    native = choose_backend(start, stop, prefer_default=True).range(start, stop, step, DType(int, 32))
    return NativeTensor(native, dim.with_sizes([stop - start]))


def range_tensor(shape: Shape):
    data = arange(spatial('range'), 0, shape.volume)
    result = split_dimension(data, 'range', shape)
    return result


def stack(values: tuple or list, dim: Shape):
    """
    Lazy stack.
    Stacks `values` along the new dimension `dim`.

    Args:
        values: Sequence of `Tensor` objects to be stacked.
        dim: Single-dimension `Shape`. This dimension must not be present with any of the `values`.

    Returns:
        `Tensor` containing `values` stacked along `dim`.
    """
    values = cast_same(*values)

    def inner_stack(*values):
        return TensorStack(values, dim)

    result = broadcast_op(inner_stack, values)
    return result


def concat(values: tuple or list, dim: Shape) -> Tensor:
    """
    Concatenates a sequence of tensors along one dimension.
    The shapes of all values must be equal, except for the size of the concat dimension.

    Args:
      values: Tensors to concatenate
      dim: concat dimension, must be present in all values
      values: tuple or list: 
      dim: str: 

    Returns:
      concatenated tensor

    """
    assert len(values) > 0, "concat() got empty sequence"
    assert isinstance(dim, Shape) and dim.rank == 1, f"dim must be a single-dimension Shape but got '{dim}' of type {type(dim)}"
    broadcast_shape = values[0].shape
    natives = [v.native(order=broadcast_shape.names) for v in values]
    backend = choose_backend(*natives)
    concatenated = backend.concat(natives, broadcast_shape.index(dim))
    return NativeTensor(concatenated, broadcast_shape.with_sizes(backend.staticshape(concatenated)))


def pad(value: Tensor, widths: dict, mode: 'e_.Extrapolation') -> Tensor:
    """
    Pads a tensor along the specified dimensions, determining the added values using the given extrapolation.
    Unlike `Extrapolation.pad()`, this function can handle negative widths which slice off outer values.

    Args:
        value: `Tensor` to be padded
        widths: `dict` mapping dimension name (`str`) to `(lower, upper)`
            where `lower` and `upper` are `int` that can be positive (pad), negative (slice) or zero (pass).
        mode: `Extrapolation` used to determine values added from positive `widths`.

    Returns:
        Padded `Tensor`
    """
    has_negative_widths = any(w[0] < 0 or w[1] < 0 for w in widths.values())
    slices = None
    if has_negative_widths:
        slices = {dim: slice(max(0, -w[0]), min(0, w[1]) or None) for dim, w in widths.items()}
        widths = {dim: (max(0, w[0]), max(0, w[1])) for dim, w in widths.items()}
    result = mode.pad(value, widths)
    return result[slices] if has_negative_widths else result


def closest_grid_values(grid: Tensor,
                        coordinates: Tensor,
                        extrap: 'e_.Extrapolation',
                        stack_dim_prefix='closest_'):
    """
    Finds the neighboring grid points in all spatial directions and returns their values.
    The result will have 2^d values for each vector in coordiantes in d dimensions.

    Args:
      grid: grid data. The grid is spanned by the spatial dimensions of the tensor
      coordinates: tensor with 1 channel dimension holding vectors pointing to locations in grid index space
      extrap: grid extrapolation
      stack_dim_prefix: For each spatial dimension `dim`, stacks lower and upper closest values along dimension `stack_dim_prefix+dim`.

    Returns:
      Tensor of shape (batch, coord_spatial, grid_spatial=(2, 2,...), grid_channel)

    """
    return broadcast_op(functools.partial(_closest_grid_values, extrap=extrap, stack_dim_prefix=stack_dim_prefix), [grid, coordinates])


def _closest_grid_values(grid: Tensor,
                         coordinates: Tensor,
                         extrap: 'e_.Extrapolation',
                         stack_dim_prefix='closest_'):
    # alternative method: pad array for all 2^d combinations, then stack to simplify gather.
    # --- Pad tensor where transform is not possible ---
    non_copy_pad = {dim: (0 if extrap[dim, 0].is_copy_pad else 1, 0 if extrap[dim, 1].is_copy_pad else 1)
                    for dim in grid.shape.spatial.names}
    grid = extrap.pad(grid, non_copy_pad)
    coordinates += wrap([not extrap[dim, 0].is_copy_pad for dim in grid.shape.spatial.names], channel('vector'))
    # --- Transform coordiantes ---
    min_coords = to_int32(floor(coordinates))
    max_coords = extrap.transform_coordinates(min_coords + 1, grid.shape)
    min_coords = extrap.transform_coordinates(min_coords, grid.shape)

    def left_right(is_hi_by_axis_left, ax_idx):
        is_hi_by_axis_right = is_hi_by_axis_left | np.array([ax == ax_idx for ax in range(grid.shape.spatial_rank)])
        coords_left = where(is_hi_by_axis_left, max_coords, min_coords)
        coords_right = where(is_hi_by_axis_right, max_coords, min_coords)
        if ax_idx == grid.shape.spatial_rank - 1:
            values_left = gather(grid, coords_left)
            values_right = gather(grid, coords_right)
        else:
            values_left = left_right(is_hi_by_axis_left, ax_idx + 1)
            values_right = left_right(is_hi_by_axis_right, ax_idx + 1)
        return stack([values_left, values_right], channel(f"{stack_dim_prefix}{grid.shape.spatial.names[ax_idx]}"))

    result = left_right(np.array([False] * grid.shape.spatial_rank), 0)
    return result


def grid_sample(grid: Tensor, coordinates: Tensor, extrap: 'e_.Extrapolation'):
    result = broadcast_op(functools.partial(_grid_sample, extrap=extrap), [grid, coordinates])
    return result


def _grid_sample(grid: Tensor, coordinates: Tensor, extrap: 'e_.Extrapolation' or None):
    if grid.shape.batch == coordinates.shape.batch or grid.shape.batch.volume == 1 or coordinates.shape.batch.volume == 1:
        # call backend.grid_sample()
        batch = grid.shape.batch & coordinates.shape.batch
        backend = choose_backend_t(grid, coordinates)
        result = NotImplemented
        if extrap is None:
            result = backend.grid_sample(reshaped_native(grid, [batch, *grid.shape.spatial, grid.shape.channel]),
                                         grid.shape.indices(grid.shape.spatial),
                                         reshaped_native(coordinates, [batch, *coordinates.shape.collection, *coordinates.shape.spatial, 'vector']),
                                         'undefined')
        elif extrap.native_grid_sample_mode:
            result = backend.grid_sample(reshaped_native(grid, [batch, *grid.shape.spatial, grid.shape.channel]),
                                         grid.shape.indices(grid.shape.spatial),
                                         reshaped_native(coordinates, [batch, *coordinates.shape.collection, *coordinates.shape.spatial, 'vector']),
                                         extrap.native_grid_sample_mode)
        if result is NotImplemented:
            # pad one layer
            grid_padded = pad(grid, {dim: (1, 1) for dim in grid.shape.spatial.names}, extrap or e_.ZERO)
            if extrap is not None:
                from .extrapolation import _CopyExtrapolation
                if isinstance(extrap, _CopyExtrapolation):
                    inner_coordinates = extrap.transform_coordinates(coordinates, grid.shape) + 1
                else:
                    inner_coordinates = extrap.transform_coordinates(coordinates + 1, grid_padded.shape)
            else:
                inner_coordinates = coordinates + 1
            result = backend.grid_sample(reshaped_native(grid_padded, [batch, *grid_padded.shape.spatial.names, grid.shape.channel]),
                                         grid.shape.indices(grid.shape.spatial),
                                         reshaped_native(inner_coordinates, [batch, *coordinates.shape.collection, *coordinates.shape.spatial, 'vector']),
                                         'boundary')
        if result is not NotImplemented:
            result = reshaped_tensor(result, [grid.shape.batch & coordinates.shape.batch, *coordinates.shape.collection, *coordinates.shape.spatial, grid.shape.channel])
            return result
    # fallback to slower grid sampling
    neighbors = _closest_grid_values(grid, coordinates, extrap or e_.ZERO, 'closest_')
    binary = meshgrid(**{f'closest_{dim}': (0, 1) for dim in grid.shape.spatial.names}, dim_type=channel)
    right_weights = coordinates % 1
    binary, right_weights = join_spaces(binary, right_weights)
    weights = prod(binary * right_weights + (1 - binary) * (1 - right_weights), 'vector')
    result = sum_(neighbors * weights, dim=[f"closest_{dim}" for dim in grid.shape.spatial.names])
    return result


def join_spaces(*tensors):
    spatial_dims = merge_shapes(*[t.shape.spatial for t in tensors])
    return [CollapsedTensor(t, t.shape.non_spatial & spatial_dims) for t in tensors]


def broadcast_op(operation: Callable,
                 tensors: tuple or list,
                 iter_dims: set or tuple or list or Shape = None,
                 no_return=False):
    if iter_dims is None:
        iter_dims = set()
        for tensor in tensors:
            if isinstance(tensor, TensorStack) and tensor.requires_broadcast:
                iter_dims.add(tensor.stack_dim.name)
    if len(iter_dims) == 0:
        return operation(*tensors)
    else:
        if isinstance(iter_dims, Shape):
            iter_dims = iter_dims.names
        dim = next(iter(iter_dims))
        dim_type = None
        size = None
        unstacked = []
        for tensor in tensors:
            if dim in tensor.shape.names:
                unstacked_tensor = tensor.unstack(dim)
                unstacked.append(unstacked_tensor)
                if size is None:
                    size = len(unstacked_tensor)
                    dim_type = tensor.shape.get_type(dim)
                else:
                    assert size == len(unstacked_tensor)
                    assert dim_type == tensor.shape.get_type(dim)
            else:
                unstacked.append(tensor)
        result_unstacked = []
        for i in range(size):
            gathered = [t[i] if isinstance(t, tuple) else t for t in unstacked]
            result_unstacked.append(broadcast_op(operation, gathered, iter_dims=set(iter_dims) - {dim}))
        if not no_return:
            return TensorStack(result_unstacked, Shape([None], [dim], [dim_type]))


def split_dimension(value: Tensor, dim: str, split_dims: Shape):
    """
    Decompresses a tensor dimension by unstacking the elements along it.
    This function replaces the traditional `reshape` for these cases.
    The compressed dimension `dim` is assumed to contain elements laid out according to the order or `split_dims`.

    See Also:
        `join_dimensions()`

    Args:
        value: `Tensor` for which one dimension should be split.
        dim: Compressed dimension to be decompressed.
        split_dims: Ordered new dimensions to replace `dim` as `Shape`.

    Returns:
        `Tensor` with decompressed shape
    """
    if split_dims.rank == 0:
        return value.dimension(dim)[0]  # remove dim
    if split_dims.rank == 1:
        new_shape = value.shape.without(dim).expand(split_dims, pos=value.shape.index(dim))
        return value._with_shape_replaced(new_shape)
    else:
        native = value.native(value.shape.names)
        new_shape = value.shape.without(dim)
        i = value.shape.index(dim)
        for d in split_dims:
            new_shape = new_shape.expand(d, pos=i)
            i += 1
        native_reshaped = choose_backend(native).reshape(native, new_shape.sizes)
        return NativeTensor(native_reshaped, new_shape)


def join_dimensions(value: Tensor,
                    dims: Shape or tuple or list,
                    joined: Shape,
                    pos: int or None = None):
    """
    Compresses multiple dimensions into a single dimension by concatenating the elements.
    Elements along the new dimensions are laid out according to the order of `dims`.
    If the order of `dims` differs from the current dimension order, the tensor is transposed accordingly.
    This function replaces the traditional `reshape` for these cases.

    The type of the new dimension will be equal to the types of `dims`.
    If `dims` have varying types, the new dimension will be a batch dimension.

    See Also:
        `split_dimension()`

    Args:
        value: Tensor containing the dimensions `dims`.
        dims: Dimensions to be compressed in the specified order.
        joined: Name and type of the new dimension.
        pos: Index of new dimension. `None` for automatic, `-1` for last, `0` for first.

    Returns:
        `Tensor` with compressed shape.
    """
    dims = dims.names if isinstance(dims, Shape) else dims
    if len(dims) == 0 or all(dim not in value.shape for dim in dims):
        return CollapsedTensor(value, value.shape.expand(joined.with_sizes([1]), pos))
    if len(dims) == 1:
        new_shape = value.shape.with_names([joined.name if name == dims[0] else name for name in value.shape.names])
        return value._with_shape_replaced(new_shape)
    order = value.shape.order_group(dims)
    native = value.native(order)
    if pos is None:
        pos = min(value.shape.indices(dims))
    new_shape = value.shape.without(dims).expand(joined.with_sizes([value.shape.only(dims).volume]), pos)
    native = choose_backend(native).reshape(native, new_shape.sizes)
    return NativeTensor(native, new_shape)


def flatten(value: Tensor, flat_dim: Shape = collection('flat')):
    assert isinstance(flat_dim, Shape) and flat_dim.rank == 1, flat_dim
    return join_dimensions(value, value.shape, flat_dim)


def where(condition: Tensor or float or int, value_true: Tensor or float or int, value_false: Tensor or float or int):
    """
    Builds a tensor by choosing either values from `value_true` or `value_false` depending on `condition`.
    If `condition` is not of type boolean, non-zero values are interpreted as True.
    
    This function requires non-None values for `value_true` and `value_false`.
    To get the indices of True / non-zero values, use :func:`nonzero`.

    Args:
      condition: determines where to choose values from value_true or from value_false
      value_true: values to pick where condition != 0 / True
      value_false: values to pick where condition == 0 / False
      condition: Tensor or float or int: 
      value_true: Tensor or float or int: 
      value_false: Tensor or float or int: 

    Returns:
      tensor containing dimensions of all inputs

    """
    condition = tensor(condition)
    value_true = tensor(value_true)
    value_false = tensor(value_false)
    shape, (c, vt, vf) = broadcastable_native_tensors(condition, value_true, value_false)
    result = choose_backend(c, vt, vf).where(c, vt, vf)
    return NativeTensor(result, shape)


def nonzero(value: Tensor, list_dim: Shape = collection('nonzero'), index_dim: Shape = channel('vector')):
    """
    Get spatial indices of non-zero / True values.
    
    Batch dimensions are preserved by this operation.
    If channel dimensions are present, this method returns the indices where any component is nonzero.

    Implementations:

    * NumPy: [`numpy.argwhere`](https://numpy.org/doc/stable/reference/generated/numpy.argwhere.html)
    * PyTorch: [`torch.nonzero`](https://pytorch.org/docs/stable/generated/torch.nonzero.html)
    * TensorFlow: [`tf.where(tf.not_equal(values, 0))`](https://www.tensorflow.org/api_docs/python/tf/where)
    * Jax: [`jax.numpy.nonzero`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.nonzero.html)

    Args:
        value: spatial tensor to find non-zero / True values in.
        list_dim: Dimension listing non-zero values.
        index_dim: Index dimension.

    Returns:
        `Tensor` of shape (batch dims..., `list_dim`=#non-zero, `index_dim`=value.shape.spatial_rank)

    """
    if value.shape.channel_rank > 0:
        value = sum_(abs(value), value.shape.channel)

    def unbatched_nonzero(value: Tensor):
        native = reshaped_native(value, [*value.shape.spatial])
        backend = choose_backend(native)
        indices = backend.nonzero(native)
        indices_shape = Shape(backend.staticshape(indices), (list_dim.name, index_dim.name), (list_dim.type, index_dim.type))
        return NativeTensor(indices, indices_shape)

    return broadcast_op(unbatched_nonzero, [value], iter_dims=value.shape.batch.names)


def _reduce(value: Tensor or list or tuple,
            dim: str or tuple or list or Shape or None,
            native_function: Callable,
            collapsed_function: Callable = lambda inner_reduced, collapsed_dims_to_reduce: inner_reduced,
            unaffected_function: Callable = lambda value: value) -> Tensor:
    """

    Args:
        value:
        dim:
        native_function:
        collapsed_function: handles collapsed dimensions, called as `collapsed_function(inner_reduced, collapsed_dims_to_reduce)`
        unaffected_function: returns `unaffected_function(value)` if `len(dims) > 0` but none of them are part of `value`

    Returns:

    """
    if dim in ((), [], EMPTY_SHAPE):
        return value
    if isinstance(value, (tuple, list)):
        values = [wrap(v) for v in value]
        value = stack(values, batch('_reduce'))
        if dim is None:
            pass  # continue below
        elif dim == 0:
            dim = '_reduce'
        else:
            raise ValueError('dim must be 0 or None when passing a sequence of tensors')
    else:
        value = wrap(value)
    dims = _resolve_dims(dim, value.shape)
    return value._tensor_reduce(dims, native_function, collapsed_function, unaffected_function)


def _resolve_dims(dim: str or tuple or list or Shape or None,
                  t_shape: Shape) -> Tuple[str]:
    if dim is None:
        return t_shape.names
    return parse_dim_order(dim)


def sum_(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    """
    Sums the tensor values along the specified dimensions.

    Args:
        value: `Tensor` or `list` / `tuple` of Tensors.
        dim: Dimension or dimensions to be reduced. One of

            * `None` to reduce all dimensions
            * `str` containing single dimension or comma-separated list of dimensions
            * `Tuple[str]` or `List[str]`
            * `Shape`
            * `0` when `isinstance(value, (tuple, list))` to add up the sequence of Tensors

    Returns:
        `Tensor` without the reduced dimensions.
    """
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.sum(native, dim),
                   collapsed_function=lambda inner, red_shape: inner * red_shape.volume)


def prod(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.prod(native, dim),
                   collapsed_function=lambda inner, red_shape: inner ** red_shape.volume)


def mean(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.mean(native, dim))


def std(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.std(native, dim),
                   collapsed_function=lambda inner, red_shape: inner,
                   unaffected_function=lambda value: value * 0)


def any_(boolean_tensor: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(boolean_tensor, dim,
                   native_function=lambda backend, native, dim: backend.any(native, dim))


def all_(boolean_tensor: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(boolean_tensor, dim,
                   native_function=lambda backend, native, dim: backend.all(native, dim))


def max_(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.max(native, dim))


def min_(value: Tensor or list or tuple,
         dim: str or int or tuple or list or None or Shape = None) -> Tensor:
    return _reduce(value, dim,
                   native_function=lambda backend, native, dim: backend.min(native, dim))


def dot(x: Tensor,
        x_dims: str or tuple or list or Shape,
        y: Tensor,
        y_dims: str or tuple or list or Shape) -> Tensor:
    """
    Computes the dot product along the specified dimensions.
    Contracts `x_dims` with `y_dims` by first multiplying the elements and then summing them up.

    For one dimension, this is equal to matrix-matrix or matrix-vector multiplication.

    The function replaces the traditional `dot` / `tensordot` / `matmul` / `einsum` functions.

    * NumPy: [`numpy.tensordot`](https://numpy.org/doc/stable/reference/generated/numpy.tensordot.html), [`numpy.einsum`](https://numpy.org/doc/stable/reference/generated/numpy.einsum.html)
    * PyTorch: [`torch.tensordot`](https://pytorch.org/docs/stable/generated/torch.tensordot.html#torch.tensordot), [`torch.einsum`](https://pytorch.org/docs/stable/generated/torch.einsum.html)
    * TensorFlow: [`tf.tensordot`](https://www.tensorflow.org/api_docs/python/tf/tensordot), [`tf.einsum`](https://www.tensorflow.org/api_docs/python/tf/einsum)
    * Jax: [`jax.numpy.tensordot`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.tensordot.html), [`jax.numpy.einsum`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.einsum.html)

    Args:
        x: First `Tensor`
        x_dims: Dimensions of `x` to reduce against `y`
        y: Second `Tensor`
        y_dims: Dimensions of `y` to reduce against `x`.

    Returns:
        Dot product as `Tensor`.
    """
    x_dims = _resolve_dims(x_dims, x.shape)
    y_dims = _resolve_dims(y_dims, y.shape)
    x_native = x.native(x.shape)
    y_native = y.native(y.shape)
    backend = choose_backend(x_native, y_native)
    remaining_shape_x = x.shape.without(x_dims)
    remaining_shape_y = y.shape.without(y_dims)
    if remaining_shape_y.only(remaining_shape_x).is_empty:  # no shared batch dimensions -> tensordot
        result_native = backend.tensordot(x_native, x.shape.indices(x_dims), y_native, y.shape.indices(y_dims))
    else:  # shared batch dimensions -> einsum
        REDUCE_LETTERS = list('ijklmn')
        KEEP_LETTERS = list('abcdefgh')
        x_letters = [(REDUCE_LETTERS if dim in x_dims else KEEP_LETTERS).pop(0) for dim in x.shape.names]
        x_letter_map = {dim: letter for dim, letter in zip(x.shape.names, x_letters)}
        REDUCE_LETTERS = list('ijklmn')
        y_letters = []
        for dim in y.shape.names:
            if dim in y_dims:
                y_letters.append(REDUCE_LETTERS.pop(0))
            else:
                if dim in x_letter_map:
                    y_letters.append(x_letter_map[dim])
                else:
                    y_letters.append(KEEP_LETTERS.pop(0))
        keep_letters = list('abcdefgh')[:-len(KEEP_LETTERS)]
        subscripts = f'{"".join(x_letters)},{"".join(y_letters)}->{"".join(keep_letters)}'
        result_native = backend.einsum(subscripts, x_native, y_native)
    result_shape = merge_shapes(x.shape.without(x_dims), y.shape.without(y_dims))  # don't check group match
    return NativeTensor(result_native, result_shape)


def _backend_op1(x, unbound_method) -> Tensor:
    if isinstance(x, Tensor):
        return x._op1(lambda native: getattr(choose_backend(native), unbound_method.__name__)(native))
    elif isinstance(x, TensorLike):
        return copy_with(x, **{a: _backend_op1(getattr(x, a), unbound_method) for a in value_attributes(x)})
    else:
        backend = choose_backend(x)
        y = getattr(backend, unbound_method.__name__)(x)
        return wrap(y)


def abs_(x) -> Tensor:
    """
    Computes *||x||<sub>1</sub>*.
    Complex `x` result in matching precision float values.

    *Note*: The gradient of this operation is undefined for *x=0*.
    TensorFlow and PyTorch return 0 while Jax returns 1.

    Args:
        x: `Tensor` or `TensorLike`

    Returns:
        Absolute value of `x` of same type as `x`.
    """
    return _backend_op1(x, Backend.abs)


def sign(x) -> Tensor:
    return _backend_op1(x, Backend.sign)


def round_(x) -> Tensor:
    return _backend_op1(x, Backend.round)


def ceil(x) -> Tensor:
    return _backend_op1(x, Backend.ceil)


def floor(x) -> Tensor:
    return _backend_op1(x, Backend.floor)


def sqrt(x) -> Tensor:
    return _backend_op1(x, Backend.sqrt)


def exp(x) -> Tensor:
    return _backend_op1(x, Backend.exp)


def to_float(x) -> Tensor:
    """
    Converts the given tensor to floating point format with the currently specified precision.
    
    The precision can be set globally using `math.set_global_precision()` and locally using `with math.precision()`.
    
    See the `phi.math` module documentation at https://tum-pbs.github.io/PhiFlow/Math.html

    See Also:
        `cast()`.

    Args:
        x: values to convert

    Returns:
        `Tensor` of same shape as `x`
    """
    return _backend_op1(x, Backend.to_float)


def to_int32(x) -> Tensor:
    return _backend_op1(x, Backend.to_int32)


def to_int64(x) -> Tensor:
    return _backend_op1(x, Backend.to_int64)


def to_complex(x) -> Tensor:
    """
    Converts the given tensor to complex floating point format with the currently specified precision.

    The precision can be set globally using `math.set_global_precision()` and locally using `with math.precision()`.

    See the `phi.math` module documentation at https://tum-pbs.github.io/PhiFlow/Math.html

    See Also:
        `cast()`.

    Args:
        x: values to convert

    Returns:
        `Tensor` of same shape as `x`
    """
    return _backend_op1(x, Backend.to_complex)


def isfinite(x) -> Tensor:
    return _backend_op1(x, Backend.isfinite)


def imag(complex) -> Tensor:
    return _backend_op1(complex, Backend.imag)


def real(complex) -> Tensor:
    return _backend_op1(complex, Backend.real)


def sin(x) -> Tensor:
    return _backend_op1(x, Backend.sin)


def cos(x) -> Tensor:
    return _backend_op1(x, Backend.cos)


def tan(x) -> Tensor:
    return _backend_op1(x, Backend.tan)


def log(x) -> Tensor:
    """ Natural logarithm. """
    return _backend_op1(x, Backend.log)


def log2(x) -> Tensor:
    return _backend_op1(x, Backend.log2)


def log10(x) -> Tensor:
    return _backend_op1(x, Backend.log10)


def cast(x: Tensor, dtype: DType) -> Tensor:
    """
    Casts `x` to a different data type.

    Implementations:

    * NumPy: [`x.astype()`](numpy.ndarray.astype)
    * PyTorch: [`x.to()`](https://pytorch.org/docs/stable/tensors.html#torch.Tensor.to)
    * TensorFlow: [`tf.cast`](https://www.tensorflow.org/api_docs/python/tf/cast)
    * Jax: [`jax.numpy.array`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.array.html)

    See Also:
        `to_float`, `to_int32`, `to_int64`, `to_complex`.

    Args:
        x: `Tensor`
        dtype: New data type as `phi.math.DType`, e.g. `DType(int, 16)`.

    Returns:
        `Tensor` with data type `dtype`
    """
    return x._op1(lambda native: choose_backend(native).cast(native, dtype=dtype))


def cast_same(*values: Tensor) -> Tuple[Tensor]:
    """
    Casts all tensors to the same `DType`.
    If all data types are of the same kind, returns the largest occurring data type.
    Otherwise casts `bool` &rarr; `int` &rarr; `float` &rarr; `complex`.

    Args:
        *values: tensors to cast

    Returns:
        Tuple of Tensors with same data type.
    """
    assert all(isinstance(v, Tensor) for v in values), f"Only Tensor arguments allowed but got {values}"
    dtypes = [v.dtype for v in values]
    if any(dt != dtypes[0] for dt in dtypes):
        common_type = combine_types(*dtypes, fp_precision=get_precision())
        return tuple([cast(v, common_type) for v in values])
    else:
        return values


def divide_no_nan(x, y):
    return custom_op2(x, y, divide_no_nan, lambda x_, y_: choose_backend(x_, y_).divide_no_nan(x_, y_), lambda y_, x_: divide_no_nan(x_, y_), lambda y_, x_: choose_backend(x_, y_).divide_no_nan(x_, y_))


def maximum(x: Tensor or float, y: Tensor or float):
    return custom_op2(x, y, maximum, lambda x_, y_: choose_backend(x_, y_).maximum(x_, y_))


def minimum(x: Tensor or float, y: Tensor or float):
    return custom_op2(x, y, minimum, lambda x_, y_: choose_backend(x_, y_).minimum(x_, y_))


def clip(x: Tensor, lower_limit: float or Tensor, upper_limit: float or Tensor):
    if isinstance(lower_limit, Number) and isinstance(upper_limit, Number):

        def clip_(x):
            return x._op1(lambda native: choose_backend(native).clip(native, lower_limit, upper_limit))

        return broadcast_op(clip_, [x])
    else:
        return maximum(lower_limit, minimum(x, upper_limit))


def convolve(value: Tensor,
             kernel: Tensor,
             extrapolation: 'e_.Extrapolation' = None) -> Tensor:
    """
    Computes the convolution of `value` and `kernel` along the spatial axes of `kernel`.

    The channel dimensions of `value` are reduced against the equally named dimensions of `kernel`.
    The result will have the non-reduced channel dimensions of `kernel`.

    Args:
        value: `Tensor` whose shape includes all spatial dimensions of `kernel`.
        kernel: `Tensor` used as convolutional filter.
        extrapolation: If not None, pads `value` so that the result has the same shape as `value`.

    Returns:
        `Tensor`
    """
    assert all(dim in value.shape for dim in kernel.shape.spatial.names), f"Value must have all spatial dimensions of kernel but got value {value} kernel {kernel}"
    conv_shape = kernel.shape.spatial
    in_channels = value.shape.channel
    out_channels = kernel.shape.channel.without(in_channels)
    batch = value.shape.batch & kernel.shape.batch
    if extrapolation is not None and extrapolation != e_.ZERO:
        value = pad(value, {dim: (kernel.shape.get_size(dim) // 2, (kernel.shape.get_size(dim) - 1) // 2)
                            for dim in conv_shape.name}, extrapolation)
    native_kernel = reshaped_native(kernel, (batch, out_channels, in_channels, *conv_shape.names), force_expand=in_channels)
    native_value = reshaped_native(value, (batch, in_channels, *conv_shape.names), force_expand=batch)
    backend = choose_backend(native_value, native_kernel)
    native_result = backend.conv(native_value, native_kernel, zero_padding=extrapolation == e_.ZERO)
    result = reshaped_tensor(native_result, (batch, out_channels, *conv_shape))
    return result


def unstack(value: Tensor, dim: str):
    """ Alias for `Tensor.unstack()` """
    return value.unstack(dim)


def boolean_mask(x: Tensor, dim: str, mask: Tensor):
    """
    Discards values `x.dim[i]` where `mask.dim[i]=False`.
    All dimensions of `mask` that are not `dim` are treated as batch dimensions.

    Alternative syntax: `x.dim[mask]`.

    Implementations:

    * NumPy: Slicing
    * PyTorch: [`masked_select`](https://pytorch.org/docs/stable/generated/torch.masked_select.html)
    * TensorFlow: [`tf.boolean_mask`](https://www.tensorflow.org/api_docs/python/tf/boolean_mask)
    * Jax: Slicing

    Args:
        x: `Tensor` of values.
        dim: Dimension of `x` to along which to discard slices.
        mask: Boolean `Tensor` marking which values to keep. Must have the dimension `dim` matching `x´.

    Returns:
        Selected values of `x` as `Tensor` with dimensions from `x` and `mask`.
    """
    def uniform_boolean_mask(x: Tensor, mask_1d: Tensor):
        if dim in x.shape:
            x_native = x.native(x.shape.names)  # order does not matter
            mask_native = mask_1d.native()  # only has 1 dim
            backend = choose_backend(x_native, mask_native)
            result_native = backend.boolean_mask(x_native, mask_native, axis=x.shape.index(dim))
            new_shape = x.shape.with_sizes(backend.staticshape(result_native))
            return NativeTensor(result_native, new_shape)
        else:
            total = int(sum_(to_int64(mask_1d)))
            new_shape = mask_1d.shape.with_sizes([total])
            return expand(x, new_shape)

    return broadcast_op(uniform_boolean_mask, [x, mask], iter_dims=mask.shape.without(dim))


def gather(values: Tensor, indices: Tensor):
    batch = values.shape.batch & indices.shape.batch
    native_values = reshaped_native(values, [batch, *values.shape.spatial, values.shape.channel])
    native_indices = reshaped_native(indices, [batch, *indices.shape.non_batch])
    backend = choose_backend(native_values, native_indices)
    native_result = backend.batched_gather_nd(native_values, native_indices)
    result = reshaped_tensor(native_result, [batch, *indices.shape.non_channel.non_batch, values.shape.channel])
    return result


def scatter(base_grid: Tensor or Shape,
            indices: Tensor,
            values: Tensor,
            mode: str = 'update',
            outside_handling: str = 'discard',
            indices_gradient=False):
    """
    Scatters `values` into `base_grid` at `indices`.
    Collection dimensions of `indices` and/or `values` are reduced during scattering.
    Depending on `mode`, this method has one of the following effects:

    * `mode='update'`: Replaces the values of `base_grid` at `indices` by `values`. The result is undefined if `indices` contains duplicates.
    * `mode='add'`: Adds `values` to `base_grid` at `indices`. The values corresponding to duplicate indices are accumulated.
    * `mode='mean'`: Replaces the values of `base_grid` at `indices` by the mean of all `values` with the same index.

    Implementations:

    * NumPy: Slice assignment / `numpy.add.at`
    * PyTorch: [`torch.scatter`](https://pytorch.org/docs/stable/generated/torch.scatter.html), [`torch.scatter_add`](https://pytorch.org/docs/stable/generated/torch.scatter_add.html)
    * TensorFlow: [`tf.tensor_scatter_nd_add`](https://www.tensorflow.org/api_docs/python/tf/tensor_scatter_nd_add), [`tf.tensor_scatter_nd_update`](https://www.tensorflow.org/api_docs/python/tf/tensor_scatter_nd_update)
    * Jax: [`jax.lax.scatter_add`](https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.scatter_add.html), [`jax.lax.scatter`](https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.scatter.html)

    Args:
        base_grid: `Tensor` into which `values` are scattered.
        indices: `Tensor` of n-dimensional indices at which to place `values`.
            Must have a single channel dimension with size matching the number of spatial dimensions of `base_grid`.
            This dimension is optional if the spatial rank is 1.
            Must also contain all `scatter_dims`.
        values: `Tensor` of values to scatter at `indices`.
        mode: Scatter mode as `str`. One of ('add', 'mean', 'update')
        outside_handling: Defines how indices lying outside the bounds of `base_grid` are handled.

            * `'discard'`: outside indices are ignored.
            * `'clamp'`: outside indices are projected onto the closest point inside the grid.
            * `'undefined'`: All points are expected to lie inside the grid. Otherwise an error may be thrown or an undefined tensor may be returned.
        indices_gradient: Whether to allow the gradient of this operation to be backpropagated through `indices`.

    Returns:
        Copy of `base_grid` with updated values at `indices`.
    """
    assert mode in ('update', 'add', 'mean')
    assert outside_handling in ('discard', 'clamp', 'undefined')
    assert isinstance(indices_gradient, bool)
    grid_shape = base_grid if isinstance(base_grid, Shape) else base_grid.shape
    assert indices.shape.channel.names == ('vector',) or (grid_shape.spatial_rank == 1 and indices.shape.channel_rank == 0)
    batches = values.shape.non_channel.non_collection & indices.shape.non_channel.non_collection
    channels = grid_shape.channel & values.shape.channel
    # --- Set up grid ---
    if isinstance(base_grid, Shape):
        with choose_backend_t(indices, values):
            base_grid = zeros(base_grid & batches & values.shape.channel)
        if mode != 'add':
            base_grid += math.nan
    # --- Handle outside indices ---
    if outside_handling == 'clamp':
        indices = clip(indices, 0, tensor(grid_shape.spatial, channel('vector')) - 1)
    elif outside_handling == 'discard':
        indices_inside = min_((round_(indices) >= 0) & (round_(indices) < tensor(grid_shape.spatial, channel('vector'))), 'vector')
        indices = boolean_mask(indices, indices.shape.collection.name, indices_inside)
        if collection(values).rank > 0:
            values = boolean_mask(values, values.shape.collection.name, indices_inside)
        if indices.shape.is_non_uniform:
            raise NotImplementedError()
    lists = indices.shape.collection & values.shape.collection

    def scatter_forward(base_grid, indices, values):
        indices = to_int32(round_(indices))
        native_grid = reshaped_native(base_grid, [batches, *base_grid.shape.spatial.names, channels], force_expand=True)
        native_values = reshaped_native(values, [batches, lists, channels], force_expand=True)
        native_indices = reshaped_native(indices, [batches, lists, 'vector'], force_expand=True)
        backend = choose_backend(native_indices, native_values, native_grid)
        if mode in ('add', 'update'):
            native_result = backend.scatter(native_grid, native_indices, native_values, mode=mode)
        else:  # mean
            zero_grid = backend.zeros_like(native_grid)
            summed = backend.scatter(zero_grid, native_indices, native_values, mode='add')
            count = backend.scatter(zero_grid, native_indices, backend.ones_like(native_values), mode='add')
            native_result = summed / backend.maximum(count, 1)
            native_result = backend.where(count == 0, native_grid, native_result)
        return reshaped_tensor(native_result, [batches, *spatial(base_grid), channels], check_sizes=True)

    def scatter_backward(shaped_base_grid_, shaped_indices_, shaped_values_, output, d_output):
        from ._nd import spatial_gradient
        values_grad = gather(d_output, shaped_indices_)
        spatial_gradient_indices = gather(spatial_gradient(d_output), shaped_indices_)
        indices_grad = mean(spatial_gradient_indices * shaped_values_, 'vector_')
        return None, indices_grad, values_grad

    scatter_function = scatter_forward
    if indices_gradient:
        from phi.math import custom_gradient
        scatter_function = custom_gradient(scatter_forward, scatter_backward)

    result = scatter_function(base_grid, indices, values)
    return result


def fft(x: Tensor) -> Tensor:
    """
    Performs a fast Fourier transform (FFT) on all spatial dimensions of x.
    
    The inverse operation is `ifft()`.

    Implementations:

    * NumPy: [`np.fft.fft`](https://numpy.org/doc/stable/reference/generated/numpy.fft.fft.html),
      [`numpy.fft.fft2`](https://numpy.org/doc/stable/reference/generated/numpy.fft.fft2.html),
      [`numpy.fft.fftn`](https://numpy.org/doc/stable/reference/generated/numpy.fft.fftn.html)
    * PyTorch: [`torch.fft.fft`](https://pytorch.org/docs/stable/fft.html)
    * TensorFlow: [`tf.signal.fft`](https://www.tensorflow.org/api_docs/python/tf/signal/fft),
      [`tf.signal.fft2d`](https://www.tensorflow.org/api_docs/python/tf/signal/fft2d),
      [`tf.signal.fft3d`](https://www.tensorflow.org/api_docs/python/tf/signal/fft3d)
    * Jax: [`jax.numpy.fft.fft`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.fft.fft.html),
      [`jax.numpy.fft.fft2`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.fft.fft2.html)
      [`jax.numpy.fft.fft`](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.fft.fftn.html)

    Args:
        x: Complex or float `Tensor` with at least one spatial dimension.

    Returns:
        *Ƒ(x)* as complex `Tensor`

    """
    native, assemble = _invertible_standard_form(x)
    result = choose_backend(native).fft(native)
    return assemble(result)


def ifft(k: Tensor):
    """
    Inverse of `fft()`.

    Args:
        k: Complex or float `Tensor` with at least one spatial dimension.

    Returns:
        *Ƒ<sup>-1</sup>(k)* as complex `Tensor`
    """
    native, assemble = _invertible_standard_form(k)
    result = choose_backend(native).ifft(native)
    return assemble(result)


def dtype(x):
    if isinstance(x, Tensor):
        return x.dtype
    else:
        return choose_backend(x).dtype(x)


def expand(value: Tensor, dims: Shape):
    """
    Adds dimensions to a `Tensor` by implicitly repeating the tensor values along the new dimensions.
    If `value` already contains some of the new dimensions, a size and type check is performed instead.

    This function replaces the usual `tile` / `repeat` functions of
    [NumPy](https://numpy.org/doc/stable/reference/generated/numpy.tile.html),
    [PyTorch](https://pytorch.org/docs/stable/tensors.html#torch.Tensor.repeat),
    [TensorFlow](https://www.tensorflow.org/api_docs/python/tf/tile) and
    [Jax](https://jax.readthedocs.io/en/latest/_autosummary/jax.numpy.tile.html).

    Additionally, it replaces the traditional `unsqueeze` / `expand_dims` functions.

    Args:
        value: `Tensor`
        dims: Dimensions to be added as `Shape`

    Returns:
        Expanded `Tensor`.
    """
    value = wrap(value)
    shape = value.shape
    for dim in dims.reversed:
        if dim in value.shape:
            assert dim.size is None or shape.get_size(dim.name) == dim.size, f"Cannot expand tensor with shape {shape} by dimension {dim}"
            assert shape.get_type(dim.name) == dim.type, f"Cannot expand tensor with shape {shape} by dimension {dim} of type '{dim.type}' because the dimension types do not match. Original type of '{dim.name}' was {shape.get_type(dim.name)}."
        else:
            if dim.size is None:
                dim = dim.with_sizes([1])
            shape = shape.expand(dim)
    return CollapsedTensor(value, shape)


def _invertible_standard_form(value: Tensor):
    """
    Reshapes the tensor into the shape (batch, spatial..., channel) with a single batch and channel dimension.

    Args:
      value: tensor to reshape
      value: Tensor: 

    Returns:
      reshaped native tensor, inverse function

    """
    normal_order = value.shape.normal_order()
    native = value.native(normal_order.names)
    backend = choose_backend(native)
    standard_form = (value.shape.batch.volume,) + value.shape.spatial.sizes + (value.shape.channel.volume,)
    reshaped = backend.reshape(native, standard_form)

    def assemble(reshaped):
        un_reshaped = backend.reshape(reshaped, backend.shape(native))
        return NativeTensor(un_reshaped, normal_order)

    return reshaped, assemble


def close(*tensors, rel_tolerance=1e-5, abs_tolerance=0):
    """
    Checks whether all tensors have equal values within the specified tolerance.
    
    Does not check that the shapes exactly match.
    Tensors with different shapes are reshaped before comparing.

    Args:
      tensors: tensor or tensor-like (constant) each
      rel_tolerance: relative tolerance (Default value = 1e-5)
      abs_tolerance: absolute tolerance (Default value = 0)
      *tensors: 

    Returns:

    """
    tensors = [wrap(t) for t in tensors]
    for other in tensors[1:]:
        if not _close(tensors[0], other, rel_tolerance=rel_tolerance, abs_tolerance=abs_tolerance):
            return False
    return True


def _close(tensor1, tensor2, rel_tolerance=1e-5, abs_tolerance=0):
    if tensor2 is tensor1:
        return True
    new_shape, (native1, native2) = broadcastable_native_tensors(tensor1, tensor2)
    np1 = choose_backend(native1).numpy(native1)
    np2 = choose_backend(native2).numpy(native2)
    return np.allclose(np1, np2, rel_tolerance, abs_tolerance)


def assert_close(*values,
                 rel_tolerance: float = 1e-5,
                 abs_tolerance: float = 0,
                 msg: str = "",
                 verbose: bool = True):
    """
    Checks that all given tensors have equal values within the specified tolerance.
    Raises an AssertionError if the values of this tensor are not within tolerance of any of the other tensors.
    
    Does not check that the shapes match as long as they can be broadcast to a common shape.

    Args:
      values: Tensors or native tensors or numbers or sequences of numbers.
      rel_tolerance: Relative tolerance.
      abs_tolerance: Absolute tolerance.
      msg: Optional error message.
      verbose: Whether to print conflicting values.
    """
    phi_tensors = [t for t in values if isinstance(t, Tensor)]
    if phi_tensors:
        values = [compatible_tensor(t, phi_tensors[0].shape)._simplify() for t in values]  # use Tensor to infer dimensions
        for other in values[1:]:
            _assert_close(values[0], other, rel_tolerance, abs_tolerance, msg, verbose)
    else:
        np_values = [choose_backend(t).numpy(t) for t in values]
        for other in np_values[1:]:
            np.testing.assert_allclose(np_values[0], other, rel_tolerance, abs_tolerance, err_msg=msg, verbose=verbose)


def _assert_close(tensor1, tensor2, rel_tolerance: float, abs_tolerance: float, msg: str, verbose: bool):
    if tensor2 is tensor1:
        return
    if isinstance(tensor2, (int, float, bool)):
        np.testing.assert_allclose(tensor1.numpy(), tensor2, rel_tolerance, abs_tolerance)

    def inner_assert_close(tensor1, tensor2):
        new_shape, (native1, native2) = broadcastable_native_tensors(tensor1, tensor2)
        np1 = choose_backend(native1).numpy(native1)
        np2 = choose_backend(native2).numpy(native2)
        if not np.allclose(np1, np2, rel_tolerance, abs_tolerance):
            np.testing.assert_allclose(np1, np2, rel_tolerance, abs_tolerance, err_msg=msg, verbose=verbose)

    broadcast_op(inner_assert_close, [tensor1, tensor2], no_return=True)


def _native_wrapper(tensor_function: Callable, create_native_function: Callable, persistent_refs=False):
    INPUT_TENSORS = []
    OUTPUT_TENSORS = []

    def native_function(*natives):
        natives = list(natives)
        values = [t._op1(lambda _: natives.pop(0)) for t in INPUT_TENSORS]
        assert len(natives) == 0, "Not all arguments were converted"
        result = tensor_function(*values)
        results = [result] if not isinstance(result, (tuple, list)) else result
        OUTPUT_TENSORS.clear()
        OUTPUT_TENSORS.extend(results)
        return sum([v._natives() for v in results], ())

    backend = default_backend()
    traced = create_native_function(native_function, backend)
    if traced is NotImplemented:
        warnings.warn(f"Backend '{backend}' not supported. Returning original function.")
        return tensor_function, None, INPUT_TENSORS, OUTPUT_TENSORS

    def wrapper(*values: Tensor):
        INPUT_TENSORS.clear()
        INPUT_TENSORS.extend(values)
        for v in values:
            v._expand()
        natives = sum([v._natives() for v in values], ())
        results_native = list(traced(*natives))
        results = [t._with_natives_replaced(results_native) for t in OUTPUT_TENSORS]
        if not persistent_refs:
            INPUT_TENSORS.clear()
            # OUTPUT_TENSORS.clear()  outputs need to be saved because native_function may be called only the first time. Will get garbage collected once the function is not referenced anymore.
        assert len(results_native) == 0
        return results[0] if len(results) == 1 else results

    return wrapper, traced, INPUT_TENSORS, OUTPUT_TENSORS

# def variable(x: Tensor) -> Tensor:
#     """
#     Returns a copy of `x` for which gradients are recorded automatically.
#     The function `gradients()` may be used to compute gradients of a Tensor derived from `x` w.r.t. `x`.
#
#     If the backend of `x` does not support variables, converts `x` to the default backend.
#     If the default backend does not support variables, raises an exception.
#
#     Alternatively, `record_gradients()` may be used to record gradients only for specific operations.
#
#     Args:
#         x: Parameter for which gradients of the form dL/dx may be computed
#
#     Returns:
#         copy of `x`
#     """
#     x._expand()  # CollapsedTensors need to be expanded early
#
#     def create_var(native):
#         native_backend = choose_backend(native)
#         native_backend_var = native_backend.variable(native)
#         if native_backend_var is not NotImplemented:
#             return native_backend_var
#         default_be = default_backend()
#         if default_be == native_backend:
#             raise AssertionError(f"The backend '{native_backend}' does not support variables.")
#         native = default_be.as_tensor(native, convert_external=True)
#         default_backend_var = default_be.variable(native)
#         if default_backend_var is not NotImplemented:
#             return default_backend_var
#         raise AssertionError(f"The default backend '{default_be}' does not support variables.")
#
#     result = x._op1(create_var)
#     return result


@contextmanager
def record_gradients(*x: Tensor, persistent=False):
    """
    *Deprecated. Use `functional_gradient()` instead.*

    Context expression to record gradients for operations within that directly or indirectly depend on `x`.

    The function `gradients()` may be called within the context to evaluate the gradients of a Tensor derived from `x` w.r.t. `x`.

    Args:
        *x: Parameters for which gradients of the form dL/dx may be computed
        persistent: if `False`, `gradients()` may only be called once within the context
    """
    warnings.warn("math.record_gradients() is deprecated. Use functional_gradient() instead.", DeprecationWarning)
    for x_ in x:
        x_._expand()
    natives = sum([x_._natives() for x_ in x], ())
    backend = choose_backend(*natives)
    ctx = backend.record_gradients(natives, persistent=persistent)
    _PARAM_STACK.append(x)
    ctx.__enter__()
    try:
        yield None
    finally:
        ctx.__exit__(None, None, None)
        _PARAM_STACK.pop(0)


_PARAM_STACK = []  # list of tuples


def gradients(y: Tensor,
              *x: Tensor,
              grad_y: Tensor or None = None):
    """
    *Deprecated. Use `functional_gradient()` instead.*

    Computes the gradients dy/dx for each `x`.
    The parameters `x` must be marked prior to all operations for which gradients should be recorded using `record_gradients()`.

    Args:
        y: Scalar `Tensor` computed from `x`, typically loss.
        *x: (Optional) Parameter tensors which were previously marked in `record_gradients()`.
            If empty, computes the gradients w.r.t. all marked tensors.
        grad_y: (optional) Gradient at `y`, defaults to 1.

    Returns:
        Single `Tensor` if one `x` was passed, else sequence of tensors.
    """
    warnings.warn("math.gradients() is deprecated. Use functional_gradient() instead.", DeprecationWarning)
    assert isinstance(y, NativeTensor), f"{type(y)}"
    if len(x) == 0:
        x = _PARAM_STACK[-1]
    backend = choose_backend_t(y, *x)
    x_natives = sum([x_._natives() for x_ in x], ())
    native_gradients = list(backend.gradients(y.native(), x_natives, grad_y.native() if grad_y is not None else None))
    for i, grad in enumerate(native_gradients):
        assert grad is not None, f"Missing spatial_gradient for source with shape {x_natives[i].shape}"
    grads = [x_._op1(lambda native: native_gradients.pop(0)) for x_ in x]
    return grads[0] if len(x) == 1 else grads


def stop_gradient(x):
    """
    Disables gradients for the given tensor.
    This may switch off the gradients for `x` itself or create a copy of `x` with disabled gradients.

    Implementations:

    * PyTorch: [`x.detach()`](https://pytorch.org/docs/stable/autograd.html#torch.Tensor.detach)
    * TensorFlow: [`tf.stop_gradient`](https://www.tensorflow.org/api_docs/python/tf/stop_gradient)
    * Jax: [`jax.lax.stop_gradient`](https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.stop_gradient.html)

    Args:
        x: `Tensor` or `TensorLike` for which gradients should be disabled.

    Returns:
        Copy of `x`.
    """
    if isinstance(x, Tensor):
        return x._op1(lambda native: choose_backend(native).stop_gradient(native))
    elif isinstance(x, TensorLike):
        nest, values = disassemble_tree(x)
        new_values = [stop_gradient(v) for v in values]
        return assemble_tree(nest, new_values)
    else:
        return wrap(choose_backend(x).stop_gradient(x))
