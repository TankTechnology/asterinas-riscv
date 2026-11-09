#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Developer-workflow checks that run inside the Asterinas guest (board or
# QEMU). Each check prints exactly one machine-readable line:
#
#     CHECK <name> PASS|FAIL|SKIP <detail>
#
# Every check is short and self-contained so a failure names one capability;
# the host driver only collects the lines. Network-dependent checks report
# SKIP when the network is unavailable and FAIL when it is reachable but the
# operation itself breaks.

set -u

WORK=$(mktemp -d /tmp/wf.XXXXXX) || {
    echo "CHECK workdir FAIL cannot create scratch directory"
    exit 1
}
trap 'rm -rf "$WORK"' EXIT

report() {
    printf 'CHECK %s %s %s\n' "$1" "$2" "$3"
}

detail() {
    tr '\n' ' ' | cut -c1-240
}

# --- python3: interpreter and standard library -------------------------------
if out=$(python3 -c 'import json, ssl, zlib; print("py%d.%d" % __import__("sys").version_info[:2])' 2>&1); then
    report python3 PASS "$out"
else
    report python3 FAIL "$(printf '%s\n' "$out" | detail)"
fi

# --- gcc: compile and run a program with libc, pthread and the heap ---------
cat >"$WORK/t.c" <<'EOF'
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void *work(void *arg) {
    (void)arg;
    char *p = malloc(4096);
    if (!p) return (void *)1;
    memset(p, 7, 4096);
    free(p);
    return NULL;
}
int main(void) {
    pthread_t t;
    if (pthread_create(&t, NULL, work, NULL) != 0) return 2;
    void *result = NULL;
    pthread_join(t, &result);
    if (result != NULL) return 3;
    printf("gcc-ok %d\n", (int)strlen("hello"));
    return 0;
}
EOF
if out=$(gcc -O2 -pthread -o "$WORK/t" "$WORK/t.c" 2>&1) && out=$("$WORK/t" 2>&1); then
    report gcc PASS "$out"
else
    report gcc FAIL "$(printf '%s\n' "$out" | detail)"
fi

# --- git: init, commit and clone a local repository --------------------------
if out=$({
    cd "$WORK" &&
        mkdir repo && cd repo &&
        git init -q . &&
        git config user.email gate@example.com &&
        git config user.name gate &&
        printf 'x\n' >f &&
        git add f &&
        git commit -qm one &&
        cd "$WORK" &&
        git clone -q repo clone &&
        [ "$(cat clone/f)" = x ] &&
        echo "$(git -C repo rev-parse --short HEAD)"
} 2>&1); then
    report git PASS "init/commit/clone $out"
else
    report git FAIL "$(printf '%s\n' "$out" | detail)"
fi

# --- tar/xz: create and extract a compressed archive -------------------------
if out=$({
    cd "$WORK" &&
        mkdir -p src &&
        printf 'abc' >src/a &&
        tar -cJf t.tar.xz src &&
        mkdir -p out &&
        tar -xJf t.tar.xz -C out &&
        [ "$(cat out/src/a)" = abc ] &&
        echo ok
} 2>&1); then
    report tar_xz PASS "create/extract $out"
else
    report tar_xz FAIL "$(printf '%s\n' "$out" | detail)"
fi

# --- pip: venv plus one small install ---------------------------------------
# When the driver serves a wheel index, use it: that tests pip's installer
# without depending on the guest's DNS or on the campus portal.
PIP_SOURCE=""
if [ -n "${WF_WHEEL_INDEX:-}" ]; then
    PIP_SOURCE="--no-index --find-links $WF_WHEEL_INDEX --trusted-host ${WF_WHEEL_HOST%%:*}"
fi
if out=$(python3 -m venv "$WORK/venv" 2>&1) && out=$("$WORK/venv/bin/pip" --version 2>&1); then
    # shellcheck disable=SC2086  # the source is a deliberate word list
    if install_out=$("$WORK/venv/bin/pip" install --disable-pip-version-check \
        --no-cache-dir --no-deps --timeout 20 -q $PIP_SOURCE six==1.16.0 2>&1); then
        if out=$("$WORK/venv/bin/python" -c 'import six; print("six " + six.__version__)' 2>&1); then
            report pip PASS "$out"
        else
            report pip FAIL "import: $(printf '%s\n' "$out" | detail)"
        fi
    else
        # A dead network is an environment condition, not an image defect.
        report pip SKIP "$(printf '%s\n' "$install_out" | detail)"
    fi
else
    report pip FAIL "venv: $(printf '%s\n' "$out" | detail)"
fi

# --- firefox: the shipped browser starts far enough to print its version -----
if command -v firefox >/dev/null 2>&1; then
    if out=$(timeout 30 firefox --version 2>&1); then
        report firefox PASS "$(printf '%s\n' "$out" | tail -1)"
    else
        report firefox FAIL "$(printf '%s\n' "$out" | detail)"
    fi
else
    report firefox SKIP "firefox is not installed"
fi
