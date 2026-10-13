# Scripted A1 capacity retry (ruled 2026-10-12)

**Ruling:** a restricted OCI user that can only create instances. Its API key
lives only on your Mac, never in GitHub, never in a chat.

Console labels move between releases. Where one differs, look for the same
concept. Do the steps in order: no key is created before step 4, and the
retry does not run until step 7 has passed.

## 1. Compartment (console, as your admin login)
1. Sign in at cloud.oracle.com. The region (top right) must say **US East
   (Ashburn)**.
2. ☰ → **Identity & Security** → **Compartments** → **Create compartment**.
3. Name `kalshi`. Description `kalshi paper VM`. Parent: the root (your
   tenancy name). **Create compartment**.
4. Open `kalshi` → next to **OCID** click **Copy**. Paste it in a scratch note
   as `COMPARTMENT_OCID`. (OCIDs are identifiers, not secrets.)

## 2. Network inside the compartment (console)
1. ☰ → **Networking** → **Virtual cloud networks**. In the left **Compartment**
   picker choose `kalshi`.
2. **Start VCN Wizard** → **Create VCN with Internet Connectivity** → **Start
   VCN Wizard**.
3. Name `kalshi-vcn`, compartment `kalshi`; leave the CIDRs at their defaults.
   **Next** → **Create**. Wait for it to finish, then **View VCN**.
4. **Subnets** → `public subnet-kalshi-vcn` → **OCID** → **Copy**. Note it as
   `SUBNET_OCID`.
5. Still in that subnet → **Security** (or Security Lists) → **Default
   Security List for kalshi-vcn** → **Ingress Rules**. Edit the TCP 22 rule:
   Source CIDR = your IP `/32`. Find your IP in Terminal with
   `curl -4 ifconfig.me`. Leave the ICMP rules, and add nothing else
   (SPEC §2.3).

## 3. Group, user and policy (console)
1. ☰ → **Identity & Security** → **Domains** → **Default** →
   **User management** → **Groups** → **Create group**. Name
   `kalshi-launchers`. **Create**.
2. **User management** → **Users** → **Create user**.
   - Last name `kalshi-retry`.
   - Username / email: an address you control. If "Use the email address as
     the username" is shown, untick it and set the username to
     `kalshi-retry`.
   - Groups: tick `kalshi-launchers`. **Create**.
3. On `kalshi-retry` → **Edit user capabilities**. Leave **only API keys**
   ticked. Untick Local password, SMTP credentials, Auth tokens, Customer
   secret keys, OAuth 2.0 client credentials and Database passwords. **Save
   changes**. This user can never sign in to the console.
4. ☰ → **Identity & Security** → **Policies**. Compartment picker: the
   **root** (tenancy). **Create Policy**.
   - Name `kalshi-launch-only`. Description `retry user: create only`.
   - Compartment: root.
   - **Show manual editor**, then paste exactly:

```
Allow group kalshi-launchers to manage instances in compartment kalshi where request.permission = 'INSTANCE_CREATE'
Allow group kalshi-launchers to manage volume-family in compartment kalshi where request.permission = 'VOLUME_CREATE'
Allow group kalshi-launchers to inspect instances in compartment kalshi
Allow group kalshi-launchers to read vnic-attachments in compartment kalshi
Allow group kalshi-launchers to read instance-images in compartment kalshi
Allow group kalshi-launchers to use virtual-network-family in compartment kalshi
Allow group kalshi-launchers to read app-catalog-listing in tenancy
Allow group kalshi-launchers to inspect compartments in tenancy
```
   **Create**.

**Why this cannot terminate or delete.**
- In OCI, terminate and delete exist only under the `manage` verb.
- Both `manage` lines carry a `where request.permission = '…_CREATE'`
  condition, so they grant creation and nothing else. You can check this by
  eye: every line that says `manage` ends in a `_CREATE` condition.
- Stop, start and reboot are under `use instances`. This policy grants only
  `inspect instances`, so those are refused too.
- `use virtual-network-family` attaches the VM to the subnet. Deleting a VCN
  or subnet needs `manage`, which is not granted.

Step 7 proves all of this against a real instance instead of trusting the
reasoning.

**One thing the policy cannot remove:** an OCI user can always manage its
*own* API keys. The worst this allows is replacing its key, which gives the
same create-only rights.

## 4. Keys, generated on your Mac (Terminal)
The SSH key is for the VM. The API key is what the retry user signs with.
Both private halves stay in your home directory.

```bash
# SSH key (skip if ~/.ssh/oci_kalshi already exists). Set a passphrase when asked.
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/oci_kalshi -C "kalshi-oci"
ssh-add --apple-use-keychain ~/.ssh/oci_kalshi

# OCI CLI and the retry user's API key
brew install oci-cli
mkdir -p ~/.oci && chmod 700 ~/.oci
oci setup keys --key-name kalshi-retry --output-dir ~/.oci
#   passphrase prompt: press Enter (none). See the note below.
chmod 600 ~/.oci/kalshi-retry.pem
pbcopy < ~/.oci/kalshi-retry_public.pem     # copies the PUBLIC key only
```

