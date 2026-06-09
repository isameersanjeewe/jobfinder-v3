#!/bin/bash
set -e

echo "Installing Python 3.11..."
apt-get update
apt-get install -y python3.11 python3.11-venv python3.11-dev

# Use Python 3.11 explicitly
/usr/bin/python3.11 -m pip install --upgrade pip
/usr/bin/python3.11 -m pip install -r requirements.txt

echo "Build complete with Python 3.11"
