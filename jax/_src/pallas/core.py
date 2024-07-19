# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module for pallas-core functionality."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import contextlib
import copy
import dataclasses
import enum
import functools
import threading
from typing import Any, Union
import warnings

import jax
from jax._src import api_util
from jax._src import core as jax_core
from jax._src import deprecations
from jax._src import linear_util as lu
from jax._src import state
from jax._src import tree_util
from jax._src import util
from jax._src.interpreters import partial_eval as pe
from jax._src.state import discharge as state_discharge
import jax.numpy as jnp


class DynamicGridDim:
  pass
dynamic_grid_dim = DynamicGridDim()


partial = functools.partial
Grid = tuple[Union[int, jax_core.Array], ...]
StaticGrid = tuple[int, ...]
GridMappingGrid = tuple[Union[int, DynamicGridDim], ...]
split_list = util.split_list

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip


class AbstractMemoryRef(state.AbstractRef):
  __slots__ = ["inner_aval", "memory_space"]

  def __init__(self, inner_aval: jax_core.AbstractValue,
               memory_space: Any):
    assert isinstance(inner_aval, jax_core.ShapedArray)
    self.inner_aval = inner_aval
    self.memory_space = memory_space

  def __repr__(self) -> str:
    return f'MemRef<{self.memory_space}>{{{self.inner_aval.str_short()}}}'

  def join(self, other):
    assert isinstance(other, AbstractMemoryRef)
    return AbstractMemoryRef(self.inner_aval.join(other.inner_aval),
                             self.memory_space)

  def update(self, inner_aval=None, memory_space=None):
    inner_aval = self.inner_aval if inner_aval is None else inner_aval
    memory_space = self.memory_space if memory_space is None else memory_space
    return AbstractMemoryRef(inner_aval, memory_space)

  def at_least_vspace(self):
    return AbstractMemoryRef(
        self.inner_aval.at_least_vspace(), self.memory_space)

  def __eq__(self, other):
    return (type(self) is type(other) and self.inner_aval == other.inner_aval
            and self.memory_space == other.memory_space)

  def __hash__(self):
    return hash((self.__class__, self.inner_aval, self.memory_space))


class MemorySpace(enum.Enum):
  """ Logical, device-agnostic memory spaces.

  Each memory space will be translated to a device-specific memory
  type during lowering.
  """
  ERROR = "error"  # Memory space for checkify errors.
  INDEX = "index"  # Memory space for scalar prefetch arguments.

  def __str__(self) -> str:
    return self.value


def _ref_raise_to_shaped(ref_aval: AbstractMemoryRef, weak_type):
  return AbstractMemoryRef(
      jax_core.raise_to_shaped(ref_aval.inner_aval, weak_type),
      ref_aval.memory_space)
jax_core.raise_to_shaped_mappings[AbstractMemoryRef] = _ref_raise_to_shaped


@dataclasses.dataclass(frozen=True)
class PallasGridContext:
  grid: GridMappingGrid
  mapped_dims: tuple[int, ...]

  def size(self, axis: int) -> int | DynamicGridDim:
    valid_grid = tuple(
        s for i, s in enumerate(self.grid) if i not in self.mapped_dims
    )
    try:
      size = valid_grid[axis]
    except IndexError as e:
      raise ValueError(
          f"Axis {axis} is out of bounds for grid {self.grid}"
      ) from e
    return size


@dataclasses.dataclass
class PallasTracingEnv(threading.local):
  grid_context: PallasGridContext | None = None
_pallas_tracing_env = PallasTracingEnv()


def axis_frame() -> PallasGridContext:
  # This is like jax_core.axis_frame, except there should only ever be one
  # active PallasGridAxisName for a particular main_trace because we cannot
  # nest pallas_calls.
  env = _pallas_tracing_env
  assert env.grid_context is not None
  return env.grid_context


@dataclasses.dataclass(frozen=True)
class GridAxis:
  index: jax.Array
  size: int

# Stores the kernel execution position and the size along grid axes.
GridEnv = Sequence[GridAxis]

_grid_env_stack: list[GridEnv] = []


@contextlib.contextmanager
def grid_env(env: GridEnv) -> Iterator[None]:
  _grid_env_stack.append(env)
  try:
    yield
  finally:
    _grid_env_stack.pop()


def current_grid_env() -> GridEnv | None:
  if not _grid_env_stack:
    return None
  return _grid_env_stack[-1]


class Mapped:
  pass
mapped = Mapped()


@dataclasses.dataclass(frozen=True)
class Unblocked:
  padding: tuple[tuple[int, int], ...] | None = None
unblocked = Unblocked()


class Blocked:
  pass
blocked = Blocked()


IndexingMode = Union[Blocked, Unblocked]


