#!/bin/bash
set -e
cd "$(dirname "$0")"
exec "${PYTHON_BIN:-python3}" cloud_run_ops_workbench.py --deploy
