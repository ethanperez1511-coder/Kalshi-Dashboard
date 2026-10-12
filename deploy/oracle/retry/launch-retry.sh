#!/usr/bin/env bash
# Retry an Always Free A1 launch across Ashburn's availability domains until
# capacity appears. Uses the restricted KALSHI_RETRY profile (RETRY.md).
set -uo pipefail
cd "$(dirname "$0")"
[ -f retry.env ] || { echo "copy retry.env.example to retry.env and fill it in"; exit 2; }
# shellcheck disable=SC1091
source retry.env
export OCI_CLI_PROFILE=KALSHI_RETRY

case "$SSH_PUBLIC_KEY_FILE" in *.pub) ;; *) echo "SSH_PUBLIC_KEY_FILE must be the .pub file"; exit 2;; esac

ads=$(oci iam availability-domain list --compartment-id "$COMPARTMENT_OCID" \
        --query 'data[].name' --raw-output | tr -d '[]", ' | grep -v '^$')
image=$(oci compute image list --compartment-id "$COMPARTMENT_OCID" \
          --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
          --shape VM.Standard.A1.Flex --sort-by TIMECREATED --sort-order DESC \
          --query 'data[0].id' --raw-output)
[ -n "$image" ] || { echo "no Ubuntu 24.04 aarch64 image found"; exit 2; }
echo "image $image" | tee -a retry.log

attempt=0
while true; do
  for ad in $ads; do
    attempt=$((attempt + 1))
    ts=$(date -u +%FT%TZ)
    out=$(oci compute instance launch \
      --compartment-id "$COMPARTMENT_OCID" --availability-domain "$ad" \
      --shape VM.Standard.A1.Flex \
      --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}" \
      --image-id "$image" --subnet-id "$SUBNET_OCID" --assign-public-ip true \
      --boot-volume-size-in-gbs "$BOOT_GB" --display-name "$DISPLAY_NAME" \
      --ssh-authorized-keys-file "$SSH_PUBLIC_KEY_FILE" \
      --wait-for-state RUNNING --max-wait-seconds 600 2>&1)
    if echo "$out" | grep -q '"lifecycle-state": "RUNNING"'; then
      id=$(echo "$out" | grep -m1 '"id": "ocid1.instance' | cut -d'"' -f4)
      ip=$(oci compute instance list-vnics --instance-id "$id" --query 'data[0]."public-ip"' --raw-output)
      echo "$ts attempt $attempt $ad: SUCCESS $id $ip" | tee -a retry.log
      exit 0
    fi
    reason=$(echo "$out" | grep -o '"message": "[^"]*"' | head -1)
    echo "$ts attempt $attempt $ad: ${reason:-failed}" | tee -a retry.log
    case "$out" in
      *NotAuthorized*) echo "permission problem, not capacity: fix the policy (RETRY.md §2)"; exit 1;;
      *LimitExceeded*) echo "a limit, not capacity: an A1 instance may already exist"; exit 1;;
    esac
    sleep "$SLEEP_SECONDS"
  done
done
