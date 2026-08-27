#!/bin/sh
# Builds libnetcard.so -- the original Linux_NetCard.cpp (send_frame() and
# friends) plus a thin extern "C" wrapper (netcard_capi.cpp), exposed for
# python/bridge.py to call via ctypes. Run this once, or again after editing
# either .cpp file.
set -e
cd "$(dirname "$0")"
g++ -O3 -fPIC -shared -o libnetcard.so Linux_NetCard.cpp netcard_capi.cpp
echo "built $(pwd)/libnetcard.so"
