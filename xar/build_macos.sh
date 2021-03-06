#!/usr/bin/env bash
#
# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

set -e

# System includes from:
#     echo | clang++ -E -x c++ -v -
includes=(
    "/usr/local/Cellar/llvm/3.9.0/include/c++/v1"
    "/usr/local/include"
    "/usr/local/Cellar/llvm/3.9.0/lib/clang/3.9.0/include"
    "/usr/include"

    # Needed to make local #include's work
    "../../"
)
include_flags=$(for i in ${includes[*]}; do echo -n "-I$i "; done)

srcs=(
    "XarExecFuse.cpp"
    "XarMacOS.cpp"
)

out="xarexec_fuse"
clang++ -std=c++11 ${include_flags} "${srcs[@]}" -o "$out"
