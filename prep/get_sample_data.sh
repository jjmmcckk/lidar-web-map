#!/usr/bin/env bash
# Download the Autzen sample point cloud from the PDAL project's test data (about 3.7 MB)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
curl -L -o data/autzen_trim.las https://raw.githubusercontent.com/PDAL/PDAL/master/test/data/las/autzen_trim.las
echo "Saved data/autzen_trim.las"
