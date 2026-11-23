#!/usr/bin/env bash
# Real-environment check: a failing sync service must trigger the shipped
# config-saver-notify@.service through OnFailure= (real systemd --user, real
# notify-send; one desktop notification is shown). Touches no installed unit:
# it uses transient units with a random suffix, removed on every exit path.
#
#   scripts/check-sync-notify.sh                 # expected: PASS
#   NOTIFY_TEMPLATE=nonexistent-notify@ scripts/check-sync-notify.sh   # expected: FAIL
set -u
here="$(cd "$(dirname "$0")/.." && pwd)"
sfx="$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fail_unit="cs-sync-check-$sfx"
tmpl="${NOTIFY_TEMPLATE:-cs-notify-check-$sfx@}"
notify_unit="${tmpl}${fail_unit}.service"
dropdir="$XDG_RUNTIME_DIR/systemd/user"   # transient, tmpfs, gone on logout
shipped="$here/contrib/systemd/user/config-saver-notify@.service"

cleanup() {
  systemctl --user stop "$fail_unit.service" "$notify_unit" 2>/dev/null
  systemctl --user reset-failed "$fail_unit.service" "$notify_unit" 2>/dev/null
  rm -f "$dropdir/cs-notify-check-$sfx@.service"
  systemctl --user daemon-reload 2>/dev/null
}
trap cleanup EXIT

die() { echo "FAIL: $*"; exit 1; }

[ -f "$shipped" ] || die "missing $shipped"
# Phase 1: install the SHIPPED notify template under a unique name (runtime dir).
mkdir -p "$dropdir"
cp "$shipped" "$dropdir/cs-notify-check-$sfx@.service"
systemctl --user daemon-reload || die "daemon-reload"
echo "PASS phase 1: notify template installed as cs-notify-check-$sfx@"

# Phase 2: run a failing service whose OnFailure= targets the notify unit.
systemd-run --user --unit="$fail_unit" --wait --quiet \
  -p Type=oneshot -p "OnFailure=$notify_unit" /bin/false >/dev/null 2>&1
rc=$?
[ "$rc" -ne 0 ] || die "the failing service did not fail"
echo "PASS phase 2: failing service failed (rc=$rc)"

# Phase 3: the notify unit must have run (wait on state, no fixed sleep).
ts=""
for _ in $(seq 1 50); do
  ts="$(systemctl --user show -p ExecMainExitTimestamp --value "$notify_unit" 2>/dev/null)"
  [ -n "$ts" ] && break
  sleep 0.1
done
[ -n "$ts" ] || die "notify unit $notify_unit never ran (OnFailure= did not fire)"
status="$(systemctl --user show -p ExecMainStatus --value "$notify_unit")"
[ "$status" = "0" ] || die "notify unit exited with status $status"
echo "PASS phase 3: $notify_unit ran at '$ts' (exit status 0)"
echo "ALL PASS"
