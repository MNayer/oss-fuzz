#!/bin/bash -eu

# Copyright 2017-2018 Glenn Randers-Pehrson
# Copyright 2016 Google Inc.
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
################################################################################

git clean -f -x
export ASAN_OPTIONS=detect_leaks=0
./configure --disable-shared --disable-doc --disable-gdb --disable-libdecnumber --disable-readline --disable-sim --disable-ld --disable-werror
make
unset ASAN_OPTIONS
cp binutils/cxxfilt $OUT/

#pushd /src/binutils-gdb/libiberty
#echo """--- libiberty/cplus-dem.c	(revision 234607)
#+++ libiberty/cplus-dem.c	(working copy)
#@@ -1237,11 +1237,13 @@  squangle_mop_up (struct work_stuff *work)
#     {
#       free ((char *) work -> btypevec);
#       work->btypevec = NULL;
#+      work->bsize = 0;
#     }
#   if (work -> ktypevec != NULL)
#     {
#       free ((char *) work -> ktypevec);
#       work->ktypevec = NULL;
#+      work->ksize = 0;
#     }
# }""" > patch.diff
#set +e
#patch -p1 --forward < patch.diff
#set -e
#popd

#mkdir obj-aflgo; mkdir obj-aflgo/temp
#export SUBJECT=$PWD; export TMP_DIR=$PWD/obj-aflgo/temp
#export CC=$AFLGO/afl-clang-fast; export CXX=$AFLGO/afl-clang-fast++
#export LDFLAGS=-lpthread
##export ADDITIONAL="-targets=$TMP_DIR/BBtargets.txt -outdir=$TMP_DIR -flto -fuse-ld=gold -Wl,-plugin-opt=save-temps"
#
##echo $'cxxfilt.c:227\ncxxfilt.c:62\ncplus-dem.c:886\ncplus-dem.c:1203\ncplus-dem.c:1490\ncplus-dem.c:2594\ncplus-dem.c:4319' > $TMP_DIR/BBtargets.txt
#cd obj-aflgo; CFLAGS="-DFORTIFY_SOURCE=2 -fstack-protector-all -fno-omit-frame-pointer -g -Wno-error $ADDITIONAL" LDFLAGS="-ldl -lutil" ../configure --disable-shared --disable-gdb --disable-libdecnumber --disable-readline --disable-sim --disable-ld
#make clean; make
#cat $TMP_DIR/BBnames.txt | rev | cut -d: -f2- | rev | sort | uniq > $TMP_DIR/BBnames2.txt && mv $TMP_DIR/BBnames2.txt $TMP_DIR/BBnames.txt
#cat $TMP_DIR/BBcalls.txt | sort | uniq > $TMP_DIR/BBcalls2.txt && mv $TMP_DIR/BBcalls2.txt $TMP_DIR/BBcalls.txt
#cd binutils; $AFLGO/scripts/genDistance.sh $SUBJECT $TMP_DIR cxxfilt
#cd ../../; mkdir obj-dist; cd obj-dist; # work around because cannot run make distclean
#CFLAGS="-DFORTIFY_SOURCE=2 -fstack-protector-all -fno-omit-frame-pointer -g -Wno-error -distance=$TMP_DIR/distance.cfg.txt" LDFLAGS="-ldl -lutil" ../configure --disable-shared --disable-gdb --disable-libdecnumber --disable-readline --disable-sim --disable-ld
#make
#mkdir in; echo "" > in/in
#$AFLGO/afl-fuzz -m none -z exp -c 45m -i in -o out binutils/cxxfilt
## mkdir out; for i in {1..10}; do timeout -sHUP 60m $AFLGO/afl-fuzz -m none -z exp -c 45m -i in -o "out/out_$i" binutils/cxxfilt > /dev/null 2>&1 & done
