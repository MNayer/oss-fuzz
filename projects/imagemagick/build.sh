#!/bin/bash -eu
# Copyright 2017 Google Inc.  #
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

# Patch libheif build for old project versions
for file in $(find Magick++/fuzz/ -type f); do
	sed -i 's/\.\/configure --disable-shared --disable-go --prefix="$WORK" PKG_CONFIG_PATH="$WORK\/lib\/pkgconfig"/\.\/configure --disable-shared --disable-go --disable-examples --disable-tests --prefix="$WORK" PKG_CONFIG_PATH="$WORK\/lib\/pkgconfig"/g' $file
done

# Patch dng bug for old project versions
pattern='raw_info=libraw_init(LIBRAW_OPIONS_NO_MEMERR_CALLBACK |.*\n.*LIBRAW_OPIONS_NO_DATAERR_CALLBACK);'
sub='unsigned int\nflags;\n\tflags=LIBRAW_OPIONS_NO_DATAERR_CALLBACK;\n#if LIBRAW_SHLIB_CURRENT < 23\n\tflags|=LIBRAW_OPIONS_NO_MEMERR_CALLBACK;\n#endif\n\traw_info=libraw_init(flags);'
sed -i "N;s/$pattern/$sub/g" coders/dng.c

. Magick++/fuzz/build.sh
