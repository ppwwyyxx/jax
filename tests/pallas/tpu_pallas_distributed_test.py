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

"""Tests for distributed pallas TPU operations."""

import functools
from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import lax
from jax._src import test_util as jtu
from jax._src.pallas.pallas_call import _trace_to_jaxpr
from jax.experimental import mesh_utils
from jax.experimental import pallas as pl
from jax.experimental import shard_map
from jax.experimental.pallas import tpu as pltpu
import jax.numpy as jnp
import numpy as np

jax.config.parse_flags_with_absl()

P = jax.sharding.PartitionSpec

partial = functools.partial


class PallasCallRemoteDMATest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    if jax.device_count() < 2:
      self.skipTest('Only >=2 devices are supported.')
    if not jtu.is_device_tpu_at_least(5):
      self.skipTest('Only works with TPU v5')

  @parameterized.named_parameters(
      ('vmem', pltpu.TPUMemorySpace.VMEM),
      ('hbm', pltpu.TPUMemorySpace.ANY),
  )
  def test_basic_remote_vmem_dma(self, mem):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        dev_id = pltpu.device_id()
        other_dev_id = 1 - dev_id
        pltpu.semaphore_signal(ready_sem, device_id=other_dev_id,
                               device_id_type=pltpu.DeviceIdType.LOGICAL)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, other_dev_id,
            device_id_type=pltpu.DeviceIdType.LOGICAL,
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    x = jnp.arange(2 * 8 * 128.0).reshape((2 * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=mem)],
          out_specs=pl.BlockSpec(memory_space=mem),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

    devices = jax.devices()[:2]
    mesh = jax.sharding.Mesh(devices, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(
      ('left', 'left'),
      ('right', 'right')
  )
  def test_pallas_call_axis_index(self, direction):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, num_devices)
        else:
          neighbor = lax.rem(my_id - 1, num_devices)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + num_devices, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(('left', 'left'), ('right', 'right'))
  def test_pallas_call_axis_index_2d_mesh(self, direction):
    # Implements very simple collective permute in a 2D mesh.
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        my_other_id = lax.axis_index('y')
        axis_size = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, axis_size)
        else:
          neighbor = lax.rem(my_id - 1, axis_size)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + axis_size, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=(my_other_id, neighbor))
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=(my_other_id, neighbor)
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(
          body,
          pltpu.SemaphoreType.REGULAR,
          pltpu.SemaphoreType.DMA,
          pltpu.SemaphoreType.DMA,
      )

    axis_size = jax.device_count() // 2
    x = jnp.arange(axis_size * 8 * 128).reshape((axis_size * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (2, axis_size), jax.devices()
    )
    mesh = jax.sharding.Mesh(device_mesh, ['y', 'x'])
    y = jax.jit(
        shard_map.shard_map(
            body,
            mesh,
            in_specs=P('x', None),
            out_specs=P('x', None),
            check_rep=False,
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  def test_barrier_semaphore(self):
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        neighbor = lax.rem(my_id + 1, num_devices)
        barrier_sem = pltpu.get_barrier_semaphore()
        pltpu.semaphore_signal(barrier_sem, device_id=neighbor)
        pltpu.semaphore_wait(barrier_sem)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        ).wait()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
          compiler_params=dict(mosaic=dict(collective_id=0)),
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[-8:], x[:-8]])
    np.testing.assert_allclose(y, expected)


class PallasCallRemoteDMAInterpretTest(parameterized.TestCase):
  @parameterized.parameters(('left',), ('right',))
  def test_interpret_remote_dma_ppermute(self, permutation):
    if jax.device_count() <= 1:
      self.skipTest('Test requires multiple devices.')
    num_devices = jax.device_count()
    if permutation == 'left':
      permute_fn = lambda x: lax.rem(x + num_devices - 1, num_devices)
    else:
      permute_fn = lambda x: lax.rem(x + num_devices + 1, num_devices)

    # Construct a kernel which performs a ppermute based on permute_fn.
    def test_kernel(x_ref,
                    o_ref,
                    copy_send_sem,
                    copy_recv_sem,
                ):
      o_ref[...] = jnp.zeros_like(o_ref[...])
      my_id = lax.axis_index('x')
      dst_device = permute_fn(my_id)
      input_to_output_copy = pltpu.make_async_remote_copy(
          src_ref=x_ref,
          dst_ref=o_ref,
          send_sem=copy_send_sem,
          recv_sem=copy_recv_sem,
          device_id=dst_device,
          device_id_type=pltpu.DeviceIdType.LOGICAL,
      )
      input_to_output_copy.start()
      input_to_output_copy.wait()

    out_shape = (jax.ShapeDtypeStruct((8, 128), jnp.float32))
    grid_spec = pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
            ],
            scratch_shapes=(
                [pltpu.SemaphoreType.DMA] * 2
            )
        )

    devices = mesh_utils.create_device_mesh((1, num_devices))
    mesh = jax.sharding.Mesh(devices, P(None, 'x'))
    sharding = jax.sharding.NamedSharding(mesh, P(None, 'x'))
    unsharded_arr = jax.random.normal(
        jax.random.key(0), shape=(8, 128 * num_devices))
    sharded_arr = jax.device_put(unsharded_arr, sharding)

    kernel = pl.pallas_call(
        test_kernel,
        out_shape=out_shape,
        grid_spec=grid_spec,
        interpret=True,
    )
    compiled_func = jax.jit(shard_map.shard_map(
      kernel,
      mesh=mesh,
      in_specs=P(None, 'x'),
      out_specs=P(None, 'x'),
      check_rep=False))
    result = compiled_func(sharded_arr)

    perm = tuple((src, permute_fn(src)) for src in range(num_devices))
    perm = jax.tree_util.tree_map(int, perm)
    def lax_permute(x):
      return lax.ppermute(x, 'x', perm)
    expected = jax.jit(shard_map.shard_map(lax_permute,
                                   mesh=mesh,
                                   in_specs=P(None, 'x'),
                                   out_specs=P(None, 'x')))(sharded_arr)
    np.testing.assert_array_equal(result, expected)


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