@dataclasses.dataclass(unsafe_hash=True)
class BlockSpec:
  """Specifies how an array should be sliced for each iteration of a kernel.

  This encodes the parameters as passed from the API, before canonicalization.
  BlockMapping contains a canonicalized internal form of this information.

  See :ref:`pallas_blockspec` for more details.
  """
  block_shape: tuple[int | None, ...] | None = None
  index_map: Callable[..., Any] | None = None
  memory_space: Any | None = dataclasses.field(kw_only=True, default=None)
  indexing_mode: IndexingMode = dataclasses.field(kw_only=True, default=blocked)

  def __init__(
      self,
      block_shape: Any | None = None,
      index_map: Any | None = None,
      *,
      memory_space: Any | None = None,
      indexing_mode: IndexingMode = blocked,
  ) -> None:
    if callable(block_shape):
      # TODO(slebedev): Remove this code path and update the signature of
      # __init__ after October 1, 2024.
      message = (
          "BlockSpec now expects ``block_shape`` to be passed before"
          " ``index_map``. Update your code by swapping the order of these"
          " arguments. For example, ``pl.BlockSpace(lambda i: i, (42,))``"
          " should be written as ``pl.BlockSpec((42,), lambda i: i)``."
      )
      if deprecations.is_accelerated("pallas-block-spec-order"):
        raise TypeError(message)
      warnings.warn(message, DeprecationWarning)
      index_map, block_shape = block_shape, index_map

    self.block_shape = block_shape
    self.index_map = index_map
    self.memory_space = memory_space
    self.indexing_mode = indexing_mode

  def compute_index(self, *args):
    assert self.index_map is not None
    out = self.index_map(*args)
    if not isinstance(out, tuple):
      out = (out,)
    return out

class NoBlockSpec:
  pass
no_block_spec = NoBlockSpec()


# A PyTree of BlockSpec | NoBlockSpec.
BlockSpecTree = Any


@dataclasses.dataclass(frozen=True)
class BlockMapping:
  """Canonicalized internal version of BlockSpec."""
  block_shape: tuple[Mapped | int, ...]
  index_map_jaxpr: jax_core.ClosedJaxpr
  indexing_mode: IndexingMode

  def compute_start_indices(self, loop_idx, *args):
    discharged_jaxpr, discharged_consts = state_discharge.discharge_state(
        self.index_map_jaxpr.jaxpr, self.index_map_jaxpr.consts
    )
    jaxpr = jax_core.ClosedJaxpr(discharged_jaxpr, discharged_consts)
    block_indices_and_rest = jax_core.jaxpr_as_fun(jaxpr)(*loop_idx, *args)
    # Since we're passing in `Ref`s potentially, we need to split out their
    # updated values since we only care about the return values.
    block_indices, _ = split_list(block_indices_and_rest,
                                  [len(self.block_shape)])
    if isinstance(self.indexing_mode, Blocked):
      return tuple(i if b is mapped else b * i
                  for b, i in zip(self.block_shape, block_indices))
    elif isinstance(self.indexing_mode, Unblocked):
      return block_indices
    else:
      raise RuntimeError(f"Unknown indexing mode: {self.indexing_mode}")

  replace = dataclasses.replace


@contextlib.contextmanager
def tracing_grid_env(grid: GridMappingGrid, mapped_dims: tuple[int, ...]):
  assert all(i is dynamic_grid_dim or isinstance(i, int) for i in grid)
  old_grid_context = _pallas_tracing_env.grid_context
  try:
    _pallas_tracing_env.grid_context = PallasGridContext(grid, mapped_dims)
    yield
  finally:
    _pallas_tracing_env.grid_context = old_grid_context


@dataclasses.dataclass(frozen=True)
class GridMapping:
  """Canonicalized internal version of GridSpec."""
  grid: GridMappingGrid
  block_mappings: tuple[BlockMapping, ...]
  mapped_dims: tuple[int, ...] = ()
  num_index_operands: int = 0
  num_scratch_operands: int = 0
  # Number of constants hoisted to operands by ``_hoist_consts_to_refs``.
  num_constant_operands: int = 0

  replace = dataclasses.replace

  @property
  def num_dynamic_grid_bounds(self):
    return sum(b is dynamic_grid_dim for b in self.grid)

  @property
  def static_grid(self) -> StaticGrid:
    if self.num_dynamic_grid_bounds:
      raise ValueError("Expected a grid with fully static bounds")
    return self.grid  # type: ignore

  @contextlib.contextmanager
  def trace_env(self):
    with tracing_grid_env(self.grid, self.mapped_dims):
      yield


def _preprocess_grid(grid: Grid | int | None) -> Grid:
  if grid is None:
    return ()
  if isinstance(grid, int):
    return (grid,)
  return grid


