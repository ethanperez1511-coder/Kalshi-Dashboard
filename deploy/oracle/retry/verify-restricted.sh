#!/usr/bin/env bash
# Prove the KALSHI_RETRY user can create an instance and can terminate, stop,
# modify or delete nothing. Run once, before the first retry (RETRY.md step 7).
#
# A refusal is only evidence if there was something real to refuse. So this
# first launches a throwaway Always Free E2.1.Micro named kalshi-perm-test AS
# the restricted user (which also proves create works end to end), then aims
# every destructive call at that instance and its network.
#
# PASS for a destructive call = 404 NotAuthorizedOrNotFound.
# A 409 Conflict is a FAIL: the permission was granted and only the
# resource's state (attached, not empty) stopped it.
set -uo pipefail
cd "$(dirname "$0")"
[ -f retry.env ] || { echo "copy retry.env.example to retry.env and fill it in"; exit 2; }
# shellcheck disable=SC1091
source retry.env
export OCI_CLI_PROFILE=KALSHI_RETRY
TEST_NAME=kalshi-perm-test
fails=0

# denied <label> <oci args...>: the call must be refused as unauthorized.
denied() {
  local label=$1; shift
  local out rc
  out=$(oci "$@" 2>&1); rc=$?
  if [ $rc -ne 0 ] && echo "$out" | grep -q '"status": 404' \
       && echo "$out" | grep -q 'NotAuthorizedOrNotFound'; then
    echo "PASS  refused: $label"
  else
    echo "FAIL  NOT refused as unauthorized: $label"
    if [ $rc -eq 0 ]; then echo "      (the call SUCCEEDED)"; fi
    echo "$out" | grep -E '"(status|code|message)"' | sed 's/^/      /'
    fails=$((fails + 1))
  fi
}

find_test() {
  local id
  id=$(oci compute instance list --compartment-id "$COMPARTMENT_OCID" \
         --display-name "$TEST_NAME" --lifecycle-state RUNNING \
         --query 'data[0].id' --raw-output 2>/dev/null)
  case "$id" in ocid1.instance.*) echo "$id" ;; esac
}

echo "== 1. Create works (launch $TEST_NAME as KALSHI_RETRY)"
probe=$(oci compute instance list --compartment-id "$COMPARTMENT_OCID" 2>&1) \
  || { echo "cannot list instances as KALSHI_RETRY:"; echo "$probe"; exit 1; }
test_id=$(find_test)
if [ -n "$test_id" ]; then
  echo "reusing running $TEST_NAME $test_id"
else
  ads=$(oci iam availability-domain list --compartment-id "$COMPARTMENT_OCID" \
          --query 'data[].name' --raw-output | tr -d '[]" ' | tr ',' '\n' | grep -v '^$')
  [ -n "$ads" ] || { echo "cannot list availability domains"; exit 1; }
  img=$(oci compute image list --compartment-id "$COMPARTMENT_OCID" \
          --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
          --shape VM.Standard.E2.1.Micro --sort-by TIMECREATED --sort-order DESC \
          --query 'data[0].id' --raw-output)
  case "$img" in ocid1.image.*) ;; *) echo "no Ubuntu 24.04 image for E2.1.Micro: $img"; exit 1;; esac
  for ad in $ads; do
    out=$(oci compute instance launch \
      --compartment-id "$COMPARTMENT_OCID" --availability-domain "$ad" \
      --shape VM.Standard.E2.1.Micro --image-id "$img" \
      --subnet-id "$SUBNET_OCID" --assign-public-ip true \
      --display-name "$TEST_NAME" \
      --wait-for-state RUNNING --max-wait-seconds 600 2>&1)
    if echo "$out" | grep -q '"lifecycle-state": "RUNNING"'; then
      echo "PASS  launched in $ad"
      break
    fi
    msg=$(echo "$out" | grep -o '"message": "[^"]*"' | head -1)
    case "$out" in
      *NotAuthorized*)
        echo "FAIL  create refused in $ad: $msg"
        echo "      The policy is missing a permission LaunchInstance needs."
        echo "      Add only the permission the message names (RETRY.md step 3)."
        exit 1 ;;
    esac
    echo "      $ad: ${msg:-failed}; trying the next AD"
  done
  test_id=$(find_test)
fi
[ -n "$test_id" ] || { echo "no running $TEST_NAME: micro capacity in every AD? rerun later"; exit 1; }

# Control: the restricted user must SEE the target. Otherwise a 404 below
# would mean "not found", not "not allowed", and prove nothing.
name=$(oci compute instance list --compartment-id "$COMPARTMENT_OCID" \
         --query "data[?id=='$test_id'] | [0].\"display-name\"" --raw-output)
[ "$name" = "$TEST_NAME" ] || { echo "target not visible or misnamed ($name); refusing to test"; exit 1; }
echo "      target $TEST_NAME $test_id is visible"

echo
echo "In the console: Compute → Instances → $TEST_NAME → Boot volume (left"
echo "menu or tab) → the boot volume's OCID → Copy. Paste it here."
read -r -p "Boot volume OCID: " bv
case "$bv" in ocid1.bootvolume.*) ;; *) echo "not a boot volume OCID"; exit 2;; esac

vcn_id=$(oci network subnet get --subnet-id "$SUBNET_OCID" \
           --query 'data."vcn-id"' --raw-output)
case "$vcn_id" in ocid1.vcn.*) ;; *) echo "cannot read the subnet's VCN: $vcn_id"; exit 1;; esac

echo
echo "== 2. Everything destructive is refused"
denied "terminate instance"           compute instance terminate --instance-id "$test_id" --force
denied "stop instance"                compute instance action --instance-id "$test_id" --action STOP
denied "reboot instance"              compute instance action --instance-id "$test_id" --action RESET
denied "rename instance"              compute instance update --instance-id "$test_id" --display-name renamed --force
denied "delete boot volume"           bv boot-volume delete --boot-volume-id "$bv" --force
denied "delete subnet"                network subnet delete --subnet-id "$SUBNET_OCID" --force
denied "delete VCN"                   network vcn delete --vcn-id "$vcn_id" --force
denied "delete compartment kalshi"    iam compartment delete --compartment-id "$COMPARTMENT_OCID" --force
denied "grant itself more (policy)"   iam policy create --compartment-id "$COMPARTMENT_OCID" \
  --name kalshi-escalation-test --description "must be refused" \
  --statements '["Allow group kalshi-launchers to manage all-resources in compartment kalshi"]'

echo
if [ $fails -eq 0 ]; then
  echo "ALL PASS. The retry user can create and can do nothing destructive."
  echo "Now, in the console, confirm $TEST_NAME still shows Running (so the"
  echo "terminate above really was refused), then terminate it YOURSELF with"
  echo "'Permanently delete the attached boot volume' ticked (RETRY.md step 8)."
  exit 0
fi
echo "$fails FAILED. Stop. Console → Users → kalshi-retry → API keys → delete the"
echo "key, then fix the policy (RETRY.md step 3). If 'grant itself more' failed,"
echo "delete the policy kalshi-escalation-test in compartment kalshi first."
exit 1
