#!/bin/bash
# Configure RD_ROS_SETUP with a trusted, absolute setup.bash path when using ROS2.
set -eo pipefail
if [[ -n "${RD_ROS_SETUP:-}" ]]; then
    if [[ "$RD_ROS_SETUP" != /* || ! -f "$RD_ROS_SETUP" ]]; then
        printf '%s\n' 'RD_ROS_SETUP must name an existing absolute setup.bash' >&2
        exit 2
    fi
    source "$RD_ROS_SETUP"
fi
exec /usr/bin/python3.12 /usr/local/lib/remote-driving/manage_release.py run --root "$1"
