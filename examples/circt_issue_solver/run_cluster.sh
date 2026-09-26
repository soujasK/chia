#!/usr/bin/env bash
set -e

source ~/.bashrc
conda activate circtissues

export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-a3-chia-hack26ath-7735}"
export CHIA_HEAD=localhost
export RAY_memory_usage_threshold=0.99
export RAY_memory_monitor_refresh_ms=0
export NINJAFLAGS="-j4"
export CMAKE_BUILD_PARALLEL_LEVEL=4
export SSH_AUTH_SOCK=/tmp/ssh-agent.sock

if [ ! -S /tmp/ssh-agent.sock ]; then
    rm -f /tmp/ssh-agent.sock
    eval $(ssh-agent -a /tmp/ssh-agent.sock)
fi

cd /mnt/c/Users/kudch/Downloads/circt-context-precision-gate/chia/examples/circt_issue_solver

case "$1" in
    down)
        chia down -y --no-scoped cluster_opencode_vertex.yaml || true
        docker rm -f circt_issue_solver_opencode_llm_${USER}-0 circt_issue_solver_worker_${USER}-0 2>/dev/null || true
        ray stop || true
        ;;
    up)
        chia up -y cluster_opencode_vertex.yaml
        ;;
    status)
        ray status
        docker ps
        ;;
    single)
        shift
        ISSUE=${1:-7388}
        ./fix_issues_submit.sh --backend opencode --issue "$ISSUE"
        ;;
    validation)
        python circt_issue_loop.py --backend opencode --validation-set 2>&1 | tee validation_gated.log
        ;;
    *)
        echo "Usage: $0 {down|up|status|single <issue>|validation}"
        ;;
esac
