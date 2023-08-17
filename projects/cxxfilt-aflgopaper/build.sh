#!/bin/bash -eu
# Copyright 2019 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

# build project
if [ "$SANITIZER" = undefined ]; then
    export CFLAGS="$CFLAGS -fno-sanitize=unsigned-integer-overflow"
    export CXXFLAGS="$CXXFLAGS -fno-sanitize=unsigned-integer-overflow"
fi
#cd binutils-gdb

## Comment out the lines of logging to stderror from elfcomm.c
## This is to make it nicer to read the output of libfuzzer.
#Cd binutils
#Sed -i 's/vfprintf (stderr/\/\//' elfcomm.c
#Sed -i 's/fprintf (stderr/\/\//' elfcomm.c
#Cd ../

./configure --disable-gdb --enable-targets=all
make MAKEINFO=true && true

# Make fuzzer directory
mkdir fuzz
cp ../fuzz_cxxfilt.c fuzz/
cd fuzz

#for i in fuzz_disassemble fuzz_bfd; do
#    $CC $CFLAGS -I ../include -I ../bfd -I ../opcodes -c $i.c -o $i.o
#    $CXX $CXXFLAGS $i.o -o $OUT/$i $LIB_FUZZING_ENGINE ../opcodes/libopcodes.a ../bfd/libbfd.a ../libiberty/libiberty.a ../zlib/libz.a
#done

# Now compile the src/binutils fuzzers
cd ../binutils

cp ../../fuzz_cxxfilt.c .

# Modify main functions so we dont have them anymore
sed 's/main (int argc/old_main (int argc, char **argv);\nint old_main (int argc/' cxxfilt.c >> cxxfilt.h

# Compile object file
$CC $CFLAGS -DHAVE_CONFIG_H -I. -I../bfd -I./../bfd -I./../include -I./../zlib -DLOCALEDIR="\"/usr/local/share/locale\"" -Dbin_dummy_emulation=bin_vanilla_emulation -W -Wall -MT fuzz_cxxfilt.o -MD -MP -c -o fuzz_cxxfilt.o fuzz_cxxfilt.c

## cxxfilt
$CXX $CXXFLAGS $LIB_FUZZING_ENGINE -W -Wall -I./../zlib -o fuzz_cxxfilt fuzz_cxxfilt.o bucomm.o version.o filemode.o ../bfd/.libs/libbfd.a -L/src/binutils-gdb/zlib -lz ../libiberty/libiberty.a 
mv fuzz_cxxfilt $OUT/fuzz_cxxfilt