def _convert_block_spec_to_block_mapping(
    block_spec: BlockSpec,
    path: tree_util.KeyPath,
    aval: jax_core.ShapedArray,
    *,
    in_avals: Sequence[jax_core.ShapedArray],
    in_tree: Any,
    grid: GridMappingGrid,
    mapped_dims: tuple[int, ...],
    what: str,  # Used to localize error messages, e.g., {what}{path}
) -> BlockMapping:
  if block_spec is no_block_spec:
    block_spec = BlockSpec(None, None)
  if block_spec.index_map is None:
    compute_index = lambda *args, **kwargs: (0,) * len(aval.shape)
  else:
    compute_index = block_spec.compute_index
  if block_spec.block_shape is None:
    block_shape = aval.shape
  else:
    block_shape = block_spec.block_shape
  block_shape = tuple(
      mapped if s is None else s for s in block_shape)
  flat_fun, _ = api_util.flatten_fun(lu.wrap_init(compute_index), in_tree)
  with tracing_grid_env(grid, mapped_dims):
    jaxpr, out_avals, consts, () = pe.trace_to_jaxpr_dynamic(flat_fun, in_avals)
    if len(out_avals) != len(block_shape):
      raise ValueError(
          f"Index map for {what}{tree_util.keystr(path)} must return "
          f"{len(aval.shape)} values to match {block_shape=}. "
          f"Currently returning {len(out_avals)} values."
      )
  return BlockMapping(
      block_shape, jax_core.ClosedJaxpr(jaxpr, consts), block_spec.indexing_mode
  )


def _tile_ref(ref: state.AbstractRef, block_shape: tuple[int, ...] | None
             ) -> state.AbstractRef:
  if block_shape is None:
    return ref
  shape = tuple(s for s in block_shape if s is not None)
  return ref.update(inner_aval=ref.inner_aval.update(shape=shape))


def _get_ref_avals(in_avals: Sequence[jax_core.ShapedArray],
                   in_specs: Sequence[BlockSpec],
                   in_paths: Sequence[tree_util.KeyPath],
                   out_avals: Sequence[jax_core.ShapedArray],
                   out_specs: Sequence[BlockSpec],
                   out_paths: Sequence[tree_util.KeyPath]):
  def make_ref_aval(aval: jax_core.ShapedArray,
                    spec: BlockSpec,
                    path: tree_util.KeyPath,
                    what: str) -> state.AbstractRef:
    if spec is no_block_spec:
      memory_space = None
      block_shape = None
    else:
      memory_space = spec.memory_space
      block_shape = spec.block_shape

    ref_aval = AbstractMemoryRef(aval, memory_space)
    if block_shape is not None:
      if len(ref_aval.shape) != len(block_shape):
        raise ValueError(
            f"Block shape for {what}{tree_util.keystr(path)} (= {block_shape}) "
            f"must have the same number of dimensions as the array shape {ref_aval.shape}"
        )
      block_shape_unmapped = tuple(s for s in block_shape if s is not None)
      ref_aval = ref_aval.update(
          inner_aval=ref_aval.inner_aval.update(shape=block_shape_unmapped))

    if not jax_core.is_constant_shape(ref_aval.shape):
      raise ValueError(
          "shape polymorphism for Pallas does not support "
          "dynamically-shaped blocks. "
          f"{what}{tree_util.keystr(path)} has block_shape: {ref_aval.shape}")
    return ref_aval

  in_ref_avals = [
      make_ref_aval(aval, in_spec, in_path, "input")
      for aval, in_spec, in_path in zip(in_avals, in_specs, in_paths)
  ]
  out_ref_avals = [
      make_ref_aval(aval, out_spec, out_path, "output")
      for aval, out_spec, out_path in zip(out_avals, out_specs, out_paths)
  ]
  return in_ref_avals, out_ref_avals


