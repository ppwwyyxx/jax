/* Copyright 2024 The JAX Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#ifndef JAXLIB_CPU_LAPACK_H_
#define JAXLIB_CPU_LAPACK_H_

#include "jaxlib/cpu/lapack_kernels.h"
#include "xla/ffi/api/ffi.h"

namespace jax {

// FFI Definition Macros (by DataType)

#define JAX_CPU_DEFINE_TRSM(name, data_type)                                  \
  XLA_FFI_DEFINE_HANDLER(name, TriMatrixEquationSolver<data_type>::Kernel,    \
                         ::xla::ffi::Ffi::Bind()                              \
                             .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)       \
                             .Arg<::xla::ffi::Buffer<data_type>>(/*y*/)       \
                             .Arg<::xla::ffi::BufferR0<data_type>>(/*alpha*/) \
                             .Ret<::xla::ffi::Buffer<data_type>>(/*y_out*/)   \
                             .Attr<MatrixParams::Side>("side")                \
                             .Attr<MatrixParams::UpLo>("uplo")                \
                             .Attr<MatrixParams::Transpose>("trans_x")        \
                             .Attr<MatrixParams::Diag>("diag"))

#define JAX_CPU_DEFINE_GETRF(name, data_type)                \
  XLA_FFI_DEFINE_HANDLER(                                    \
      name, LuDecomposition<data_type>::Kernel,              \
      ::xla::ffi::Ffi::Bind()                                \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)         \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/)     \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*ipiv*/) \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/))

#define JAX_CPU_DEFINE_GEQRF(name, data_type)                \
  XLA_FFI_DEFINE_HANDLER(                                    \
      name, QrFactorization<data_type>::Kernel,              \
      ::xla::ffi::Ffi::Bind()                                \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)         \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/)     \
          .Ret<::xla::ffi::Buffer<data_type>>(/*tau*/)       \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/) \
          .Ret<::xla::ffi::Buffer<data_type>>(/*work*/))

#define JAX_CPU_DEFINE_ORGQR(name, data_type)                \
  XLA_FFI_DEFINE_HANDLER(                                    \
      name, OrthogonalQr<data_type>::Kernel,                 \
      ::xla::ffi::Ffi::Bind()                                \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)         \
          .Arg<::xla::ffi::Buffer<data_type>>(/*tau*/)       \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/)     \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/) \
          .Ret<::xla::ffi::Buffer<data_type>>(/*work*/))

#define JAX_CPU_DEFINE_POTRF(name, data_type)            \
  XLA_FFI_DEFINE_HANDLER(                                \
      name, CholeskyFactorization<data_type>::Kernel,    \
      ::xla::ffi::Ffi::Bind()                            \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)     \
          .Attr<MatrixParams::UpLo>("uplo")              \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/) \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/))

#define JAX_CPU_DEFINE_GESDD(name, data_type)                 \
  XLA_FFI_DEFINE_HANDLER(                                     \
      name, SingularValueDecomposition<data_type>::Kernel,    \
      ::xla::ffi::Ffi::Bind()                                 \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)          \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/)      \
          .Ret<::xla::ffi::Buffer<data_type>>(/*s*/)          \
          .Ret<::xla::ffi::Buffer<data_type>>(/*u*/)          \
          .Ret<::xla::ffi::Buffer<data_type>>(/*vt*/)         \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/)  \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*iwork*/) \
          .Ret<::xla::ffi::Buffer<data_type>>(/*work*/)       \
          .Attr<svd::ComputationMode>("mode"))

#define JAX_CPU_DEFINE_GESDD_COMPLEX(name, data_type)                        \
  XLA_FFI_DEFINE_HANDLER(                                                    \
      name, SingularValueDecompositionComplex<data_type>::Kernel,            \
      ::xla::ffi::Ffi::Bind()                                                \
          .Arg<::xla::ffi::Buffer<data_type>>(/*x*/)                         \
          .Ret<::xla::ffi::Buffer<data_type>>(/*x_out*/)                     \
          .Ret<::xla::ffi::Buffer<::xla::ffi::ToReal(data_type)>>(/*s*/)     \
          .Ret<::xla::ffi::Buffer<data_type>>(/*u*/)                         \
          .Ret<::xla::ffi::Buffer<data_type>>(/*vt*/)                        \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*info*/)                 \
          .Ret<::xla::ffi::Buffer<::xla::ffi::ToReal(data_type)>>(/*rwork*/) \
          .Ret<::xla::ffi::Buffer<LapackIntDtype>>(/*iwork*/)                \
          .Ret<::xla::ffi::Buffer<data_type>>(/*work*/)                      \
          .Attr<svd::ComputationMode>("mode"))

// FFI Handlers

JAX_CPU_DEFINE_TRSM(blas_strsm_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_TRSM(blas_dtrsm_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_TRSM(blas_ctrsm_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_TRSM(blas_ztrsm_ffi, ::xla::ffi::DataType::C128);

JAX_CPU_DEFINE_GETRF(lapack_sgetrf_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_GETRF(lapack_dgetrf_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_GETRF(lapack_cgetrf_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_GETRF(lapack_zgetrf_ffi, ::xla::ffi::DataType::C128);

JAX_CPU_DEFINE_GEQRF(lapack_sgeqrf_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_GEQRF(lapack_dgeqrf_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_GEQRF(lapack_cgeqrf_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_GEQRF(lapack_zgeqrf_ffi, ::xla::ffi::DataType::C128);

JAX_CPU_DEFINE_ORGQR(lapack_sorgqr_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_ORGQR(lapack_dorgqr_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_ORGQR(lapack_cungqr_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_ORGQR(lapack_zungqr_ffi, ::xla::ffi::DataType::C128);

JAX_CPU_DEFINE_POTRF(lapack_spotrf_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_POTRF(lapack_dpotrf_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_POTRF(lapack_cpotrf_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_POTRF(lapack_zpotrf_ffi, ::xla::ffi::DataType::C128);

JAX_CPU_DEFINE_GESDD(lapack_sgesdd_ffi, ::xla::ffi::DataType::F32);
JAX_CPU_DEFINE_GESDD(lapack_dgesdd_ffi, ::xla::ffi::DataType::F64);
JAX_CPU_DEFINE_GESDD_COMPLEX(lapack_cgesdd_ffi, ::xla::ffi::DataType::C64);
JAX_CPU_DEFINE_GESDD_COMPLEX(lapack_zgesdd_ffi, ::xla::ffi::DataType::C128);

#undef JAX_CPU_DEFINE_TRSM
#undef JAX_CPU_DEFINE_GETRF
#undef JAX_CPU_DEFINE_GEQRF
#undef JAX_CPU_DEFINE_ORGQR
#undef JAX_CPU_DEFINE_POTRF
#undef JAX_CPU_DEFINE_GESDD
#undef JAX_CPU_DEFINE_GESDD_COMPLEX

}  // namespace jax

#endif  // JAXLIB_CPU_LAPACK_H_