**Why the API key has no passphrase.**
- The retry script calls `oci` several times a minute for hours. With a
  passphrase, the CLI would prompt on every call.
- The only alternative is writing the passphrase into `~/.oci/config` in
  plain text, which is no better.
- What the key can do is bounded instead:
  - it can only create instances in `kalshi`;
  - your account is not upgraded, so nothing it creates can be billed;
  - the file is mode 600 under FileVault;
  - the key is deleted in step 9.
- The SSH key does have a passphrase, held in the macOS Keychain.

## 5. Register the public key (console)
1. **Domains** → **Default** → **Users** → `kalshi-retry` → **API keys** →
   **Add API key**.
2. Choose **Paste a public key**, paste (⌘V), then **Add**.
   - Check that what you pasted starts `-----BEGIN PUBLIC KEY-----`.
   - If it says PRIVATE, stop: you copied the wrong file. Nothing was sent
     until you clicked Add.
3. The console shows a **Configuration file preview**. Click **Copy**, then
   **Close**. It holds OCIDs, the fingerprint and the region; none of them is
   secret.

Back in Terminal:
```bash
nano ~/.oci/config
```
- Paste the preview.
- Change the first line from `[DEFAULT]` to `[KALSHI_RETRY]`.
- Set `key_file=~/.oci/kalshi-retry.pem`.
- Save with Ctrl-O, Enter, then exit with Ctrl-X.

```bash
oci setup repair-file-permissions --file ~/.oci/config
oci --profile KALSHI_RETRY compute instance list --compartment-id <COMPARTMENT_OCID>
#   expected: no output, or an empty list, with no error = the key works
```

## 6. Fill in retry.env (Terminal)
```bash
cd ~/Desktop/kalshi-migration/deploy/oracle/retry
cp retry.env.example retry.env
nano retry.env    # COMPARTMENT_OCID and SUBNET_OCID from your scratch note
```
`retry.env` is git-ignored and holds identifiers only.

## 7. Prove the user is restricted (mandatory, before any retry)
```bash
./verify-restricted.sh
```

**What it does.**
1. As `kalshi-retry`, it launches a throwaway **Always Free
   VM.Standard.E2.1.Micro** named `kalshi-perm-test`.
   - This proves that create works end to end: VNIC, public IP and boot
     volume.
   - It gives the next step a real target. A refusal aimed at nothing
     proves nothing.
2. It checks that the user can **see** that instance. Without this, a "not
   found" below could just mean the target was invisible.
3. It asks for the test VM's boot-volume OCID. To get it: **Compute →
   Instances → `kalshi-perm-test` → Boot volume → OCID → Copy**.
4. It attempts each of these as the retry user. Every one must come back
   **404 NotAuthorizedOrNotFound**:
   - terminate, stop and reboot the instance;
   - rename the instance;
   - delete its boot volume;
   - delete the subnet, the VCN and the compartment;
   - create a policy granting itself `manage all-resources`.

**A 409 Conflict counts as a failure.** It means the permission was granted
and only the resource's state (attached, not empty) stopped it.

**Pass looks like:** nine `PASS refused:` lines, then `ALL PASS`, and exit 0.

**On any FAIL:**
- Stop.
- Console → `kalshi-retry` → **API keys** → **⋯ → Delete** on the key.
- Fix the policy.
- If "grant itself more" failed, delete the policy `kalshi-escalation-test`
  first.

If step 1 fails with **NotAuthorized**, the policy is missing a permission
that launch needs. Add only the permission the message names, never the
whole family, then rerun the script.

## 8. Your own check, then clean up (console)
1. **Compute → Instances** (compartment `kalshi`). `kalshi-perm-test` must
   still be **Running**. That is independent proof the terminate was refused.
2. **⋯ → Terminate**. Tick **Permanently delete the attached boot volume** →
   **Terminate instance**. You do this as admin; the retry user cannot.

## 9. Run the retry
```bash
caffeinate -i ./launch-retry.sh       # keeps the Mac awake; Ctrl-C stops it
```
- It cycles AD-1 → AD-2 → AD-3 for **VM.Standard.A1.Flex, 1 OCPU / 6 GB**,
  Ubuntu 24.04 aarch64, a 100 GB boot volume and your SSH **public** key.
- It waits 90 s between attempts and stops at the first success.
- It prints the instance OCID and public IP, and logs each attempt to
  `retry.log`.

**Once the VM exists:**
1. Console → `kalshi-retry` → **API keys** → **⋯ → Delete**.
2. Then `rm ~/.oci/kalshi-retry.pem` on the Mac.
3. Optionally deactivate the user (**⋯ → Deactivate**). It has no further
   purpose.
4. Tell me the VM exists. Phase B starts only then.