@dataclasses.dataclass(init=False, unsafe_hash=True)
class GridSpec:
  """Specifies the invocation grid and the block specs for inputs and outputs.

  This encodes the parameters as passed from the API, before canonicalization.
  GridMapping contains the canonicalized internal form of this information.
  """
  grid: Grid
  in_specs: tuple[BlockSpec | NoBlockSpec, ...] | NoBlockSpec
  out_specs: tuple[BlockSpec | NoBlockSpec, ...] | NoBlockSpec
  in_specs_tree: Any
  out_specs_tree: Any

  def __init__(
      self,
      grid: Grid | None = None,
      in_specs: BlockSpecTree = no_block_spec,
      out_specs: BlockSpecTree = no_block_spec,
  ):
    # Be more lenient for in/out_specs
    if isinstance(in_specs, list):
      in_specs = tuple(in_specs)
    elif in_specs is not no_block_spec and not isinstance(in_specs, Sequence):
      raise ValueError(f"`in_specs` must be a tuple or a list. Found: {in_specs}")
    if isinstance(out_specs, list):
      out_specs = tuple(out_specs)

    self.grid = _preprocess_grid(grid)
    if in_specs is not no_block_spec:
      flat_in_specs, self.in_specs_tree = tree_util.tree_flatten(in_specs)
      self.in_specs = tuple(flat_in_specs)
    else:
      self.in_specs = in_specs
      self.in_specs_tree = None
    if out_specs is not no_block_spec:
      flat_out_specs, self.out_specs_tree = tree_util.tree_flatten(out_specs)
      self.out_specs = tuple(flat_out_specs)
    else:
      self.out_specs = out_specs
      self.out_specs_tree = None

  def _get_in_out_specs(self, in_avals, in_tree, out_avals, out_tree):
    if self.in_specs is no_block_spec:
      flat_in_specs = [no_block_spec] * len(in_avals)
    else:
      flat_in_specs = self.in_specs
      if self.in_specs_tree != in_tree:
        raise ValueError(
            pytreedef_mismatch_err_msg("`in_specs`", self.in_specs_tree,
                                       "inputs", in_tree))
    if self.out_specs is no_block_spec:
      flat_out_specs = [no_block_spec] * len(out_avals)
    else:
      flat_out_specs = self.out_specs
      if self.out_specs_tree != out_tree:
        raise ValueError(
            pytreedef_mismatch_err_msg("`out_specs`", self.out_specs_tree,
                                       "`out_shape`", out_tree))
    return flat_in_specs, flat_out_specs

  def get_grid_mapping(
      self, in_avals, in_tree, in_paths, out_avals, out_tree, out_paths
  ) -> tuple[tuple[jax_core.AbstractValue, ...], GridMapping]:
    assert all(i is None or isinstance(i, int) for i in self.grid)
    grid_mapping_grid = tuple(
        dynamic_grid_dim if d is None else d for d in self.grid
    )
    flat_in_specs, flat_out_specs = self._get_in_out_specs(
        in_avals, in_tree, out_avals, out_tree)
    in_ref_avals, out_ref_avals = _get_ref_avals(
        in_avals, flat_in_specs, in_paths,
        out_avals, flat_out_specs, out_paths)
    grid_avals = [jax_core.ShapedArray((), jnp.dtype("int32"))] * len(self.grid)
    # Create args, kwargs pytree def
    grid_tree = tree_util.tree_structure((tuple(grid_avals), {}))
    in_block_mappings = map(
        partial(
            _convert_block_spec_to_block_mapping,
            in_avals=grid_avals,
            in_tree=grid_tree,
            grid=grid_mapping_grid,
            mapped_dims=(),
            what="input",
        ),
        flat_in_specs,
        in_paths,
        in_ref_avals,
    )
    out_block_mappings = map(
        partial(
            _convert_block_spec_to_block_mapping,
            in_avals=grid_avals,
            in_tree=grid_tree,
            grid=grid_mapping_grid,
            mapped_dims=(),
            what="output",
        ),
        flat_out_specs,
        out_paths,
        out_ref_avals,
    )
    grid_mapping = GridMapping(
        grid_mapping_grid, (*in_block_mappings, *out_block_mappings)  # type: ignore
    )
    jaxpr_in_avals = tree_util.tree_unflatten(in_tree, in_ref_avals)
    jaxpr_out_avals = tree_util.tree_unflatten(out_tree, out_ref_avals)
    if not isinstance(jaxpr_out_avals, (tuple, list)):
      jaxpr_out_avals = (jaxpr_out_avals,)
    return (*jaxpr_in_avals, *jaxpr_out_avals), grid_mapping

  def unzip_dynamic_grid_bounds(
      self,
  ) -> tuple[GridSpec, tuple[Any, ...]]:
    static_grid = tuple(
        d if isinstance(d, int) else None for d in self.grid
    )
    dynamic_bounds = tuple(d for d in self.grid if not isinstance(d, int))
    # We can't use dataclasses.replace, because our fields are incompatible
    # with __init__'s signature.
    static_self = copy.copy(self)
    static_self.grid = static_grid  # type: ignore
    return static_self, dynamic_bounds

def pytreedef_mismatch_err_msg(
    what1: str, tree1: tree_util.PyTreeDef,
    what2: str, tree2: tree_util.PyTreeDef) -> str:
  errs = list(tree_util.equality_errors_pytreedef(tree1, tree2))
  msg = []
  msg.append(
      f"Pytree for {what1} and {what2} do not match. "
      f"There are {len(errs)} mismatches, including:")
  for path, thing1, thing2, explanation in errs:
    where = f"at {tree_util.keystr(path)}, " if path else ""
    msg.append(
        f"    * {where}{what1} is a {thing1} but"
        f" {what2} is a {thing2}, so {explanation}")
  return "\n".join(msg)
